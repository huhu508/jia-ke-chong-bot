"""历史数据查询：发「数据 [时间段]」看汇总、「历史 [时间段]」看逐日明细。

与「周数据 / 月数据」互补：那两个只查当前周/月，这里查任意历史区间。
时间段支持：近30天 / 近3个月 / 8月 / 2026年8月 / 今年 / 去年 / 上月 / 本周 / 本月。
"""

import asyncio
from datetime import date, timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.exception import ActionFailed, FinishedException
from nonebot.log import logger
from nonebot.params import CommandArg

from ..db import get_session
from ..models.member import Member
from ..services import summary, sync
from ..services.cheers import format_pace
from .summary import _format_summary

data_cmd = on_command("数据", aliases={"查数据", "我的数据"}, priority=5, block=True)
history_cmd = on_command("历史", aliases={"明细", "逐日", "训练记录"}, priority=5, block=True)

_USAGE = (
    "📊 用法：\n"
    "· 数据 8月 —— 查某月汇总\n"
    "· 数据 近30天 / 今年 —— 查时间段汇总\n"
    "· 历史 8月 —— 查某月逐日明细\n"
    "支持：近N天 / 近N个月 / X月 / 2026年X月 / 今年 / 去年 / 上月 / 本周 / 本月"
)


def _span(start: date, end: date) -> str:
    last = end - timedelta(days=1)
    return f"{start.month}月{start.day}日~{last.month}月{last.day}日"


def _member_info(session, event) -> tuple[str, str, str]:
    """返回 (qq, display_name, platform)。"""
    qq = event.get_user_id()
    name = getattr(event.sender, "nickname", None) or qq
    member = session.get(Member, qq)
    platform = ""
    if member is not None:
        name = member.display_name
        platform = member.platform
    return qq, name, platform


async def _maybe_sync_today(qq: str, platform: str, start: date, end: date) -> None:
    """区间覆盖今天且已绑定平台时，先补拉今天，保证当天数据新鲜；失败不阻断。"""
    if platform and start <= date.today() < end:
        try:
            await asyncio.to_thread(sync.sync_daily, qq, platform, date.today())
        except Exception as e:
            logger.warning(f"历史查询同步今日失败（用已有数据）: {e}")


def _format_daily_list(name: str, label: str, days: list[dict]) -> str:
    if not days:
        return f"{name} {label}暂无运动记录"
    lines = [f"{name} {label}逐日明细（运动 {len(days)} 天）", "━━━━━━━━━━━━"]
    for d in days:
        parts = [f"{d['date'].month:02d}-{d['date'].day:02d}"]
        if d["distance_km"]:
            parts.append(f"📏{d['distance_km']}km")
        if d["avg_pace_sec_per_km"]:
            parts.append(f"🏃{format_pace(d['avg_pace_sec_per_km'])}/km")
        if d["active_minutes"]:
            parts.append(f"⏱{d['active_minutes']}min")
        if d["ascent_meters"]:
            parts.append(f"⛰️{d['ascent_meters']:.0f}m")
        lines.append("  ".join(parts))
    return "\n".join(lines)


@data_cmd.handle()
async def handle_data(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    rng = summary.parse_range(args.extract_plain_text())
    if rng is None:
        await data_cmd.finish(_USAGE)
    start, end, label = rng

    session = get_session()
    try:
        qq, name, platform = _member_info(session, event)
    finally:
        session.close()

    try:
        await _maybe_sync_today(qq, platform, start, end)
        s = await asyncio.to_thread(summary.compute_member_summary, qq, start, end)
        if s["active_days"] == 0 and s["distance_km"] <= 0:
            await data_cmd.finish(f"{name} {label}暂无运动记录")
        await data_cmd.finish(_format_summary(name, label, _span(start, end), s))
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"数据查询失败: {e}")
        await data_cmd.finish(f"数据查询失败：{e}")


@history_cmd.handle()
async def handle_history(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    rng = summary.parse_range(args.extract_plain_text())
    if rng is None:
        await history_cmd.finish(_USAGE)
    start, end, label = rng

    session = get_session()
    try:
        qq, name, platform = _member_info(session, event)
    finally:
        session.close()

    try:
        await _maybe_sync_today(qq, platform, start, end)
        days = await asyncio.to_thread(summary.compute_daily_list, qq, start, end)
        await history_cmd.finish(_format_daily_list(name, label, days))
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"历史查询失败: {e}")
        await history_cmd.finish(f"历史查询失败：{e}")
