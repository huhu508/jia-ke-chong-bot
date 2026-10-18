"""诊断科学计算测试：心率区间（Karvonen）、负荷状态（CTL/ATL/TSB）、强度分布、诊断信号。"""

from datetime import date, timedelta

from app.models.daily_record import DailyRecord
from app.models.member import Member
from app.services.diagnose import (
    compute_diagnosis,
    compute_load_status,
    daily_trimp,
    diagnosis_signals,
    format_diagnosis,
    heart_rate_zones,
    hr_max_from_age,
    intensity_distribution,
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

    # 带比赛成绩：给出 VO₂max/成绩预测；强度分布（近7天全 Z2）判定极化结构良好
    r = compute_diagnosis(db_session, "111", race=(5.0, 1500))
    text = format_diagnosis(r, "甲")
    assert "心率区间" in text and "Z2 有氧基础" in text
    assert "负荷状态" in text and "CTL" in text and "TSB" in text
    assert "强度分布" in text and "极化" in text
    assert "VO₂max" in text and "成绩预测" in text

    # 裸分析（无成绩）：不含成绩预测，结尾引导补充 5k 最佳成绩
    r2 = compute_diagnosis(db_session, "111")
    text2 = format_diagnosis(r2, "甲")
    assert "补充 5k" in text2
    assert "🎯 有氧能力" not in text2


def test_daily_trimp():
    # TRIMP 随强度单调上升；ΔHR ≤ 0（平均心率不高于静息）时为 0
    t1 = daily_trimp(30, 130, 60, 190)
    t2 = daily_trimp(30, 150, 60, 190)
    assert t2 > t1 > 0
    assert daily_trimp(30, 60, 60, 190) == 0.0
    assert daily_trimp(30, 140, 60, 60) == 0.0  # hr_max <= resting → 0


def test_load_status_trimp_basis(db_session):
    today = date(2026, 10, 18)
    start = today - timedelta(days=59)
    db_session.add(Member(qq="111", nickname="甲", platform="coros", age=30))
    for i in range(60):
        db_session.add(
            DailyRecord(
                member_qq="111",
                record_date=start + timedelta(days=i),
                platform="coros",
                distance_km=8.0,
                active_minutes=40,
                avg_hr=150,
                resting_hr=60,
            )
        )
    db_session.commit()
    # 全部活跃日都有 HR 数据 + 年龄 → TRIMP 基线；恒定负荷 → CTL≈ATL、TSB≈0
    ls = compute_load_status(db_session, "111", today=today)
    assert ls is not None and ls["basis"] == "trimp"
    assert ls["ctl"] > 0 and abs(ls["ratio"]) < 0.1


def test_load_status_km_basis_without_hr(db_session):
    today = date(2026, 10, 18)
    start = today - timedelta(days=29)
    db_session.add(Member(qq="111", nickname="甲", platform="", age=30))
    for i in range(30):
        db_session.add(
            DailyRecord(
                member_qq="111",
                record_date=start + timedelta(days=i),
                platform="manual",
                distance_km=5.0,
            )
        )
    db_session.commit()
    # 有年龄但无 HR 数据 → 退回跑量基线
    ls = compute_load_status(db_session, "111", today=today)
    assert ls is not None and ls["basis"] == "km"


def test_intensity_distribution_gray_zone():
    today = date(2026, 10, 18)

    # 年龄 30 静息 60：Z3 = 151~164，Z2 = 136~149
    def _rec(hr, i):
        return DailyRecord(
            member_qq="111",
            record_date=today - timedelta(days=i),
            platform="manual",
            distance_km=5.0,
            avg_hr=hr,
        )

    # 4 天全在 Z3 → 灰色地带判定
    records = [_rec(155, i) for i in range(4)]
    d = intensity_distribution(records, 30, 60)
    assert d is not None and "中等强度" in d["verdict"]

    # 5 天 Z2 + 1 天 Z4 → 接近极化模型
    records = [_rec(140, i) for i in range(5)] + [_rec(170, 6)]
    d = intensity_distribution(records, 30, 60)
    assert d is not None and "极化" in d["verdict"]

    # 有效天数不足 3 → None
    assert intensity_distribution(records[:2], 30, 60) is None


def test_diagnosis_signals_nonempty():
    result = {
        "acwr": 1.1,
        "acwr_verdict": ("合理", "负荷平稳"),
        "load_status": {"basis": "km", "verdict": "正常训练状态，保持当前节奏"},
        "intensity": {"summary": "Z2 ×5、Z4 ×1", "verdict": "接近极化模型"},
        "recovery": {"sleep_avg": 7.2, "resting_hr_delta": 1},
        "pace_trend": {"delta": -12},
    }
    text = diagnosis_signals(result)
    assert "ACWR" in text and "负荷状态" in text and "强度分布" in text
    assert "睡眠" in text and "配速趋势" in text


def test_diagnosis_signals_empty_ok():
    assert diagnosis_signals({}) == ""
