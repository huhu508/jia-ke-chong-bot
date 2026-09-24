from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CheckinDay(Base):
    """打卡日：每个成员「有运动数据的一天」记一行，永不清理。

    独立于 daily_record（后者受 365 天 retention 清理），是打卡天数 / 里程碑 /
    群抽奖的唯一长期数据源。覆盖绑定平台（自动同步）与未绑定（截图）两类成员，
    同一天多平台/多次只算一天，靠 (member_qq, record_date) 唯一约束去重。
    """

    __tablename__ = "checkin_day"
    __table_args__ = (UniqueConstraint("member_qq", "record_date", name="uq_checkin_member_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_qq: Mapped[str] = mapped_column(String(32), index=True)
    record_date: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
