import asyncio
import datetime

from nonebot import get_bots, get_driver
from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger
from sqlalchemy import select

from .config import settings
from .db import get_session
from .models.group import Group
from .services import aggregator, ranking, retention

driver = get_driver()


async def _run_weekly() -> None:
    today = datetime.date.today()
    this_monday = today - datetime.timedelta(days=today.weekday())
    last_monday = this_monday - datetime.timedelta(days=7)
    try:
        n_agg = await asyncio.to_thread(aggregator.aggregate_week, last_monday)
        n_del = await asyncio.to_thread(
            retention.cleanup_daily, today - datetime.timedelta(days=retention.RETENTION_DAYS)
        )
        logger.info(
            f"周聚合完成：聚合 {n_agg} 组，清理 {n_del} 条原始明细"
            f"（保留近 {retention.RETENTION_DAYS} 天供周榜/月榜）"
        )
    except Exception as e:
        logger.exception(f"周聚合任务失败: {e}")


async def _weekly_loop() -> None:
    hour = settings.weekly_hour
    minute = settings.weekly_minute
    while True:
        now = datetime.datetime.now()
        # 计算下一个周一的 hour:minute
        days_ahead = (7 - now.weekday()) % 7
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if days_ahead == 0 and now >= target:
            days_ahead = 7
        next_run = (now + datetime.timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        await asyncio.sleep((next_run - now).total_seconds())
        await _run_weekly()


def _discover_groups() -> list[int]:
    """返回已记录的群号列表（机器人出现过的群）。阻塞 DB 调用，放线程执行。"""
    session = get_session()
    try:
        return [int(g.group_id) for g in session.execute(select(Group)).scalars().all()]
    finally:
        session.close()


async def _run_broadcast() -> None:
    """计算并播报排行：每日今日榜；周日追加周榜、月末追加月榜，逐段独立发送。"""
    today = datetime.date.today()
    day = datetime.timedelta(days=1)

    # (标签, 起始, 结束) 三段榜单，按需追加
    periods: list[tuple[str, datetime.date, datetime.date]] = [("今日", today, today + day)]
    if today.weekday() == 6:  # 周日
        this_monday = today - datetime.timedelta(days=6)
        periods.append(("本周", this_monday, today + day))
    if (today + day).month != today.month:  # 月末（明天进入下月）
        month_start = today.replace(day=1)
        periods.append(("本月", month_start, today + day))

    messages: list[str] = []
    for label, start, end in periods:
        try:
            r = await asyncio.to_thread(ranking.compute_range_rankings, start, end)
            messages.append(ranking.format_leaderboards(r, _rank_title(label, start, end)))
        except Exception as e:
            logger.exception(f"{label}排行计算失败: {e}")
    if not messages:
        return

    bots = [b for b in get_bots().values() if isinstance(b, Bot)]
    if not bots:
        logger.warning("没有已连接的 OneBot Bot，跳过排行播报")
        return

    groups = list(settings.broadcast_groups)
    if not groups:
        try:
            groups = await asyncio.to_thread(_discover_groups)
        except Exception as e:
            logger.exception(f"发现播报群失败: {e}")
            return
    if not groups:
        logger.warning("没有可播报的群，跳过排行播报")
        return

    for bot in bots:
        for gid in groups:
            for text in messages:
                try:
                    await bot.send_group_msg(group_id=gid, message=text)
                    logger.info(f"已向群 {gid} 播报一段排行")
                except Exception as e:
                    logger.warning(f"向群 {gid} 播报失败: {e}")


def _rank_title(label: str, start: datetime.date, end: datetime.date) -> str:
    if label == "今日":
        return ranking.daily_title(start)
    if label == "本周":
        last = end - datetime.timedelta(days=1)
        return ranking.weekly_title(start, last)
    return ranking.monthly_title(start)


async def _daily_broadcast_loop() -> None:
    hour = settings.broadcast_hour
    minute = settings.broadcast_minute
    while True:
        now = datetime.datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await _run_broadcast()


def start_scheduler() -> None:
    """在 driver on_startup 阶段调用，启动后台周聚合 + 每日排行播报循环。"""
    asyncio.get_running_loop().create_task(_weekly_loop())
    asyncio.get_running_loop().create_task(_daily_broadcast_loop())
    logger.info(
        f"周聚合调度已启动（每周一 {settings.weekly_hour:02d}:{settings.weekly_minute:02d}）"
    )
    logger.info(
        f"每日排行播报已启动（每天 {settings.broadcast_hour:02d}:{settings.broadcast_minute:02d}）"
    )
