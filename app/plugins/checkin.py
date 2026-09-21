"""打卡查询与里程碑礼物：打卡 看打卡天数、领礼物 领取第 100 天礼物（先到先得）。"""

from datetime import timedelta

from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.log import logger

from ..db import get_session
from ..services import checkin, timeutil
from .admin import _send_private_robust

checkin_cmd = on_command("打卡", aliases={"打卡天数", "我的打卡"}, priority=5, block=True)
gift_cmd = on_command("领礼物", aliases={"兑换礼物", "小红书礼物", "领奖"}, priority=5, block=True)


@checkin_cmd.handle()
async def handle_checkin(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    name = getattr(event.sender, "nickname", None) or qq
    today = timeutil.today()
    month_start = today.replace(day=1)
    session = get_session()
    try:
        total = checkin.total_days(qq, session)
        month = checkin.month_days(qq, month_start, today + timedelta(days=1), session)
    finally:
        session.close()

    lines = [
        f"🎓 {name} 的打卡记录",
        "━━━━━━━━━━━━",
        f"· 累计打卡 {total} 天",
        f"· 本月打卡 {month} 天",
    ]
    if total >= checkin.GIFT_DAYS:
        lines.append(f"· 已达成第 {checkin.GIFT_DAYS} 天里程碑，发「领礼物」兑换小红书惊喜小礼物")
    else:
        lines.append(f"· 距第 {checkin.GIFT_DAYS} 天惊喜礼物还差 {checkin.GIFT_DAYS - total} 天")
    await checkin_cmd.finish("\n".join(lines))


@gift_cmd.handle()
async def handle_gift(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    name = getattr(event.sender, "nickname", None) or qq
    session = get_session()
    rank = 0
    try:
        total = checkin.total_days(qq, session)
        if total < checkin.GIFT_DAYS:
            await gift_cmd.finish(
                f"🎁 第 {checkin.GIFT_DAYS} 天惊喜礼物还没解锁～"
                f"你还差 {checkin.GIFT_DAYS - total} 天打卡，加油！"
            )
        claimed = checkin.gift_claims(session)
        if claimed >= checkin.GIFT_QUOTA:
            await gift_cmd.finish(
                f"😢 第 {checkin.GIFT_DAYS} 天惊喜礼物已被领完啦（先到先得），下次早点来～"
            )
        if not checkin.claim_gift(qq, session):
            await gift_cmd.finish("你已经领过啦，别贪心～")
        session.commit()
        rank = claimed + 1
    finally:
        session.close()

    # 通知团长（superuser）安排发货
    gid = getattr(event, "group_id", None)
    for su in get_driver().config.superusers:
        try:
            await _send_private_robust(
                bot,
                int(su),
                gid,
                f"🎁 {name}（QQ {qq}）领取了第 {checkin.GIFT_DAYS} 天小红书惊喜礼物（第 {rank} 位），请安排发货～",
            )
        except Exception as e:
            logger.warning(f"通知团长礼物领取失败: {e}")

    await gift_cmd.finish(
        f"🎁 恭喜 {name}！你是第 {rank} 位领取第 {checkin.GIFT_DAYS} 天「小红书惊喜小礼物」的跑友，"
        f"已通知团长，请留意私聊安排发货～"
    )
