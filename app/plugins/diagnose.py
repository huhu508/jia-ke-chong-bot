"""训练诊断命令：基于已同步数据给确定性训练建议，可选附加比赛成绩解锁精确指标。

用法：
  诊断               —— 训练负荷 / 恢复 / 跑量 / 配速趋势（日常数据可支撑的部分）
  诊断 5k 25:00      —— 附加「最近一次比赛成绩」，解锁 VO₂max / 成绩预测 / 训练配速
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.exception import ActionFailed, FinishedException
from nonebot.log import logger
from nonebot.params import CommandArg

from ..db import get_session
from ..models.member import Member
from ..services import diagnose

diagnose_cmd = on_command("诊断", aliases={"能力诊断", "跑步诊断"}, priority=5, block=True)

_USAGE = (
    "用法：\n"
    "· 诊断 —— 看训练负荷 / 恢复 / 跑量 / 配速趋势\n"
    "· 诊断 5k 25:00 —— 附最近一次比赛成绩，解锁 VO₂max / 成绩预测 / 训练配速\n"
    "（距离支持 5k / 10k / 半马 / 全马 等，成绩如 25:00 或 1:45:00 或 50分）"
)


@diagnose_cmd.handle()
async def handle_diagnose(bot: Bot, event: MessageEvent, args=CommandArg()):
    qq = event.get_user_id()
    name = getattr(event.sender, "nickname", None) or qq
    arg_text = args.extract_plain_text().strip()

    session = get_session()
    try:
        member = session.get(Member, qq)
        if member is not None and member.nickname:
            name = member.nickname

        # 带参数 → 按比赛成绩解析；解析失败给用法提示
        race = None
        if arg_text:
            race = diagnose.parse_race(arg_text)
            if race is None:
                await diagnose_cmd.finish(f"没看懂比赛成绩「{arg_text}」\n\n{_USAGE}")

        result = diagnose.compute_diagnosis(session, qq, race)

        # 既无同步数据、也未提供比赛成绩 → 引导先积累数据
        if result["records"] == 0 and race is None:
            await diagnose_cmd.finish(
                "还没有可诊断的数据：\n"
                "① 绑定平台：发「绑定 garmin」或「绑定 coros」，同步几天后即可诊断\n"
                "② 其他平台：多发几张运动截图也能积累跑量/配速数据\n"
                "③ 或直接发「诊断 5k 25:00」用一次比赛成绩做有氧能力评估"
            )

        text = diagnose.format_diagnosis(result, name)
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"诊断失败: {e}")
        await diagnose_cmd.finish(f"诊断失败：{e}")
    finally:
        session.close()

    await diagnose_cmd.finish(text)
