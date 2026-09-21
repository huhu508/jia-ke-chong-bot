"""北京时间工具：统一从北京时区取「今天」和「当前时间」。

NUC 部署机系统时区是 UTC，若直接 `date.today()` / `datetime.now()`，会把「今天」
错判成 UTC 日期（北京 0~8 点之间差一天），且排行播报时间错位（UTC 23:00 = 北京 07:00）。
业务里所有「今天 / 当前时刻」一律从这里取，保证任何部署环境都按北京时间算。
中国无夏令时，固定 UTC+8 即可，无需 tzdata。
"""
from datetime import date, datetime, timedelta, timezone

CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now() -> datetime:
    """当前北京时间（带 +08:00 时区信息）。"""
    return datetime.now(CHINA_TZ)


def today() -> date:
    """今天（北京日期）。"""
    return now().date()
