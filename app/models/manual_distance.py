from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ManualDistance(Base):
    """未绑定平台的成员通过截图手动记录时累加的累计里程。

    只保留「距离和」，不保留每次的明细数据（不记日期、不记配速/心率等）。
    除总累计 total_distance_km 外，另存「本周累计」week_distance_km（配合 week_start
    记录对应周一），供周排行直接取用——即使 daily_record 原始明细被 retention 清理后，
    未绑定成员的周跑量仍可参与周榜。
    """

    __tablename__ = "manual_distance"

    member_qq: Mapped[str] = mapped_column(String(32), primary_key=True)
    total_distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    week_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # 当前累计对应的周一
    week_distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
