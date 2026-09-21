from datetime import date, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models.daily_record import DailyRecord
from . import timeutil


def cleanup_daily(before: date | None = None) -> int:
    """删除 record_date < before 的原始明细，返回删除行数。

    默认按 settings.retention_days（=365）滚动保留最近一年，超过一年的明细在每日
    播报前清理时删除。retention_days <= 0 表示永久保留、不清理（如需停用可改 .env）。
    before 省略时按 retention_days 自动计算截止日期（以北京时间为准）。
    """
    if settings.retention_days <= 0:
        return 0
    if before is None:
        before = timeutil.today() - timedelta(days=settings.retention_days)
    session: Session = get_session()
    try:
        result = session.execute(delete(DailyRecord).where(DailyRecord.record_date < before))
        session.commit()
        return result.rowcount or 0
    finally:
        session.close()
