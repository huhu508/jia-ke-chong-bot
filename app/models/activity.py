from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Activity(Base):
    """活动记录，按 kind 分两类：

    - manual：管理员手动添加的一次性活动，机器人自动做两件事：
      1. 每月末把下月「跑团日历」图发到群里（有活动的日期画红圈、格内标注活动名）；
      2. 活动前一天 23:00 在群里提醒「明天有活动」+ 时间/地点。
    - cancel：删除某天「固定活动」（每周三例训 / 周五例跑）的标记，那天不再显示固定活动。
    """

    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), default="manual")  # manual / cancel
    name: Mapped[str] = mapped_column(String(64))  # 活动名（可重复）
    date: Mapped[date] = mapped_column(Date, index=True)  # 活动日期（具体哪天）
    time: Mapped[str] = mapped_column(String(16), default="")  # 当天时刻（如 16:00）
    location: Mapped[str] = mapped_column(String(128), default="")  # 地点
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
