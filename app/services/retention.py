from datetime import date

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..db import get_session
from ..models.daily_record import DailyRecord

# 保留近 N 天的原始明细，供周榜（7 天）/月榜（最多 31 天）聚合，留足缓冲
RETENTION_DAYS = 45


def cleanup_daily(before: date) -> int:
    """删除 record_date < before 的原始明细，返回删除行数。"""
    session: Session = get_session()
    try:
        result = session.execute(delete(DailyRecord).where(DailyRecord.record_date < before))
        session.commit()
        return result.rowcount or 0
    finally:
        session.close()
