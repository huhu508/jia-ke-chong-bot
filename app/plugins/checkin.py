"""打卡查询：打卡 查看自己的累计/本月打卡天数。

礼物（第 GIFT_DAYS 天）不再靠手动「领礼物」，改为在截图/今日路径跨过里程碑时
自动领取并通知团长（见 services.checkin.auto_gift 与 query.py / image_ocr.py）。
"""

from datetime import timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

from ..db import get_session
from ..models.member import Member
from ..services import checkin, timeutil

checkin_cmd = on_command("打卡", aliases={"打卡天数", "我的打卡"}, priority=5, block=True)


@checkin_cmd.handle()
async def handle_checkin(event: MessageEvent):
    qq = event.get_user_id()
    name = getattr(event.sender, "nickname", None) or qq
    today = timeutil.today()
    month_start = today.replace(day=1)
    session = get_session()
    try:
        member = session.get(Member, qq)
        if member is not None:
            name = member.display_name
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
    # 下一个里程碑倒计时：把「达成后被动触发」的彩蛋，补上「达成前主动引导」
    next_m = checkin.next_milestone(total)
    if next_m is not None:
        lines.append(f"· 距下一个里程碑（{next_m} 天）还差 {next_m - total} 天")
    await checkin_cmd.finish("\n".join(lines))
