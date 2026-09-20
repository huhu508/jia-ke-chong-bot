from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DailyRecord(Base):
    """每日运动数据快照（原始明细）。保留近 RETENTION_DAYS（45）天供周榜/月榜聚合，过期由 retention 清理。"""

    __tablename__ = "daily_record"
    __table_args__ = (
        UniqueConstraint(
            "member_qq", "record_date", "platform", name="uq_daily_member_date_platform"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_qq: Mapped[str] = mapped_column(String(32), index=True)
    record_date: Mapped[date] = mapped_column(Date, index=True)
    platform: Mapped[str] = mapped_column(String(16), default="")

    steps: Mapped[int] = mapped_column(Integer, default=0)
    distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    active_minutes: Mapped[int] = mapped_column(Integer, default=0)
    calories: Mapped[int] = mapped_column(Integer, default=0)
    resting_hr: Mapped[int] = mapped_column(Integer, default=0)
    sleep_hours: Mapped[float] = mapped_column(Float, default=0.0)
    activities_count: Mapped[int] = mapped_column(Integer, default=0)

    ascent_meters: Mapped[float] = mapped_column(Float, default=0.0)
    training_load: Mapped[float] = mapped_column(Float, default=0.0)
    avg_pace_sec_per_km: Mapped[float] = mapped_column(Float, default=0.0)
    avg_hr: Mapped[int] = mapped_column(Integer, default=0)
    max_activity_distance_km: Mapped[float] = mapped_column(Float, default=0.0)

    raw_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
