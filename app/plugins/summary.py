"""个人周期数据：发「周数据」「月数据」查看自己的运动次数/跑量/爬升/负荷等；「总结」用大模型做自然语言解读。

设计要点：
  - 「周数据」「月数据」走确定性聚合（不依赖 AI），随时可用；
  - 「总结」是 LLM 增强层：配置了大模型 key 时用大模型润色，否则自动降级到模板文案，
    二者共用同一份汇总数字，保证口径一致。
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
from ..services import llm, summary, sync, timeutil
from ..services.cheers import format_pace

weekly_cmd = on_command("周数据", aliases={"本周数据", "我的周数据"}, priority=5, block=True)
monthly_cmd = on_command("月数据", aliases={"本月数据", "我的月数据"}, priority=5, block=True)
ai_cmd = on_command("总结", aliases={"AI总结", "运动总结"}, priority=5, block=True)
encourage_cmd = on_command("鼓励我", aliases={"点评", "夸夸我"}, priority=5, block=True)
advise_cmd = on_command("建议", aliases={"训练建议", "运动建议", "怎么练"}, priority=5, block=True)


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

    mode: ``data``（周/月数据）/ ``ai``（总结）/ ``advise``（建议）/ ``encourage``（鼓励）。
    """
    name, period, span, s, bound = await _gather_qq(qq, nickname, kind)

    # 鼓励：无数据也照常鼓励（沿用原行为，不拦）
    if mode == "encourage":
        text = await asyncio.to_thread(llm.encourage, name, s)
        if text:
            return f"🤖 {name}，{text}"
        return _template_cheer(name, s)

    # 与 周数据/月数据/建议 口径一致：无数据先给友好提示，避免把全 0 数据喂给 LLM 编造
    if s["active_days"] == 0 and s["distance_km"] <= 0:
        return _empty_hint(name, period, span, bound)

    if mode == "data":
        return _format_summary(name, period, span, s)

    if mode == "ai":
        text = await asyncio.to_thread(llm.summarize_sport, name, period, s)
        if text:
            return f"🤖 {name} {period}总结\n━━━━━━━━━━━━\n{text}"
        return f"🤖 {name} {period}总结\n━━━━━━━━━━━━\n{_template_summary(name, period, s)}"

    # advise
    text = await asyncio.to_thread(llm.advise, name, period, s)
    if text:
        return f"🤖 {name} {period}训练建议\n━━━━━━━━━━━━\n{text}"
    return f"🤖 {name} {period}训练建议\n━━━━━━━━━━━━\n{_template_advise(name, period, s)}"


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
    if s["max_activity_distance_km"]:
        lines.append(f"🏆 单次最长：{s['max_activity_distance_km']} km")
    return f"{name} {period}运动数据：\n" + "\n".join(lines)


def _empty_hint(name: str, period: str, span: str, bound: bool) -> str:
    if bound:
        return f"{name} {period}（{span}）暂无运动数据。发「今日」同步当天，或让管理员「同步数据」补齐历史"
    return (
        f"{name} {period}（{span}）暂无运动数据。\n"
        "试试：① 发「绑定 coros / garmin」接入平台；② 其他 App 直接发运动截图，我会自动记录"
    )


def _template_summary(name: str, period: str, s: dict) -> str:
    """LLM 不可用时的确定性兜底总结。"""
    if s["distance_km"] <= 0:
        return f"{name} {period}还没有运动记录，动起来再来看总结吧 💪"
    parts = [f"{name} {period}运动了 {s['active_days']} 天，累计 {s['distance_km']} km"]
    if s["activities"]:
        parts.append(f"共 {s['activities']} 次运动")
    if s["ascent_meters"]:
        parts.append(f"爬升 {s['ascent_meters']:.0f} m")
    if s["training_load"]:
        parts.append(f"负荷 {s['training_load']:.0f}")
    tail = (
        "继续保持，身体会记住每一分努力 💪"
        if s["distance_km"] >= 10
        else "动起来就是胜利，慢慢加量 🐢"
    )
    return "，".join(parts) + "。" + tail


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


def _template_advise(name: str, period: str, s: dict) -> str:
    """LLM 不可用时的确定性兜底建议（按周/月跑量分档）。"""
    km = s["distance_km"]
    if km <= 0:
        return f"{name} {period}还没有运动记录，先去跑一次，我才能给你建议哦 💪"
    parts = [f"{name} {period}跑了 {km} km（运动 {s['active_days']} 天）"]
    if km >= 40:
        parts.append("训练量已经不小，注意安排轻松日和充分恢复，避免过度训练")
    elif km >= 15:
        parts.append("量保持得不错，可以每周穿插一次间歇或爬坡来提强度")
    else:
        parts.append("建议稳步加量，每周增幅控制在 10% 以内，先规律再进阶")
    if s.get("training_load") and s["training_load"] >= 600:
        parts.append("负荷偏高，留意睡眠与疲劳感，必要时减量")
    return "，".join(parts) + "。"


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


@ai_cmd.handle()
async def handle_ai(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    # 可选参数：总结 月 → 总结本月；默认总结本周
    kind = "月" if "月" in args.extract_plain_text() else "周"
    try:
        text = await build_period(
            event.get_user_id(), getattr(event.sender, "nickname", None), kind, "ai"
        )
        await ai_cmd.finish(text)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"总结生成失败: {e}")
        await ai_cmd.finish(f"总结生成失败：{e}")


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


@advise_cmd.handle()
async def handle_advise(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    # 可选参数：建议 月 → 按本月数据；默认按本周
    kind = "月" if "月" in args.extract_plain_text() else "周"
    try:
        text = await build_period(
            event.get_user_id(), getattr(event.sender, "nickname", None), kind, "advise"
        )
        await advise_cmd.finish(text)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"训练建议生成失败: {e}")
        await advise_cmd.finish(f"训练建议生成失败：{e}")
