"""群自动发现：机器人出现在哪些群，就记录哪些群，供每日排行播报定位目标群。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.log import logger

from ..db import get_session
from ..models.group import Group

group_tracker = on_message(priority=20, block=False)


@group_tracker.handle()
async def handle_group_message(event: GroupMessageEvent):
    group_id = str(event.group_id)
    session = get_session()
    try:
        g = session.get(Group, group_id)
        if g is None:
            g = Group(group_id=group_id)
            session.add(g)
            session.commit()
            logger.info(f"发现新群 {group_id}，已记录用于每日排行播报")
    finally:
        session.close()
