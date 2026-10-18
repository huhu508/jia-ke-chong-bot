"""帮助与菜单：/帮助 查看命令（两级菜单）、@机器人 回复菜单 / 运动问答。"""

import asyncio
import re
import time

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import to_me

from ..services import guards, llm, summary
from .analysis import build_analysis
from .history import build_range
from .query import build_today
from .ranking import build_ranking
from .summary import build_period

# 首页菜单：只给三大类入口，避免一上来刷一大屏（群里反馈「字好多」）。
# AI 能力刻意不在菜单显式展示（AI 融入各入口：分析 / 鼓励我 / 截图打卡点评 / @我问答）。
HELP_OVERVIEW = (
    "🐞 甲壳虫使用说明\n"
    "━━━━━━━━━━━━\n"
    "📊 查询 · 🔗 绑定 · 🎲 其它\n"
    "· 发「帮助 查询」看查询命令\n"
    "· 发「帮助 绑定」看绑定与设置\n"
    "· 发「帮助 其它」看抽奖 / 玩法\n"
    "有问题或想查数据，直接 @我 说一句即可。"
)

# 分类详情：按需展开，只有被点名时才显示。
_HELP_DETAILS = {
    "查询": (
        "📊 查询命令\n"
        "━━━━━━━━━━━━\n"
        "· 今日 —— 今日运动数据\n"
        "· 周数据 / 月数据 / 近30天 —— 个人周期汇总\n"
        "· 分析（可加 月 / 5k 25:00）—— 数据总结 + 训练建议 + 诊断\n"
        "· 处方 —— 下周训练安排（AI 给方案，机器人校验后渲染）\n"
        "· 鼓励我 —— 一句话鼓励\n"
        "· 日榜 / 周榜 / 月榜 —— 当日 / 本周 / 本月排行\n"
        "· 详细数据 9月 —— 查任意时间段逐日明细\n"
        "· 删除打卡 —— 撤销最近一次截图打卡"
    ),
    "绑定": (
        "🔗 绑定与设置（每人独立账号）\n"
        "━━━━━━━━━━━━\n"
        "· 绑定 coros —— 高驰：无需加好友，私聊收授权链接 → 回群发「绑定确认」\n"
        "· 绑定 garmin —— 佳明：无需加好友，私聊按提示回复「garmin绑定 邮箱 密码」\n"
        "· 其他平台 —— 无需绑定，直接发运动截图\n"
        "· 我的绑定 / 解绑\n"
        "· 昵称 xxx —— 设置自己的显示昵称（昵称 清空 恢复）\n"
        "· 年龄 25 —— 设置年龄（分析里算心率区间用，年龄 清空 清除）"
    ),
    "其它": (
        "🎲 其它\n"
        "━━━━━━━━━━━━\n"
        "· 抽奖 [N] 候选… / 骰子 / 随机数 —— 群互动\n"
        "· 管理员命令 —— 管理员查看需权限才能执行的命令"
    ),
}

# 帮助主题的关键词映射：用于「帮助 xxx」定位到具体分类。
_HELP_TOPIC_KEYS = {
    "查询": (
        "查询",
        "query",
        "今日",
        "步数",
        "周数据",
        "月数据",
        "近30天",
        "日榜",
        "周榜",
        "月榜",
        "排行",
        "分析",
        "总结",
        "建议",
        "诊断",
        "鼓励",
        "删除打卡",
        "撤销打卡",
        "详细数据",
        "历史",
        "明细",
        "训练记录",
    ),
    "绑定": (
        "绑定",
        "bind",
        "coros",
        "garmin",
        "佳明",
        "高驰",
        "解绑",
        "我的绑定",
        "昵称",
        "改名",
        "年龄",
    ),
    "其它": ("其它", "其他", "抽奖", "骰子", "随机", "管理", "机器状态", "同步", "管理员命令"),
}


def _match_help_topic(text: str) -> str | None:
    """把「帮助 xxx」的正文映射到分类 key，命中返回 key，否则 None。"""
    t = text.strip().lower()
    if not t:
        return None
    for topic, keys in _HELP_TOPIC_KEYS.items():
        if any(k in t for k in keys):
            return topic
    return None


help_cmd = on_command("帮助", aliases={"菜单", "命令", "使用说明", "help"}, priority=5, block=True)


@help_cmd.handle()
async def handle_help(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    topic = _match_help_topic(args.extract_plain_text())
    if topic:
        await help_cmd.finish(_HELP_DETAILS[topic])
    await help_cmd.finish(HELP_OVERVIEW)


# ---------------------------------------------------------------------------
# 多轮对话上下文（内存态，per-user，TTL 过期）
# 只存最近几轮纯文本问答，不落盘、不跨重启；超时即清空，避免长期占用内存。
# ---------------------------------------------------------------------------

_CHAT_HISTORY: dict[str, list[dict]] = {}
_CHAT_LAST: dict[str, float] = {}
_CHAT_TTL = 600.0  # 10 分钟无交互即清空上下文
_MAX_TURNS = 6  # 最多保留 6 条（3 轮问答）
_HISTORY_MAX_CHARS = 200  # 单条历史内容上限，防上下文污染/超长占用内存


def _purge_chat() -> None:
    """惰性清理超过 TTL 的对话上下文。

    否则只聊过一次就不再回来的成员，其条目会永久残留、随群规模无界增长。
    在「读/写」任一入口顺带清理，频率低（仅 @问答 触发），全量遍历代价可忽略。
    """
    now = time.time()
    for k in [k for k, ts in _CHAT_LAST.items() if now - ts > _CHAT_TTL]:
        _CHAT_HISTORY.pop(k, None)
        _CHAT_LAST.pop(k, None)


def _chat_history(qq: str, group_id) -> list[dict]:
    key = (group_id, qq)
    now = time.time()
    if now - _CHAT_LAST.get(key, 0.0) > _CHAT_TTL:
        _CHAT_HISTORY.pop(key, None)
        return []
    return list(_CHAT_HISTORY.get(key, []))


def _remember(qq: str, group_id, question: str, answer: str) -> None:
    _purge_chat()
    key = (group_id, qq)
    hist = _CHAT_HISTORY.setdefault(key, [])
    # 只存清洗后的提问与回答，各自截断，避免上下文污染 / 超长占用内存
    q = guards.clean_question(question, _HISTORY_MAX_CHARS)
    a = (answer or "")[:_HISTORY_MAX_CHARS]
    hist.append({"role": "user", "content": q})
    hist.append({"role": "assistant", "content": a})
    if len(hist) > _MAX_TURNS:
        del hist[: len(hist) - _MAX_TURNS]
    _CHAT_LAST[key] = time.time()


# 自然语言查数据的强信号词：命中才走 LLM 意图分类，纯闲聊/问答直接跳过，省一次调用延迟。
_QUERY_HINTS = re.compile(
    r"今日|今天|步数|距离|排行|周榜|月榜|日榜|榜|总结|鼓励|建议|诊断|分析|历史|详细数据|明细|"
    r"数据|配速|消耗|爬升|心率|睡眠|跑了|跑量|运动了|公里"
)


async def _dispatch_query(event: GroupMessageEvent, question: str, bot=None) -> str | None:
    """自然语言查数据路由：关键词预筛 → 意图分类 → 调对应 build 函数。

    命中数据查询返回结果文本；非查询意图或任何异常返回 None（由调用方降级纯问答）。
    """
    # 预筛：明显不是查数据（如「膝盖疼怎么办」）直接跳过分类，避免白等一次 LLM
    if not _QUERY_HINTS.search(question):
        return None
    intent = await asyncio.to_thread(llm.classify_intent, question)
    if not intent:
        return None
    kind = intent.get("intent")
    qq = event.get_user_id()
    nickname = getattr(event.sender, "nickname", None)

    try:
        if kind == "today":
            return await build_today(qq, nickname, bot, getattr(event, "group_id", None))
        if kind == "weekly":
            return await build_period(qq, nickname, "周", "data")
        if kind == "monthly":
            return await build_period(qq, nickname, "月", "data")
        if kind in ("summary", "advise"):
            # 「总结 / 建议 / 诊断」类自然语言统一汇入三合一的「分析」
            period = "月" if intent.get("period") == "month" else "周"
            return await build_analysis(qq, nickname, period)
        if kind == "encourage":
            period = "月" if intent.get("period") == "month" else "周"
            return await build_period(qq, nickname, period, "encourage")
        if kind == "ranking":
            scope = intent.get("scope") or "day"
            if scope not in ("day", "week", "month"):
                scope = "day"
            return await build_ranking(scope)
        if kind in ("history", "data_range"):
            # 任意时间段明细（「详细数据 9月」）；data_range 为旧意图名，兼容处理
            return await build_range(qq, nickname, intent.get("range", ""), True)
        if kind == "help":
            return HELP_OVERVIEW
    except Exception as e:
        logger.warning(f"自然语言查询路由失败（降级问答）: {e}")
        return None
    return None


# @机器人 → 无正文回首页菜单；带问题则先走自然语言查数据路由，再走运动问答（LLM）。
# 优先级最低（99），只有没被其它命令处理时才兜底。
at_me = on_message(rule=to_me(), priority=99, block=True)


@at_me.handle()
async def handle_at(bot: Bot, event: MessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return
    question = guards.clean_question(event.get_plaintext())
    if not question:
        await at_me.finish(HELP_OVERVIEW)

    # 注入拦截：本地命中「指令覆盖/越权」类注入时，不给 LLM、不记上下文，直接固定拒绝。
    if guards.looks_like_injection(question):
        await at_me.finish(
            "我是甲壳虫，这个群的运动数据机器人，只聊运动和健康生活话题、查数据、解读训练，"
            "不会扮演别的角色或透露内部设定。有运动相关的事尽管找我～"
        )

    qq = event.get_user_id()

    # 1) 自然语言查数据：命中查询意图直接给结果（如「我今天跑了多少」「这周排行」）
    text = await _dispatch_query(event, question, bot)
    if text:
        await at_me.finish(text)

    # 2) 多轮问答：带短期上下文（最近几轮）+ 本人本周数据概览（只读聚合，让回答
    # 能结合成员真实数据），回答后记住本轮（按群隔离，避免串群）。
    gid = getattr(event, "group_id", None)
    history = _chat_history(qq, gid)
    context = ""
    if _QUERY_HINTS.search(question):
        # 数据相关的提问才带上下文：既省 DB 查询，也避免闲聊被无关数字干扰
        try:
            start, end, _ = summary.parse_range("本周")
            s = await asyncio.to_thread(summary.compute_member_summary, qq, start, end)
            if s["active_days"] > 0 or s["distance_km"] > 0:
                context = llm.sport_facts_text(getattr(event.sender, "nickname", None) or qq, s)
        except Exception as e:
            logger.warning(f"构建成员数据上下文失败（不带上下文继续）: {e}")
    text = await asyncio.to_thread(llm.answer_question, question, history, context)
    if text:
        _remember(qq, gid, question, text)
        await at_me.finish(text)
    await at_me.finish("这个问题我暂时答不上来，发「帮助」看我能做什么吧")
