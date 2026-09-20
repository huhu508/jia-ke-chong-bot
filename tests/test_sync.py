"""截图里程累加 + 跨周清零 + 单次运动累加（DailyStats 版）测试。"""

import datetime
from datetime import date

from sqlalchemy import select

from app.models.daily_record import DailyRecord
from app.models.manual_distance import ManualDistance
from app.models.member import Member
from app.services.providers.base import DailyStats
from app.services.sync import add_manual_distance, record_manual_activity


def test_add_manual_distance_accumulates(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()

    assert add_manual_distance(m, 5.0, db_session) == 5.0
    assert add_manual_distance(m, 3.0, db_session) == 8.0

    rec = db_session.get(ManualDistance, "111")
    assert rec.total_distance_km == 8.0
    assert rec.week_distance_km == 8.0


def test_add_manual_distance_resets_across_week(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()
    add_manual_distance(m, 5.0, db_session)

    # 伪装成上一周的累计，触发跨周清零
    rec = db_session.get(ManualDistance, "111")
    rec.week_start = rec.week_start - datetime.timedelta(days=7)
    db_session.commit()

    assert add_manual_distance(m, 2.0, db_session) == 7.0  # 总累计 5 + 2
    rec = db_session.get(ManualDistance, "111")
    assert rec.week_distance_km == 2.0  # 本周从 2.0 重计


def test_record_manual_activity_accumulates(db_session):
    m = Member(qq="111", nickname="甲")
    db_session.add(m)
    db_session.commit()

    d = date(2026, 9, 20)
    record_manual_activity(
        m,
        d,
        DailyStats(
            date=d,
            distance_km=5.0,
            ascent_meters=50.0,
            calories=400,
            active_minutes=30,
            avg_pace_sec_per_km=300.0,
            avg_hr=150,
        ),
        db_session,
    )
    record_manual_activity(
        m,
        d,
        DailyStats(date=d, distance_km=3.0, ascent_meters=20.0, calories=200, active_minutes=20),
        db_session,
    )

    rec = db_session.execute(
        select(DailyRecord).where(DailyRecord.member_qq == "111")
    ).scalar_one()
    assert rec.distance_km == 8.0
    assert rec.ascent_meters == 70.0
    assert rec.calories == 600
    assert rec.active_minutes == 50
    assert rec.activities_count == 2
    assert rec.max_activity_distance_km == 5.0  # max(5.0, 3.0)
    assert rec.avg_pace_sec_per_km == 300.0  # 第二次无配速，保持第一次
    assert rec.avg_hr == 150
