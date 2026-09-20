from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Member(Base):
    """群成员 -> 运动平台账号的绑定关系。"""

    __tablename__ = "member"

    qq: Mapped[str] = mapped_column(String(32), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(64), default="")
    # 绑定的运动平台：garmin / coros（其余平台无开放接口，靠截图，不进此字段）
    platform: Mapped[str] = mapped_column(String(16), default="")
    # 平台账号标识（凭据按 QQ 存 data/accounts/<platform>_<qq>.json，Fernet 加密落盘；此字段留空备用）
    platform_account: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
