from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class WeeklyStat(Base):
    """每周统计汇总（永久保留）。由 daily_record 聚合而来。"""

    __tablename__ = "weekly_stat"
    __table_args__ = (
        UniqueConstraint(
            "member_qq", "week_start", "platform", name="uq_weekly_member_week_platform"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_qq: Mapped[str] = mapped_column(String(32), index=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)  # 周一
    platform: Mapped[str] = mapped_column(String(16), default="")

    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    total_distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    total_active_minutes: Mapped[int] = mapped_column(Integer, default=0)
    total_calories: Mapped[int] = mapped_column(Integer, default=0)
    avg_resting_hr: Mapped[float] = mapped_column(Float, default=0.0)
    avg_sleep_hours: Mapped[float] = mapped_column(Float, default=0.0)
    active_days: Mapped[int] = mapped_column(Integer, default=0)
    total_activities: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
