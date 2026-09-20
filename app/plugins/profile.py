"""个人设置：/昵称 设置自定义显示昵称、/删除打卡 撤销最近一次截图打卡。"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.exception import ActionFailed, FinishedException
from nonebot.log import logger
from nonebot.params import CommandArg

from ..db import get_session
from ..models.member import Member
from ..services import sync
from ..services.member import get_or_create_member

nickname_cmd = on_command("昵称", aliases={"设置昵称", "改名"}, priority=5, block=True)
undo_cmd = on_command("删除打卡", aliases={"撤销打卡", "删除记录"}, priority=5, block=True)


@nickname_cmd.handle()
async def handle_nickname(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    qq = event.get_user_id()
    text = args.extract_plain_text().strip()
    session = get_session()
    try:
        member = get_or_create_member(session, qq, getattr(event.sender, "nickname", None))
        if not text:
            await nickname_cmd.finish(
                f"你当前的昵称：{member.display_name}\n"
                "· 发「昵称 新昵称」设置自定义昵称\n"
                "· 发「昵称 清空」恢复为 QQ 昵称"
            )
        if text in ("清空", "清除", "删除", "取消"):
            member.custom_nickname = ""
            session.commit()
            await nickname_cmd.finish("已清空自定义昵称，恢复显示 QQ 昵称")
        if len(text) > 32:
            await nickname_cmd.finish("昵称太长了，最多 32 个字")
        member.custom_nickname = text
        session.commit()
        await nickname_cmd.finish(f"昵称已设置为「{text}」，之后榜单、查询、播报都会显示这个昵称")
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"设置昵称失败: {e}")
        await nickname_cmd.finish(f"设置昵称失败：{e}")
    finally:
        session.close()


@undo_cmd.handle()
async def handle_undo(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    session = get_session()
    try:
        member = session.get(Member, qq)
        if member is not None and member.platform:
            await undo_cmd.finish(
                "你已绑定平台，数据来自平台自动同步，无需手动删除打卡；如需切换数据源请用「解绑」"
            )
        dist, d = sync.undo_last_checkin(qq, session)
        if d is None:
            await undo_cmd.finish("你还没有可删除的打卡记录，发一张运动截图即可开始打卡")
        await undo_cmd.finish(
            f"已删除你最近一次打卡（{d.month}月{d.day}日 {dist} km），累计里程与当日排行已同步扣回"
        )
    except (FinishedException, ActionFailed):
        raise
    except Exception as e:
        logger.exception(f"删除打卡失败: {e}")
        await undo_cmd.finish(f"删除打卡失败：{e}")
    finally:
        session.close()
