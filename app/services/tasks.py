"""定时任务：每日排行播报 + 过期明细清理。

从 app/scheduler.py 归位到服务层。原「周聚合」任务与周汇总中间表已一并删除——
周榜/月榜直接由 daily_record 聚合，无需中间表。
过期明细清理从「每周一」改为「每日播报前」执行，幂等、更及时。
"""

import asyncio
import datetime

from nonebot import get_driver
from nonebot.log import logger

from ..config import settings
from ..db import get_session
from ..models.member import Member
from . import activity, checkin, ranking, retention, sync, timeutil

driver = get_driver()


def _lottery_if_due() -> tuple[int, list[str]] | None:
    """全群累计打卡突破抽奖阈值时开奖，返回 (阈值, 中奖者昵称列表)；否则 None。

    状态表（lottery:<阈值>）保证只触发一次。阻塞 DB 调用，放线程执行。
    """
    session = get_session()
    try:
        t = checkin.check_lottery(session)
        if t is None:
            return None
        winners = checkin.draw_lottery(session)
        names = []
        for w in winners:
            m = session.get(Member, w)
            names.append(m.display_name if m else w)
        session.commit()
        return t, names
    finally:
        session.close()


async def _run_broadcast() -> None:
    """计算并播报排行：每日今日榜；周日追加周榜、月末追加月榜，逐段独立发送。"""
    today = timeutil.today()
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

    # 解析播报目标：bots 按 self_id 去重、群按白名单过滤（见 activity.resolve_targets）
    bots, groups = await activity.resolve_targets()
    if not bots:
        logger.warning("没有已连接的 OneBot Bot，跳过排行播报")
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

    # 群抽奖兜底：全群累计打卡突破阈值时开奖，公告到所有播报群（状态表保证只触发一次）
    try:
        lot = await asyncio.to_thread(_lottery_if_due)
    except Exception as e:
        logger.warning(f"群抽奖检查失败: {e}")
        lot = None
    if lot:
        t, winners = lot
        text = (
            f"🎉 全群累计打卡突破 {t} 天！群抽奖开奖：{'、'.join(winners)} "
            f"获得{checkin.LOTTERY_PRIZE}～恭喜这位跑友！"
        )
        for bot in bots:
            for gid in groups:
                try:
                    await bot.send_group_msg(group_id=gid, message=text)
                    logger.info(f"已向群 {gid} 发送群抽奖公告")
                except Exception as e:
                    logger.warning(f"向群 {gid} 发送群抽奖公告失败: {e}")

    # 活动提醒：明天有活动则提醒（搭 23:00 榜单播报时刻，幂等保证只提醒一次）
    try:
        await activity.remind_tomorrow(today)
    except Exception as e:
        logger.warning(f"活动提醒失败: {e}")

    # 下月跑团日历：今天是本月最后一天 → 生成并发送下月日历图（同月只发一次）
    if (today + day).month != today.month:
        next_first = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        try:
            await activity.send_next_month_calendar(next_first)
        except Exception as e:
            logger.warning(f"发送下月跑团日历失败: {e}")


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
        now = timeutil.now()
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
