"""个人汇总加权口径测试（配速按距离加权、心率按时长加权）。"""

from datetime import date

from app.models.daily_record import DailyRecord
from app.services.summary import compute_member_summary


def test_summary_weighted(db_session):
    db_session.add(
        DailyRecord(
            member_qq="111",
            record_date=date(2026, 9, 19),
            platform="garmin",
            distance_km=10.0,
            active_minutes=60,
            calories=500,
            activities_count=1,
            avg_pace_sec_per_km=300.0,
            avg_hr=150,
        )
    )
    db_session.add(
        DailyRecord(
            member_qq="111",
            record_date=date(2026, 9, 20),
            platform="garmin",
            distance_km=5.0,
            active_minutes=30,
            calories=300,
            activities_count=2,
            avg_pace_sec_per_km=360.0,
            avg_hr=160,
        )
    )
    db_session.commit()

    s = compute_member_summary(
        "111", date(2026, 9, 19), date(2026, 9, 21), session=db_session
    )

    assert s["active_days"] == 2
    assert s["activities"] == 3
    assert s["distance_km"] == 15.0
    assert s["active_minutes"] == 90
    assert s["calories"] == 800
    # 配速按距离加权：(300*10 + 360*5) / 15 = 320
    assert s["avg_pace_sec_per_km"] == 320.0
    # 心率按时长加权：(150*60 + 160*30) / 90 = 153.33 → 153
    assert s["avg_hr"] == 153
