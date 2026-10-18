"""诊断科学计算测试：心率区间（Karvonen）、负荷状态（CTL/ATL/TSB）、历史成绩估算。"""

from datetime import date, timedelta

from app.models.checkin_log import CheckinLog
from app.models.daily_record import DailyRecord
from app.models.member import Member
from app.services.diagnose import (
    compute_diagnosis,
    compute_load_status,
    estimate_best_race,
    format_diagnosis,
    heart_rate_zones,
    hr_max_from_age,
    zone_for_avg_hr,
)


def test_hr_max_from_age_tanaka():
    # Tanaka 2001：208 − 0.7×年龄
    assert hr_max_from_age(30) == 187  # 208 - 21
    assert hr_max_from_age(20) == 194  # 208 - 14


def test_heart_rate_zones_karvonen():
    # 年龄 30 → HRmax 187；静息 60 → HRR 127
    zones = heart_rate_zones(30, 60)
    assert len(zones) == 5
    labels = [z[0] for z in zones]
    assert labels[0].startswith("Z1") and labels[4].startswith("Z5")
    # Z2 有氧基础：60 + 60%×127 ≈ 136 ～ 60 + 70%×127 ≈ 149
    _, low, high, _ = zones[1]
    assert low == 136 and high == 149
    # 区间边界单调递增、且覆盖 Z1 下限到 Z5 上限
    lows = [z[1] for z in zones]
    highs = [z[2] for z in zones]
    assert lows == sorted(lows) and highs == sorted(highs)
    assert lows[0] < highs[4]


def test_zone_for_avg_hr():
    zones = heart_rate_zones(30, 60)
    idx, verdict = zone_for_avg_hr(zones, 140)
    assert idx == 1 and "有氧基础" in verdict
    idx, verdict = zone_for_avg_hr(zones, 120)
    assert idx == -1 and "训练刺激很小" in verdict
    idx, verdict = zone_for_avg_hr(zones, 200)
    assert idx == -1 and "超出 Z5" in verdict
    assert zone_for_avg_hr(zones, 0) == (-1, "")


def _seed_daily_km(db_session, qq, start: date, km_by_day: dict):
    for i, km in km_by_day.items():
        if km <= 0:
            continue
        db_session.add(
            DailyRecord(
                member_qq=qq,
                record_date=start + timedelta(days=i),
                platform="manual",
                distance_km=float(km),
            )
        )
    db_session.commit()


def test_load_status_steady_normal(db_session):
    today = date(2026, 10, 18)
    start = today - timedelta(days=59)
    _seed_daily_km(db_session, "111", start, {i: 5.0 for i in range(60)})
    ls = compute_load_status(db_session, "111", today=today)
    assert ls is not None
    assert abs(ls["ctl"] - 5.0) < 0.5  # 恒定 5km/天 → CTL 收敛到 5
    assert abs(ls["atl"] - 5.0) < 0.5
    assert abs(ls["tsb"]) < 1.0
    assert "正常训练" in ls["verdict"]


def test_load_status_spike_fatigued(db_session):
    today = date(2026, 10, 18)
    start = today - timedelta(days=59)
    # 前 53 天 5km，最近 7 天突然加到 20km → 急性负荷飙升 → TSB 大幅转负
    _seed_daily_km(
        db_session, "111", start, {i: 5.0 for i in range(53)} | {i: 20.0 for i in range(53, 60)}
    )
    ls = compute_load_status(db_session, "111", today=today)
    assert ls is not None
    assert ls["tsb"] < -5
    assert "疲劳" in ls["verdict"]


def test_load_status_insufficient_data(db_session):
    today = date(2026, 10, 18)
    start = today - timedelta(days=59)
    _seed_daily_km(db_session, "111", start, {0: 5.0, 1: 6.0, 2: 4.0})
    assert compute_load_status(db_session, "111", today=today) is None


def test_estimate_best_race_5k_from_checkin(db_session):
    today = date(2026, 10, 18)
    # 最近一次 5km 用了 30 分钟（6:00/km）→ 估算 5k = 1800s
    db_session.add(
        CheckinLog(
            member_qq="111",
            record_date=today - timedelta(days=3),
            distance_km=5.0,
            active_minutes=30,
        )
    )
    db_session.commit()
    est = estimate_best_race(db_session, "111", today=today)
    assert est == (5.0, 1800)


def test_estimate_best_race_picks_fastest_and_10k_fallback(db_session):
    today = date(2026, 10, 18)
    # 两条 5k 窗口候选：25min/4.5km（5:33）更快，应胜出
    db_session.add(
        CheckinLog(
            member_qq="111",
            record_date=today - timedelta(days=5),
            distance_km=5.0,
            active_minutes=30,
        )
    )
    db_session.add(
        CheckinLog(
            member_qq="111",
            record_date=today - timedelta(days=2),
            distance_km=4.5,
            active_minutes=25,
        )
    )
    db_session.commit()
    est = estimate_best_race(db_session, "111", today=today)
    assert est == (5.0, round(25 * 60 / 4.5 * 5))

    # 只有 10k 窗口候选（平台单次运动日）→ 回退 10k 估算
    db_session.add(
        DailyRecord(
            member_qq="222",
            record_date=today - timedelta(days=1),
            platform="coros",
            distance_km=10.0,
            avg_pace_sec_per_km=300.0,
            activities_count=1,
        )
    )
    db_session.commit()
    est = estimate_best_race(db_session, "222", today=today)
    assert est == (10.0, 3000)


def test_estimate_best_race_confidence_gates(db_session):
    today = date(2026, 10, 18)
    # 置信度门槛：全部应被忽略 → None
    # ① 多活动日（activities_count=2）的日均配速混合了不同强度，不可用
    db_session.add(
        DailyRecord(
            member_qq="111",
            record_date=today - timedelta(days=1),
            platform="coros",
            distance_km=6.0,
            avg_pace_sec_per_km=280.0,
            activities_count=2,
        )
    )
    # ② 距离低于 5k 窗口下限（4.5km）的短冲配速失真
    db_session.add(
        CheckinLog(
            member_qq="111",
            record_date=today - timedelta(days=2),
            distance_km=4.3,
            active_minutes=18,
        )
    )
    # ③ 超过 30 天窗口的旧成绩不代表当前状态
    db_session.add(
        CheckinLog(
            member_qq="111",
            record_date=today - timedelta(days=40),
            distance_km=5.0,
            active_minutes=20,
        )
    )
    db_session.commit()
    assert estimate_best_race(db_session, "111", today=today) is None

    # 加上一条合格的 5k 窗口单次记录 → 立即有估算
    db_session.add(
        CheckinLog(
            member_qq="111",
            record_date=today - timedelta(days=1),
            distance_km=5.0,
            active_minutes=25,
        )
    )
    db_session.commit()
    assert estimate_best_race(db_session, "111", today=today) == (5.0, 1500)


def test_estimate_best_race_ignores_out_of_window_and_dirty(db_session):
    today = date(2026, 10, 18)
    # 距离窗口外（3km）与配速不合理（1200s/km）的候选都应被忽略 → None
    db_session.add(
        CheckinLog(
            member_qq="111",
            record_date=today,
            distance_km=3.0,
            active_minutes=18,
        )
    )
    db_session.add(
        CheckinLog(
            member_qq="111",
            record_date=today - timedelta(days=1),
            distance_km=5.0,
            active_minutes=100,  # 20:00/km，超出合理配速上限
        )
    )
    db_session.commit()
    assert estimate_best_race(db_session, "111", today=today) is None


def test_compute_diagnosis_hr_zones_needs_age_and_resting(db_session):
    today = date(2026, 10, 18)
    # 无年龄 → 不计算心率区间
    db_session.add(Member(qq="111", nickname="甲", platform="", age=0))
    db_session.add(
        DailyRecord(
            member_qq="111",
            record_date=today,
            platform="manual",
            distance_km=5.0,
            resting_hr=60,
            avg_hr=140,
        )
    )
    db_session.commit()
    r = compute_diagnosis(db_session, "111")
    assert r["hr_zones"] is None

    # 设年龄 → 计算区间并定位最近平均心率所在区
    m = db_session.get(Member, "111")
    m.age = 25
    db_session.commit()
    r = compute_diagnosis(db_session, "111")
    assert r["hr_zones"] is not None
    assert len(r["hr_zones"]["zones"]) == 5
    assert r["hr_zones"]["avg_hr"] == 140


def test_format_diagnosis_includes_new_sections(db_session):
    today = date(2026, 10, 18)
    db_session.add(Member(qq="111", nickname="甲", platform="", age=25))
    for i in range(30):
        db_session.add(
            DailyRecord(
                member_qq="111",
                record_date=today - timedelta(days=i),
                platform="manual",
                distance_km=5.0,
                active_minutes=30,
                resting_hr=60,
                avg_hr=140,
                avg_pace_sec_per_km=360.0,
            )
        )
    db_session.commit()

    # 自动估算的比赛成绩：输出须明确标注「非比赛成绩」
    r = compute_diagnosis(db_session, "111", race=(5.0, 1500), race_estimated=True)
    text = format_diagnosis(r, "甲")
    assert "心率区间" in text and "Z2 有氧基础" in text
    assert "负荷状态" in text and "CTL" in text and "TSB" in text
    assert "非比赛成绩" in text

    # 未给成绩时给引导提示
    r2 = compute_diagnosis(db_session, "111")
    assert format_diagnosis(r2, "甲").count("心率区间") >= 1
