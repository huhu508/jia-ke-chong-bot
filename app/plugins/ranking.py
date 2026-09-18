from datetime import date, timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.log import logger

from ..services import ranking

ranking_cmd = on_command("排行", aliases={"运动排行", "今日排行"}, priority=5, block=True)
weekly_cmd = on_command("周榜", aliases={"本周排行"}, priority=5, block=True)
monthly_cmd = on_command("月榜", aliases={"本月排行"}, priority=5, block=True)


async def _finish_ranking(matcher, start: date, end: date, title: str) -> None:
    try:
        r = ranking.compute_range_rankings(start, end)
        text = ranking.format_leaderboards(r, title)
    except Exception as e:
        logger.exception(f"排行查询失败: {e}")
        await matcher.finish(f"排行查询失败：{e}")
    await matcher.finish(text)


@ranking_cmd.handle()
async def handle_ranking(bot: Bot, event: MessageEvent):
    today = date.today()
    await _finish_ranking(ranking_cmd, today, today + timedelta(days=1), ranking.daily_title(today))


@weekly_cmd.handle()
async def handle_weekly(bot: Bot, event: MessageEvent):
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    await _finish_ranking(
        weekly_cmd, this_monday, today + timedelta(days=1), ranking.weekly_title(this_monday, today)
    )


@monthly_cmd.handle()
async def handle_monthly(bot: Bot, event: MessageEvent):
    today = date.today()
    month_start = today.replace(day=1)
    await _finish_ranking(
        monthly_cmd, month_start, today + timedelta(days=1), ranking.monthly_title(today)
    )
