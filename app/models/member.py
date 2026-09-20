from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Member(Base):
    """群成员 -> 运动平台账号的绑定关系。"""

    __tablename__ = "member"

    qq: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 自动同步的 QQ 昵称（每次发言更新），作为自定义昵称未设置时的兜底显示
    nickname: Mapped[str] = mapped_column(String(64), default="")
    # 用户手动设置的自定义昵称（/昵称 xxx），优先级高于 QQ 昵称，与 QQ 号唯一绑定
    custom_nickname: Mapped[str] = mapped_column(String(64), default="")
    # 绑定的运动平台：garmin / coros（其余平台无开放接口，靠截图，不进此字段）
    platform: Mapped[str] = mapped_column(String(16), default="")
    # 平台账号标识（凭据按 QQ 存 data/accounts/<platform>_<qq>.json，Fernet 加密落盘；此字段留空备用）
    platform_account: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def display_name(self) -> str:
        """显示用昵称：自定义昵称 > QQ 昵称 > QQ 号。"""
        return self.custom_nickname or self.nickname or self.qq
