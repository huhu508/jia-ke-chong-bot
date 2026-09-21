"""定时任务：每日排行播报 + 过期明细清理。

从 app/scheduler.py 归位到服务层。原「周聚合」任务与周汇总中间表已一并删除——
周榜/月榜直接由 daily_record 聚合，无需中间表。
过期明细清理从「每周一」改为「每日播报前」执行，幂等、更及时。
"""

import asyncio
import datetime

from nonebot import get_bots, get_driver
from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger
from sqlalchemy import select

from ..config import settings
from ..db import get_session
from ..models.group import Group
from . import ranking, retention, sync

driver = get_driver()


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

    # 播报前清理过期明细（retention_days<=0 时永久保留、自动跳过），幂等、每日执行
    try:
        await asyncio.to_thread(retention.cleanup_daily)
    except Exception as e:
        logger.warning(f"清理过期明细失败（不影响播报）: {e}")

    # 播报前先同步所有已绑定成员的今日数据，否则绑定平台后榜单可能因当天未入库而为空
    try:
        await asyncio.to_thread(sync.sync_today_all)
    except Exception as e:
        logger.warning(f"同步今日数据失败（仍用已有数据播报）: {e}")

    # (scope, 标签, 起始, 结束) 三段榜单，按需追加
    periods: list[tuple[str, str, datetime.date, datetime.date]] = [
        ("day", "今日", today, today + day)
    ]
    if today.weekday() == 6:  # 周日
        this_monday = today - datetime.timedelta(days=6)
        periods.append(("week", "本周", this_monday, today + day))
    if (today + day).month != today.month:  # 月末（明天进入下月）
        month_start = today.replace(day=1)
        periods.append(("month", "本月", month_start, today + day))

    messages: list[str] = []
    for scope, label, start, end in periods:
        try:
            r = await asyncio.to_thread(ranking.compute_range_rankings, start, end, scope=scope)
            messages.append(ranking.format_leaderboards(r, _rank_title(label, start, end)))
        except Exception as e:
            logger.exception(f"{label}排行计算失败: {e}")
    if not messages:
        return

    # 按 self_id 去重，避免 NapCat 重复反向 WS 连接导致同一 bot 播报两遍
    bots: list[Bot] = []
    seen_ids: set[str] = set()
    for b in get_bots().values():
        if isinstance(b, Bot) and b.self_id not in seen_ids:
            seen_ids.add(b.self_id)
            bots.append(b)
    if not bots:
        logger.warning("没有已连接的 OneBot Bot，跳过排行播报")
        return

    # 去重，避免 .env 手写重复群号导致同群播报两遍
    groups = list(dict.fromkeys(settings.broadcast_groups))
    if not groups:
        try:
            groups = await asyncio.to_thread(_discover_groups)
        except Exception as e:
            logger.exception(f"发现播报群失败: {e}")
            return
        groups = list(dict.fromkeys(groups))
    if not groups:
        logger.warning("没有可播报的群，跳过排行播报")
        return

    # 群白名单：仅在 allowed_groups 内的群播报（未配置则不限制）
    if settings.allowed_groups:
        allowed = {int(g) for g in settings.allowed_groups}
        groups = [g for g in groups if g in allowed]
        if not groups:
            logger.warning("没有白名单内的可播报群，跳过排行播报")
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


_started = False


def start_scheduler() -> None:
    """在 driver on_startup 阶段调用，启动每日排行播报循环。

    幂等：若 on_startup 被重复触发，也只创建一次循环任务，避免每天 23:00 重复播报。
    """
    global _started
    if _started:
        logger.warning("调度器已启动，跳过重复启动")
        return
    _started = True
    asyncio.get_running_loop().create_task(_daily_broadcast_loop())
    logger.info(
        f"每日排行播报已启动（每天 {settings.broadcast_hour:02d}:{settings.broadcast_minute:02d}）"
    )
