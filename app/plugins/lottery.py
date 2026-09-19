import random

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg

dice_cmd = on_command("骰子", aliases={"roll"}, priority=5, block=True)
rand_cmd = on_command("随机数", priority=5, block=True)
lottery_cmd = on_command("抽奖", priority=5, block=True)


@dice_cmd.handle()
async def handle_dice(bot: Bot, event: MessageEvent):
    await dice_cmd.finish(f"🎲 掷出了 {random.randint(1, 6)}")


@rand_cmd.handle()
async def handle_rand(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    parts = args.extract_plain_text().strip().replace("，", " ").replace(",", " ").split()
    try:
        if len(parts) >= 2:
            a, b = int(parts[0]), int(parts[1])
            a, b = min(a, b), max(a, b)
        else:
            a, b = 1, 100
    except ValueError:
        await rand_cmd.finish("用法：/随机数 [最小值] [最大值]")
    await rand_cmd.finish(f"🎲 随机数：{random.randint(a, b)}")


@lottery_cmd.handle()
async def handle_lottery(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    # 与 handle_rand 一致：中英文逗号统一归一化为空格，避免「抽奖 3,甲,乙,丙」被当成一个整体
    parts = args.extract_plain_text().strip().replace("，", " ").replace(",", " ").split()
    if not parts:
        await lottery_cmd.finish("用法：/抽奖 [中奖人数] 候选1 候选2 ...")
    try:
        n = int(parts[0])
        candidates = parts[1:]
    except ValueError:
        n = 1
        candidates = parts
    if not candidates:
        await lottery_cmd.finish("没有候选人哦")
    n = max(1, min(n, len(candidates)))
    winners = random.sample(candidates, n)
    await lottery_cmd.finish("🎉 中奖：" + "、".join(winners))
