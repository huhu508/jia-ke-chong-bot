from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CheckinLog(Base):
    """截图打卡明细：记录**每一次**截图打卡的距离，供「删除最近一次打卡」精确回退。

    未绑定平台的成员靠截图打卡，距离累加进 manual_distance + daily_record(manual)，
    本身不留单次明细；本表补上这一粒度，撤销时按最近一条扣回对应距离。
    已绑定成员不写本表（数据来自平台接口，非截图打卡）。
    """

    __tablename__ = "checkin_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_qq: Mapped[str] = mapped_column(String(32), index=True)
    record_date: Mapped[date] = mapped_column(Date, index=True)
    distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
