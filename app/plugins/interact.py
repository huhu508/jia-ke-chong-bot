"""帮助与菜单：/帮助 查看命令（两级菜单）、@机器人 回复菜单 / 运动问答。"""

import asyncio

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.rule import to_me

from ..services import llm

# 首页菜单：只给四大类入口，避免一上来刷一大屏（群里反馈「字好多」）。
HELP_OVERVIEW = (
    "🐞 甲壳虫使用说明\n"
    "━━━━━━━━━━━━\n"
    "📊 查询 · 🔗 绑定 · 🤖 AI 助手 · 🎲 其它\n"
    "· 发「帮助 查询」看查询命令\n"
    "· 发「帮助 绑定」看绑定流程\n"
    "· 发「帮助 ai」看 AI 玩法\n"
    "· 发「帮助 其它」看抽奖 / 管理\n"
    "有问题直接 @我 即可。"
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
        "· 数据 8月 / 数据 近30天 —— 查任意时间段汇总\n"
        "· 历史 8月 —— 查逐日明细\n"
        "· 删除打卡 —— 撤销最近一次截图打卡"
    ),
    "绑定": (
        "🔗 绑定（每人独立账号）\n"
        "━━━━━━━━━━━━\n"
        "· 绑定 coros —— 高驰：私聊收授权链接 → 回群发「绑定确认」\n"
        "· 绑定 garmin —— 佳明：先加好友，再私聊「garmin绑定 邮箱 密码」\n"
        "· 其他平台 —— 无需绑定，直接发运动截图\n"
        "· 我的绑定 / 解绑"
    ),
    "ai": (
        "🤖 AI 助手\n"
        "━━━━━━━━━━━━\n"
        "· @我 + 运动问题 —— 配速 / 跑量 / 恢复等问答（只聊运动）\n"
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
    "查询": ("查询", "query", "今日", "步数", "周数据", "月数据", "排行", "周榜", "月榜", "总结", "建议", "诊断", "鼓励", "删除打卡", "撤销打卡", "打卡", "数据", "历史", "明细", "训练记录"),
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


# @机器人 → 无正文回首页菜单；带问题则走运动问答（LLM，失败回落菜单/提示）。
# 优先级最低（99），只有没被其它命令处理时才兜底。
at_me = on_message(rule=to_me(), priority=99, block=True)


@at_me.handle()
async def handle_at(bot: Bot, event: MessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return
    question = event.get_plaintext().strip()
    if not question:
        await at_me.finish(HELP_OVERVIEW)
    text = await asyncio.to_thread(llm.answer_question, question)
    if text:
        await at_me.finish(text)
    await at_me.finish("这个问题我暂时答不上来（AI 未接入或出错），发「帮助」看我能做什么吧")
