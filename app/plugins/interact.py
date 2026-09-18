"""帮助与菜单：/帮助 查看命令、@机器人 回复菜单 / 运动问答。"""

import asyncio

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.rule import to_me

from ..services import llm

HELP_TEXT = (
    "🐞 甲壳虫使用说明\n"
    "━━━━━━━━━━━━\n"
    "📊 查询：\n"
    "· 今日 —— 查询今日运动数据（已绑定平台自动同步；未绑定发截图自动记录）\n"
    "· 周数据 / 月数据 —— 查看自己的运动次数、跑量、爬升、负荷等\n"
    "· 总结 —— 大模型把本周数据说成人话（「总结 月」看本月）\n"
    "· 鼓励我 —— 大模型按你的数据来一句鼓励\n"
    "· 建议 —— 大模型按你的周/月数据给训练建议（「建议 月」看本月）\n"
    "· 排行 / 周榜 / 月榜 —— 今日 / 本周 / 本月运动三榜\n\n"
    "🔗 绑定（每人独立账号）：\n"
    "· 绑定 coros —— 高驰，私聊发授权链接 → 回群「绑定确认」\n"
    "· 绑定 garmin —— 佳明，私聊我「garmin绑定 邮箱 密码」\n"
    "· 其他平台 —— 无需绑定，直接发运动截图\n"
    "· 我的绑定 / 解绑\n\n"
    "🤖 AI 助手：\n"
    "· @我 + 问题 —— 运动知识问答（配速 / 跑量 / 恢复…）\n"
    "· 截图打卡 —— 记录成功后 AI 自动补一句点评\n\n"
    "🔧 管理员：\n"
    "· 同步数据 / 周聚合 / 机器状态\n\n"
    "🎲 其它：抽奖 / 骰子 / 随机数\n"
    "有问题 @我 即可。"
)


help_cmd = on_command("帮助", aliases={"菜单", "命令", "使用说明", "help"}, priority=5, block=True)


@help_cmd.handle()
async def handle_help(bot: Bot, event: MessageEvent):
    await help_cmd.finish(HELP_TEXT)


# @机器人 → 无正文回菜单；带问题则走运动问答（LLM，失败回落菜单/提示）。
# 优先级最低（99），只有没被其它命令处理时才兜底。
at_me = on_message(rule=to_me(), priority=99, block=True)


@at_me.handle()
async def handle_at(bot: Bot, event: MessageEvent):
    if not isinstance(event, GroupMessageEvent):
        return
    question = event.get_plaintext().strip()
    if not question:
        await at_me.finish(HELP_TEXT)
    text = await asyncio.to_thread(llm.answer_question, question)
    if text:
        await at_me.finish(text)
    await at_me.finish("这个问题我暂时答不上来（AI 未接入或出错），发「帮助」看我能做什么吧")
