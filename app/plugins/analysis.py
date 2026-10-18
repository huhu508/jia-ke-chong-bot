"""分析命令：AI 总结 + 训练建议 + 确定性诊断 三合一（原「总结 / 建议 / 诊断」合并）。

用法：
  分析            —— 本周：数据摘要 + AI 总结 + 训练建议 + 负荷诊断
  分析 月         —— 按本月数据
  分析 5k 25:00   —— 附加「最近一次比赛成绩」，解锁 VO₂max / 成绩预测 / 训练配速

旧命令「总结 / 建议 / 诊断」作为别名保留，全部汇入本命令；
「鼓励我」仍为独立命令（一句话鼓励，不在此列）。
"""

import asyncio

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.exception import ActionFailed, FinishedException
from nonebot.log import logger
from nonebot.params import CommandArg

from ..db import get_session
from ..services import diagnose, llm
from .summary import _format_summary, _gather_qq

analyze_cmd = on_command(
    "分析",
    aliases={
        "总结",
        "AI总结",
        "运动总结",
        "建议",
        "训练建议",
        "运动建议",
        "怎么练",
        "诊断",
        "能力诊断",
        "跑步诊断",
    },
    priority=5,
    block=True,
)

_RACE_USAGE = (
    "比赛成绩写法：分析 5k 25:00 / 分析 半马 1:45:00 / 分析 10k 50分\n"
    "（距离支持 5k / 10k / 半马 / 全马 等）"
)


def _template_summary(name: str, period: str, s: dict) -> str:
    """LLM 不可用时的确定性兜底总结。"""
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


def _template_advise(name: str, period: str, s: dict) -> str:
    """LLM 不可用时的确定性兜底建议（按周/月跑量分档）。"""
    km = s["distance_km"]
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


def _no_data_hint() -> str:
    return (
        "还没有可分析的数据：\n"
        "① 绑定平台：发「绑定 garmin」或「绑定 coros」，同步几天后即可分析\n"
        "② 其他平台：多发几张运动截图也能积累跑量/配速数据\n"
        "③ 或直接发「分析 5k 25:00」用一次比赛成绩做有氧能力评估"
    )


async def build_analysis(qq: str, nickname: str, kind: str, race_text: str = "") -> str:
    """构建「分析」结果文本（命令 handler 与自然语言路由共用）。

    kind: ``周``/``月``；race_text 为可选比赛成绩原文（如「5k 25:00」）。
    三合一输出：数据摘要 → AI 总结（失败降级模板）→ 训练建议（失败降级模板）→
    确定性诊断（无数据但有成绩时单独出诊断）。
    """
    name, period, span, s, bound = await _gather_qq(qq, nickname, kind)

    # 比赛成绩解析：失败给用法提示
    race = None
    if race_text:
        race = diagnose.parse_race(race_text)
        if race is None:
            return f"没看懂比赛成绩「{race_text}」\n\n{_RACE_USAGE}"

    session = get_session()
    try:
        result = diagnose.compute_diagnosis(session, qq, race)
    finally:
        session.close()

    # 完全没数据：无成绩 → 引导积累数据；有成绩 → 只出诊断部分
    if s["active_days"] == 0 and s["distance_km"] <= 0:
        if race is None:
            return _no_data_hint()
        return (
            f"🤖 {name} {period}分析（{span}）\n"
            "━━━━━━━━━━━━\n"
            f"🩺 诊断\n{diagnose.format_diagnosis(result, name)}"
        )

    # 数据摘要（复用周数据/月数据的展示口径）
    stats = _format_summary(name, period, span, s)
    lines = [f"🤖 {name} {period}分析（{span}）", "━━━━━━━━━━━━"]
    lines.append("📊 数据")
    lines.extend(stats.splitlines()[1:])

    # AI 总结与训练建议：并行请求，失败各自降级模板
    ai_text, advise_text = await asyncio.gather(
        asyncio.to_thread(llm.summarize_sport, name, period, s),
        asyncio.to_thread(llm.advise, name, period, s),
    )
    lines.append("\n📝 总结")
    lines.append(ai_text or _template_summary(name, period, s))
    lines.append("\n💪 建议")
    lines.append(advise_text or _template_advise(name, period, s))

    # 确定性诊断（数据可支撑时始终给出；带比赛成绩则解锁 VO₂max/成绩预测）
    if result["records"] > 0 or race is not None:
        lines.append("\n🩺 诊断")
        lines.append(diagnose.format_diagnosis(result, name))

    return "\n".join(lines)


@analyze_cmd.handle()
async def handle_analyze(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    # 可选参数：分析 月 → 本月；分析 5k 25:00 → 附加比赛成绩（两者可同时给）
    arg_text = args.extract_plain_text().strip()
    kind = "月" if "月" in arg_text else "周"
    race_text = arg_text.replace("月", " ").strip()
    try:
        text = await build_analysis(
            event.get_user_id(), getattr(event.sender, "nickname", None), kind, race_text
        )
        await analyze_cmd.finish(text)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"分析生成失败: {e}")
        await analyze_cmd.finish(f"分析生成失败：{e}")
