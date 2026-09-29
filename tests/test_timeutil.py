"""timeutil：统一北京时区（UTC+8），任何部署环境按北京时间算「今天」。"""

from datetime import date, timedelta

from app.services import timeutil


def test_china_tz_is_utc_plus_8():
    assert timeutil.CHINA_TZ.utcoffset(None) == timedelta(hours=8)


def test_now_has_beijing_tzinfo():
    n = timeutil.now()
    assert n.tzinfo is timeutil.CHINA_TZ
    assert n.utcoffset() == timedelta(hours=8)


def test_today_returns_date():
    assert isinstance(timeutil.today(), date)
