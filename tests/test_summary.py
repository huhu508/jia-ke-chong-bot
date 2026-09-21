"""个人汇总加权口径测试（配速按距离加权、心率按时长加权）。"""

from datetime import date, timedelta

from app.models.daily_record import DailyRecord
from app.services.summary import compute_daily_list, compute_member_summary, parse_range


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


def test_parse_range_近N天():
    today = date.today()
    assert parse_range("近30天") == (today - timedelta(days=29), today + timedelta(days=1), "近30天")


def test_parse_range_近N个月():
    today = date.today()
    start, end, label = parse_range("最近3个月")
    y, mo = today.year, today.month - 3
    while mo <= 0:
        mo += 12
        y -= 1
    assert start == date(y, mo, 1)
    assert end == today + timedelta(days=1)
    assert label == "近3个月"


def test_parse_range_指定年月():
    assert parse_range("2026年8月") == (date(2026, 8, 1), date(2026, 9, 1), "2026年8月")
    assert parse_range("8月")[0].month == 8  # 无年份，month 恒为 8


def test_parse_range_未来月视为去年():
    today = date.today()
    if today.month < 12:
        start, end, label = parse_range(f"{today.month + 1}月")
        assert start.year == today.year - 1
        assert start.month == today.month + 1


def test_parse_range_快捷词():
    today = date.today()
    assert parse_range("本月")[0] == date(today.year, today.month, 1)


def test_parse_range_年粒度已删除():
    assert parse_range("今年") is None
    assert parse_range("去年") is None
    assert parse_range("2026年") is None
    assert parse_range("2026") is None


def test_parse_range_无效():
    assert parse_range("") is None
    assert parse_range("   ") is None
    assert parse_range("随便乱写") is None


def test_compute_daily_list_aggregates(db_session):
    qq = "111"
    d1 = date(2026, 9, 1)
    d2 = date(2026, 9, 2)
    db_session.add_all(
        [
            DailyRecord(
                member_qq=qq, record_date=d1, platform="manual",
                distance_km=5.0, active_minutes=30, activities_count=1,
                avg_pace_sec_per_km=300.0,
            ),
            DailyRecord(
                member_qq=qq, record_date=d2, platform="manual",
                distance_km=8.0, active_minutes=50, activities_count=1,
            ),
            DailyRecord(
                member_qq=qq, record_date=d2, platform="garmin",
                distance_km=2.0, active_minutes=10, activities_count=1,
            ),
        ]
    )
    db_session.commit()

    days = compute_daily_list(qq, date(2026, 9, 1), date(2026, 9, 3), session=db_session)
    assert len(days) == 2  # 9月2日两条聚合为一天
    assert days[0]["date"] == d1 and days[0]["distance_km"] == 5.0
    assert days[1]["date"] == d2
    assert days[1]["distance_km"] == 10.0  # 8 + 2
    assert days[1]["active_minutes"] == 60
    assert days[1]["activities_count"] == 2
