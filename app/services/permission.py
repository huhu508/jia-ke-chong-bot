"""权限判断：统一「是否超级管理员」的口径，供需要管理员权限的命令复用。"""

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import MessageEvent


def is_superuser(event: MessageEvent) -> bool:
    """判断消息发送者是否为超级管理员（.env 的 SUPERUSERS）。"""
    return event.get_user_id() in get_driver().config.superusers
