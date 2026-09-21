from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CheckinState(Base):
    """打卡触发状态：记录已触发过的里程碑 / 礼物 / 抽奖，保证幂等、不重复发彩蛋。

    key 形如：
      - milestone:<天数>:<qq>    —— 某成员累计打卡达某里程碑已触发
      - gift:<天数>:<qq>         —— 某成员已领取某天数的礼物
      - lottery:<阈值>           —— 全群累计突破某阈值已触发群抽奖
      - festival:<节日名>:<年份>:<qq> —— 某成员在某年某节日已触发特殊距离彩蛋
    """

    __tablename__ = "checkin_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
