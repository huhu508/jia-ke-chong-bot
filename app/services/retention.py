from datetime import date, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models.daily_record import DailyRecord


def cleanup_daily(before: date | None = None) -> int:
    """删除 record_date < before 的原始明细，返回删除行数。

    settings.retention_days <= 0 表示永久保留、不清理，直接返回 0（保留原 45 天
    清理逻辑的接口，便于将来想收紧时只改 .env 一处）。
    before 省略时按 retention_days 自动计算截止日期。
    """
    if settings.retention_days <= 0:
        return 0
    if before is None:
        before = date.today() - timedelta(days=settings.retention_days)
    session: Session = get_session()
    try:
        result = session.execute(delete(DailyRecord).where(DailyRecord.record_date < before))
        session.commit()
        return result.rowcount or 0
    finally:
        session.close()
