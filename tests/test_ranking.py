"""排行聚合测试：日榜/周榜（含未绑定成员「本周累计」参与周榜）。"""

from datetime import date

from app.models.daily_record import DailyRecord
from app.models.manual_distance import ManualDistance
from app.models.member import Member
from app.services.ranking import compute_range_rankings, format_leaderboards, today_distance_rank


def _seed(db_session):
    db_session.add(Member(qq="111", nickname="甲", platform="garmin"))
    db_session.add(Member(qq="222", nickname="乙", platform=""))
    db_session.add(
        DailyRecord(
            member_qq="111",
            record_date=date(2026, 9, 20),
            platform="garmin",
            distance_km=10.0,
            ascent_meters=100.0,
            max_activity_distance_km=8.0,
        )
    )
    db_session.add(
        DailyRecord(
            member_qq="222",
            record_date=date(2026, 9, 20),
            platform="manual",
            distance_km=5.0,
            ascent_meters=50.0,
            max_activity_distance_km=5.0,
        )
    )
    # 未绑定成员的本周累计（周榜距离数据源）
    db_session.add(
        ManualDistance(
            member_qq="222",
            total_distance_km=30.0,
            week_start=date(2026, 9, 14),
            week_distance_km=20.0,
        )
    )
    db_session.commit()


def test_day_rankings(db_session):
    _seed(db_session)
    r = compute_range_rankings(
        date(2026, 9, 20), date(2026, 9, 21), session=db_session, scope="day"
    )
    dist = dict(r["distance"])
    assert dist["甲"] == 10.0
    assert dist["乙"] == 5.0  # 日榜按明细聚合
    assert "max_single" not in r  # 单次运动距离排行已删除，榜单只出距离/爬升/运动天数


def test_week_rankings_uses_manual_week_distance(db_session):
    _seed(db_session)
    r = compute_range_rankings(
        date(2026, 9, 14), date(2026, 9, 21), session=db_session, scope="week"
    )
    dist = dict(r["distance"])
    assert dist["甲"] == 10.0
    # 未绑定成员乙：周榜距离改用 week_distance_km（20），而非明细（5）
    assert dist["乙"] == 20.0


def test_week_rankings_ignore_stale_manual_distance(db_session):
    # 未绑定成员乙：本周没有新截图，week_start 仍是上周一（2026-09-07），
    # week_distance_km 是上周旧值，不应计入本周榜（否则上周跑量被重复算进本周）。
    db_session.add(Member(qq="222", nickname="乙", platform=""))
    db_session.add(
        ManualDistance(
            member_qq="222",
            total_distance_km=30.0,
            week_start=date(2026, 9, 7),  # 上周一
            week_distance_km=20.0,  # 上周的旧累计，本周未重置
        )
    )
    db_session.commit()

    r = compute_range_rankings(
        date(2026, 9, 14), date(2026, 9, 21), session=db_session, scope="week"
    )
    # 乙本周无任何记录，上周的 20 km 不得进入本周榜
    assert dict(r["distance"]) == {}


def test_today_distance_rank(db_session):
    db_session.add(Member(qq="111", nickname="甲", platform="garmin"))
    db_session.add(Member(qq="222", nickname="乙", platform=""))
    db_session.add(Member(qq="333", nickname="丙", platform=""))
    d = date(2026, 9, 20)
    db_session.add(DailyRecord(member_qq="111", record_date=d, platform="garmin", distance_km=10.0))
    db_session.add(DailyRecord(member_qq="222", record_date=d, platform="manual", distance_km=5.0))
    db_session.add(DailyRecord(member_qq="333", record_date=d, platform="manual", distance_km=15.0))
    db_session.commit()
    assert today_distance_rank("333", db_session, d=d) == 1
    assert today_distance_rank("111", db_session, d=d) == 2
    assert today_distance_rank("222", db_session, d=d) == 3
    # 当日无距离记录 → 不参与名次
    assert today_distance_rank("999", db_session, d=d) is None


def test_format_leaderboards_no_single_distance_section():
    # 单次运动距离排行已删除：即使调用方仍传入 max_single 键（防御未来回归），
    # 渲染结果也不应出现该段；距离/爬升两段保留，月榜运动天数排行正常追加。
    r = {
        "distance": [("甲", 10.0), ("乙", 5.0)],
        "ascent": [("甲", 100.0)],
        "max_single": [("甲", 8.0)],
        "active_days": [("甲", 10)],
    }
    text = format_leaderboards(r, "🏆 测试排行（9月20日）")
    assert "🚀" not in text
    assert "单次运动距离排行" not in text
    assert "📏 运动距离排行" in text
    assert "⛰️ 爬升排行" in text
    assert "📅 运动天数排行" not in text  # with_active_days 默认 False

    text_month = format_leaderboards(r, "🏆 测试排行（9月）", with_active_days=True)
    assert "📅 运动天数排行" in text_month
    # 爬升段与天数排行之间恰好一个空行（删除单次距离段后排版不塌）
    assert "\n\n⛰️ 爬升排行" in text_month
    assert "爬升排行\n🥇 甲  100 m\n\n📅 运动天数排行" in text_month
