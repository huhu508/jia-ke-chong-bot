"""打卡查询：打卡 查看自己的累计/本月打卡天数。

礼物（第 GIFT_DAYS 天）不再靠手动「领礼物」，改为在截图/今日路径跨过里程碑时
自动领取并通知团长（见 services.checkin.auto_gift 与 query.py / image_ocr.py）。
"""

from datetime import timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

from ..db import get_session
from ..models.member import Member
from ..services import checkin, cheers, timeutil

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
        streak = checkin.current_streak(qq, session)
        longest = checkin.longest_streak(qq, session)
    finally:
        session.close()

    lines = [
        f"🎓 {name} 的打卡记录",
        "━━━━━━━━━━━━",
        f"· 累计打卡 {total} 天",
        f"· 本月打卡 {month} 天",
    ]
    # 连续打卡：运动 App 最核心的坚持钩子，先报当前连击，再报历史纪录
    if streak:
        note = cheers.streak_note(streak)
        line = f"· 🔥 连续打卡 {streak} 天"
        if note:
            line += f"（{note}）"
        if longest and streak == longest:
            line += "，追平历史最长！"
        lines.append(line)
    if longest and streak != longest:
        lines.append(f"· 历史最长连续 {longest} 天")
    # 里程碑倒计时：把「达成后被动触发」的彩蛋补上「达成前主动引导」，加进度条可视化
    mp = checkin.milestone_progress(total)
    if mp is not None:
        remain, pct = mp
        next_m = checkin.next_milestone(total)
        lines.append(f"· 距里程碑 {next_m} 天还差 {remain} 天")
        lines.append(f"  {checkin.progress_bar(pct)}")
    await checkin_cmd.finish("\n".join(lines))
