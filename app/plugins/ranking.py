import asyncio
from datetime import timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.log import logger

from ..services import ranking, sync, timeutil

ranking_cmd = on_command("排行", aliases={"运动排行", "今日排行"}, priority=5, block=True)
weekly_cmd = on_command("周榜", aliases={"本周排行"}, priority=5, block=True)
monthly_cmd = on_command("月榜", aliases={"本月排行"}, priority=5, block=True)


async def _refresh_today() -> None:
    """榜单计算前同步所有已绑定成员的今日数据，保证当天数据新鲜（失败不阻断）。"""
    try:
        await asyncio.to_thread(sync.sync_today_all)
    except Exception as e:
        logger.warning(f"同步今日数据失败（仍用已有数据）: {e}")


async def build_ranking(scope: str = "day") -> str:
    """构建榜单文本（命令 handler 与自然语言路由共用）。scope: day/week/month。"""
    await _refresh_today()
    today = timeutil.today()
    if scope == "week":
        start = today - timedelta(days=today.weekday())
        r = await asyncio.to_thread(ranking.compute_range_rankings, start, today + timedelta(days=1), scope="week")
        return ranking.format_leaderboards(r, ranking.weekly_title(start, today))
    if scope == "month":
        start = today.replace(day=1)
        r = await asyncio.to_thread(ranking.compute_range_rankings, start, today + timedelta(days=1), scope="month")
        return ranking.format_leaderboards(r, ranking.monthly_title(today))
    r = await asyncio.to_thread(ranking.compute_range_rankings, today, today + timedelta(days=1), scope="day")
    return ranking.format_leaderboards(r, ranking.daily_title(today))


@ranking_cmd.handle()
async def handle_ranking(bot: Bot, event: MessageEvent):
    try:
        text = await build_ranking("day")
        await ranking_cmd.finish(text)
    except Exception as e:
        logger.exception(f"排行查询失败: {e}")
        await ranking_cmd.finish(f"排行查询失败：{e}")


@weekly_cmd.handle()
async def handle_weekly(bot: Bot, event: MessageEvent):
    try:
        text = await build_ranking("week")
        await weekly_cmd.finish(text)
    except Exception as e:
        logger.exception(f"周榜查询失败: {e}")
        await weekly_cmd.finish(f"周榜查询失败：{e}")


@monthly_cmd.handle()
async def handle_monthly(bot: Bot, event: MessageEvent):
    try:
        text = await build_ranking("month")
        await monthly_cmd.finish(text)
    except Exception as e:
        logger.exception(f"月榜查询失败: {e}")
        await monthly_cmd.finish(f"月榜查询失败：{e}")
