"""历史数据查询：发「数据 [时间段]」看汇总、「历史 [时间段]」看逐日明细。

与「周数据 / 月数据」互补：那两个只查当前周/月，这里查任意历史区间。
时间段支持：近30天 / 近3个月 / 9月 / 2026年9月 / 上月 / 本周 / 本月。
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
from ..services import summary, sync, timeutil
from ..services.cheers import format_pace
from .summary import _format_summary

data_cmd = on_command("数据", aliases={"查数据", "我的数据"}, priority=5, block=True)
history_cmd = on_command("历史", aliases={"明细", "逐日", "训练记录"}, priority=5, block=True)

_USAGE = (
    "📊 用法：\n"
    "· 数据 9月 —— 查某月汇总\n"
    "· 数据 近30天 / 近3个月 —— 查时间段汇总\n"
    "· 历史 9月 —— 查某月逐日明细\n"
    "支持：近N天 / 近N个月 / X月 / 2026年X月 / 上月 / 本周 / 本月"
)


def _span(start: date, end: date) -> str:
    last = end - timedelta(days=1)
    return f"{start.month}月{start.day}日~{last.month}月{last.day}日"


async def build_range(qq: str, nickname: str, range_text: str, is_daily: bool) -> str:
    """构建历史区间查询文本（命令 handler 与自然语言路由共用）。

    is_daily=True 返回逐日明细，否则返回区间汇总。
    """
    rng = summary.parse_range(range_text)
    if rng is None:
        return _USAGE
    start, end, label = rng

    session = get_session()
    try:
        member = session.get(Member, qq)
        platform = ""
        if member is not None:
            nickname = member.display_name
            platform = member.platform
    finally:
        session.close()

    await _maybe_sync_today(qq, platform, start, end)

    if is_daily:
        days = await asyncio.to_thread(summary.compute_daily_list, qq, start, end)
        return _format_daily_list(nickname, label, days)

    s = await asyncio.to_thread(summary.compute_member_summary, qq, start, end)
    if s["active_days"] == 0 and s["distance_km"] <= 0:
        return f"{nickname} {label}暂无运动记录"
    return _format_summary(nickname, label, _span(start, end), s)


async def _maybe_sync_today(qq: str, platform: str, start: date, end: date) -> None:
    """区间覆盖今天且已绑定平台时，先补拉今天，保证当天数据新鲜；失败不阻断。"""
    if platform and start <= timeutil.today() < end:
        try:
            await asyncio.to_thread(sync.sync_daily, qq, platform, timeutil.today())
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
    try:
        text = await build_range(
            event.get_user_id(),
            getattr(event.sender, "nickname", None),
            args.extract_plain_text(),
            False,
        )
        await data_cmd.finish(text)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"数据查询失败: {e}")
        await data_cmd.finish(f"数据查询失败：{e}")


@history_cmd.handle()
async def handle_history(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    try:
        text = await build_range(
            event.get_user_id(),
            getattr(event.sender, "nickname", None),
            args.extract_plain_text(),
            True,
        )
        await history_cmd.finish(text)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"历史查询失败: {e}")
        await history_cmd.finish(f"历史查询失败：{e}")
