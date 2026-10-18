"""个人周期数据：发「周数据」「月数据」「近30天」查看自己的运动次数/跑量/爬升/负荷等。

设计要点：
  - 「周数据」「月数据」「近30天」走确定性聚合（不依赖 AI），随时可用；
  - 「分析」命令（见 analysis.py）集成 AI 总结 / 训练建议 / 确定性诊断，
    失败一律降级到模板文案；「鼓励我」是独立的一句话鼓励。
"""

import asyncio
from datetime import date, timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.exception import ActionFailed, FinishedException
from nonebot.log import logger

from ..db import get_session
from ..models.member import Member
from ..services import llm, summary, sync, timeutil
from ..services.cheers import format_pace

weekly_cmd = on_command("周数据", aliases={"本周数据", "我的周数据"}, priority=5, block=True)
monthly_cmd = on_command("月数据", aliases={"本月数据", "我的月数据"}, priority=5, block=True)
near30_cmd = on_command("近30天", aliases={"最近30天", "近一个月", "30天"}, priority=5, block=True)
encourage_cmd = on_command("鼓励我", aliases={"点评", "夸夸我"}, priority=5, block=True)


def _period_of(kind: str) -> tuple[str, date, date, str]:
    """按 kind（"周"/"月"）返回 (标签, start, end, 展示范围)。end 为不含当天。"""
    today = timeutil.today()
    if kind == "周":
        start = today - timedelta(days=today.weekday())  # 本周一
        return (
            "本周",
            start,
            today + timedelta(days=1),
            f"{start.month}月{start.day}日~{today.month}月{today.day}日",
        )
    start = today.replace(day=1)
    return (
        "本月",
        start,
        today + timedelta(days=1),
        f"{start.month}月1日~{today.month}月{today.day}日",
    )


async def _gather_qq(qq: str, nickname: str, kind: str):
    """按 qq 拉取某成员周期汇总，返回 (name, period, span, summary_dict, bound)。"""
    nickname = nickname or qq
    period, start, end, span = _period_of(kind)
    session = get_session()
    try:
        member = session.get(Member, qq)
        if member is not None:
            nickname = member.display_name
        # 提前把绑定平台读成普通值，session 关闭后不再碰 ORM 对象
        platform = member.platform if member is not None else ""
    finally:
        session.close()

    # 已绑定平台：先补拉今天，保证当天数据新鲜；失败不阻断，退回已有数据
    if platform:
        try:
            await asyncio.to_thread(sync.sync_daily, qq, platform, timeutil.today())
        except Exception as e:
            logger.warning(f"周期数据同步今日失败（用已有数据）: {e}")

    s = await asyncio.to_thread(summary.compute_member_summary, qq, start, end)
    return nickname, period, span, s, bool(platform)


async def build_period(qq: str, nickname: str, kind: str, mode: str) -> str:
    """构建周期结果文本（命令 handler 与自然语言路由共用）。

    mode: ``data``（周/月数据）/ ``encourage``（鼓励）。
    """
    name, period, span, s, bound = await _gather_qq(qq, nickname, kind)

    # 与 周数据/月数据/鼓励 口径一致：无数据先给友好提示，避免把全 0 数据喂给 LLM 编造。
    # 鼓励也纳入：无数据不喂 LLM，走确定性模板（_template_cheer 已有 km<=0 分支）。
    if s["active_days"] == 0 and s["distance_km"] <= 0:
        if mode == "encourage":
            return _template_cheer(name, s)
        return _empty_hint(name, period, span, bound)

    if mode == "encourage":
        text = await asyncio.to_thread(llm.encourage, name, s)
        if text:
            return f"🤖 {name}，{text}"
        return _template_cheer(name, s)

    # data
    return _format_summary(name, period, span, s)


async def build_near30(qq: str, nickname: str) -> str:
    """近30天汇总（「近30天」命令，自然语言路由也可复用）。"""
    nickname = nickname or qq
    start, end, label = summary.parse_range("近30天")
    session = get_session()
    try:
        member = session.get(Member, qq)
        platform = ""
        if member is not None:
            nickname = member.display_name
            platform = member.platform
    finally:
        session.close()

    # 已绑定平台：先补拉今天，保证当天数据新鲜；失败不阻断，退回已有数据
    if platform:
        try:
            await asyncio.to_thread(sync.sync_daily, qq, platform, timeutil.today())
        except Exception as e:
            logger.warning(f"近30天同步今日失败（用已有数据）: {e}")

    s = await asyncio.to_thread(summary.compute_member_summary, qq, start, end)
    if s["active_days"] == 0 and s["distance_km"] <= 0:
        return _empty_hint(nickname, "近30天", label, bool(platform))
    return _format_summary(nickname, "近30天", label, s)


def _format_summary(name: str, period: str, span: str, s: dict) -> str:
    lines = [
        f"📅 {period}（{span}）",
        f"🏷️ 运动次数：{s['activities']} 次（运动 {s['active_days']} 天）",
    ]
    if s["distance_km"]:
        lines.append(f"📏 跑量/距离：{s['distance_km']} km")
    if s["ascent_meters"]:
        lines.append(f"⛰️ 爬升：{s['ascent_meters']:.0f} m")
    if s["active_minutes"]:
        lines.append(f"⏱ 总时长：{s['active_minutes']} 分钟")
    if s["calories"]:
        lines.append(f"🔥 总消耗：{s['calories']} 千卡")
    if s["training_load"]:
        lines.append(f"⚡ 运动负荷：{s['training_load']:.0f}")
    if s["avg_pace_sec_per_km"]:
        lines.append(f"🏃 平均配速：{format_pace(s['avg_pace_sec_per_km'])} /km")
    if s["avg_hr"]:
        lines.append(f"💓 平均心率：{s['avg_hr']} bpm")
    return f"{name} {period}运动数据：\n" + "\n".join(lines)


def _empty_hint(name: str, period: str, span: str, bound: bool) -> str:
    if bound:
        return (
            f"{name} {period}（{span}）暂无运动数据，还没开张～"
            "发「今日」同步当天，或让管理员「同步数据」补齐历史"
        )
    return (
        f"{name} {period}（{span}）暂无运动数据，动起来就有啦 💪\n"
        "试试：① 发「绑定 coros / garmin」接入平台；② 其他 App 直接发运动截图，我会自动记录"
    )


def _template_cheer(name: str, s: dict) -> str:
    """LLM 不可用时的确定性兜底鼓励（按跑量分档）。"""
    km = s["distance_km"]
    if km <= 0:
        return f"{name} 最近还没怎么动，去跑两步吧，我会给你记着 💪"
    if km >= 30:
        return f"{name} 本周 {km} km，这训练量已经很能打了，记得给身体留点恢复空间 💪"
    if km >= 10:
        return f"{name} 本周 {km} km，稳定输出就是最好的状态，继续保持 🔥"
    return f"{name} 本周 {km} km，动起来就是胜利，慢慢加量别急 🐢"


@weekly_cmd.handle()
async def handle_weekly(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    nickname = getattr(event.sender, "nickname", None)
    try:
        text = await build_period(qq, nickname, "周", "data")
        await weekly_cmd.finish(text)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"周数据查询失败: {e}")
        await weekly_cmd.finish(f"周数据查询失败：{e}")


@monthly_cmd.handle()
async def handle_monthly(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    nickname = getattr(event.sender, "nickname", None)
    try:
        text = await build_period(qq, nickname, "月", "data")
        await monthly_cmd.finish(text)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"月数据查询失败: {e}")
        await monthly_cmd.finish(f"月数据查询失败：{e}")


@near30_cmd.handle()
async def handle_near30(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    nickname = getattr(event.sender, "nickname", None)
    try:
        text = await build_near30(qq, nickname)
        await near30_cmd.finish(text)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"近30天查询失败: {e}")
        await near30_cmd.finish(f"近30天查询失败：{e}")


@encourage_cmd.handle()
async def handle_encourage(bot: Bot, event: MessageEvent):
    try:
        text = await build_period(
            event.get_user_id(), getattr(event.sender, "nickname", None), "周", "encourage"
        )
        await encourage_cmd.finish(text)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"鼓励生成失败: {e}")
        await encourage_cmd.finish(f"鼓励生成失败：{e}")
