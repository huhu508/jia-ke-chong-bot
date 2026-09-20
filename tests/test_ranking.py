"""排行聚合测试：日榜/周榜（含未绑定成员「本周累计」参与周榜）。"""

from datetime import date

from app.models.daily_record import DailyRecord
from app.models.manual_distance import ManualDistance
from app.models.member import Member
from app.services.ranking import compute_range_rankings


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
