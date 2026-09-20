"""群使用白名单：非白名单群的消息一律不响应。

在事件分发前用 event_preprocessor 拦截，覆盖所有 matcher（命令 / 截图识别 /
@问答 / 群自动发现 / 抽奖等），避免逐个插件加规则、也避免漏网。私聊不受限制
（佳明绑定、COROS 授权链接需私聊，管理员私聊也照常可用）。
"""

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.message import event_preprocessor

from ..config import settings


@event_preprocessor
async def gate_group(event):
    """拦截非白名单群的消息，任何 matcher 都不会收到（含群自动发现）。"""
    if not isinstance(event, GroupMessageEvent):
        return
    if not settings.allowed_groups:
        return  # 未配置白名单 = 不限制（向后兼容）
    if str(event.group_id) not in {str(g) for g in settings.allowed_groups}:
        event.stop_propagation()
