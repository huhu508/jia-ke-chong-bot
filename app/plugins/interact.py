"""帮助与菜单：/帮助 查看命令（两级菜单）、@机器人 回复菜单 / 运动问答。"""

import asyncio
import re
import time

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import to_me

from ..services import llm
from .history import build_range
from .query import build_today
from .ranking import build_ranking
from .summary import build_period

# 首页菜单：只给四大类入口，避免一上来刷一大屏（群里反馈「字好多」）。
HELP_OVERVIEW = (
    "🐞 甲壳虫使用说明\n"
    "━━━━━━━━━━━━\n"
    "📊 查询 · 🔗 绑定 · 🤖 AI 助手 · 🎲 其它\n"
    "· 发「帮助 查询」看查询命令\n"
    "· 发「帮助 绑定」看绑定流程\n"
    "· 发「帮助 ai」看 AI 玩法\n"
    "· 发「帮助 其它」看抽奖 / 管理\n"
    "有问题或想查数据，直接 @我 说一句即可。"
)

# 分类详情：按需展开，只有被点名时才显示。
_HELP_DETAILS = {
    "查询": (
        "📊 查询命令\n"
        "━━━━━━━━━━━━\n"
        "· 今日 / 步数 —— 今日运动数据\n"
        "· 周数据 / 月数据 —— 个人周 / 月汇总\n"
        "· 总结（可加 月）—— AI 把数据说成人话\n"
        "· 鼓励我 / 建议（可加 月）—— AI 鼓励 / 训练建议\n"
        "· 诊断（可加 5k 25:00）—— 负荷 / 恢复 / 配速诊断\n"
        "· 排行 / 周榜 / 月榜 —— 今日 / 本周 / 本月三榜\n"
        "· 数据 9月 / 数据 近30天 —— 查任意时间段汇总\n"
        "· 历史 9月 —— 查逐日明细\n"
        "· 删除打卡 —— 撤销最近一次截图打卡"
    ),
    "绑定": (
        "🔗 绑定（每人独立账号）\n"
        "━━━━━━━━━━━━\n"
        "· 绑定 coros —— 高驰：无需加好友，私聊收授权链接 → 回群发「绑定确认」\n"
        "· 绑定 garmin —— 佳明：无需加好友，私聊按提示回复「garmin绑定 邮箱 密码」\n"
        "· 其他平台 —— 无需绑定，直接发运动截图\n"
        "· 我的绑定 / 解绑"
    ),
    "ai": (
        "🤖 AI 助手\n"
        "━━━━━━━━━━━━\n"
        "· @我 + 问题 —— 运动 / 伤病 / 疲劳 / 天气等问答\n"
        "· @我 + 一句话 —— 也能查数据（今天跑了多少 / 这周排行）\n"
        "· 截图打卡 —— 记录成功后 AI 补一句点评"
    ),
    "其它": (
        "🎲 其它\n"
        "━━━━━━━━━━━━\n"
        "· 抽奖 [N] 候选… / 骰子 / 随机数 —— 群互动\n"
        "· 昵称 xxx —— 设置自己的显示昵称（昵称 清空 恢复）\n"
        "· 机器状态 / 同步数据 —— 管理员"
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
        "排行",
        "周榜",
        "月榜",
        "总结",
        "建议",
        "诊断",
        "鼓励",
        "删除打卡",
        "撤销打卡",
        "数据",
        "历史",
        "明细",
        "训练记录",
    ),
    "绑定": ("绑定", "bind", "coros", "garmin", "佳明", "高驰", "解绑", "我的绑定"),
    "ai": ("ai", "助手", "问答", "问题", "聊天", "人工智能"),
    "其它": ("其它", "其他", "抽奖", "骰子", "随机", "管理", "机器状态", "同步", "昵称", "改名"),
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


def _chat_history(qq: str, group_id) -> list[dict]:
    key = (group_id, qq)
    now = time.time()
    if now - _CHAT_LAST.get(key, 0.0) > _CHAT_TTL:
        _CHAT_HISTORY.pop(key, None)
        return []
    return list(_CHAT_HISTORY.get(key, []))


def _remember(qq: str, group_id, question: str, answer: str) -> None:
    key = (group_id, qq)
    hist = _CHAT_HISTORY.setdefault(key, [])
    hist.append({"role": "user", "content": question})
    hist.append({"role": "assistant", "content": answer})
    if len(hist) > _MAX_TURNS:
        del hist[: len(hist) - _MAX_TURNS]
    _CHAT_LAST[key] = time.time()


# 自然语言查数据的强信号词：命中才走 LLM 意图分类，纯闲聊/问答直接跳过，省一次调用延迟。
_QUERY_HINTS = re.compile(
    r"今日|今天|步数|距离|排行|周榜|月榜|榜|总结|鼓励|建议|诊断|历史|数据|配速|消耗|爬升|心率|睡眠|跑了|跑量|运动了|公里"
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
        if kind == "summary":
            period = "月" if intent.get("period") == "month" else "周"
            return await build_period(qq, nickname, period, "ai")
        if kind == "advise":
            period = "月" if intent.get("period") == "month" else "周"
            return await build_period(qq, nickname, period, "advise")
        if kind == "encourage":
            period = "月" if intent.get("period") == "month" else "周"
            return await build_period(qq, nickname, period, "encourage")
        if kind == "ranking":
            scope = intent.get("scope") or "day"
            if scope not in ("day", "week", "month"):
                scope = "day"
            return await build_ranking(scope)
        if kind == "data_range":
            return await build_range(qq, nickname, intent.get("range", ""), False)
        if kind == "history":
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
    question = event.get_plaintext().strip()
    if not question:
        await at_me.finish(HELP_OVERVIEW)

    qq = event.get_user_id()

    # 1) 自然语言查数据：命中查询意图直接给结果（如「我今天跑了多少」「这周排行」）
    text = await _dispatch_query(event, question, bot)
    if text:
        await at_me.finish(text)

    # 2) 多轮问答：带短期上下文（最近几轮），回答后记住本轮（按群隔离，避免串群）
    gid = getattr(event, "group_id", None)
    history = _chat_history(qq, gid)
    text = await asyncio.to_thread(llm.answer_question, question, history)
    if text:
        _remember(qq, gid, question, text)
        await at_me.finish(text)
    await at_me.finish("这个问题我暂时答不上来，发「帮助」看我能做什么吧")
