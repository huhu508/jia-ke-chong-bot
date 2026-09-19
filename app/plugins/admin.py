import asyncio
from datetime import date, timedelta

from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import (
    Bot,
    Message,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.exception import ActionFailed
from nonebot.log import logger
from nonebot.params import CommandArg
from sqlalchemy import func, select

from ..db import get_session
from ..models.manual_distance import ManualDistance
from ..models.member import Member
from ..services import aggregator, credentials, retention, sync
from ..services.member import get_or_create_member
from ..services.providers import VALID_PLATFORMS, get_provider

bind_cmd = on_command("绑定", priority=5, block=True)
my_bind_cmd = on_command("我的绑定", priority=5, block=True)
confirm_cmd = on_command("绑定确认", priority=5, block=True)
unbind_cmd = on_command("解绑", priority=5, block=True)
status_cmd = on_command("机器状态", priority=5, block=True)
sync_all_cmd = on_command("同步数据", priority=5, block=True)
aggregate_cmd = on_command("周聚合", priority=5, block=True)
garmin_bind_cmd = on_command("garmin绑定", aliases={"佳明绑定"}, priority=5, block=True)

# 平台展示名（绑定提示 / 状态 / 我的绑定 三处共用）
_PLATFORM_LABELS = {
    "garmin": "佳明 Garmin",
    "coros": "高驰 COROS",
}

# 绑定/同步时回填平台历史的天数（覆盖周榜 7 天 + 月榜最多 31 天）
BACKFILL_DAYS = 31


def _is_superuser(event: MessageEvent) -> bool:
    superusers = get_driver().config.superusers
    return event.get_user_id() in superusers


async def _backfill_async(qqs: list[str] | None = None) -> None:
    """后台回填平台历史数据（默认全部已绑定成员，或指定 qq 列表）。

    逐日调用 provider.fetch_daily 并 upsert 进 daily_record，供周榜/月榜聚合。
    阻塞的网络 + DB 调用放 asyncio.to_thread，避免卡住事件循环；sync_history 内部自开
    session，这里只把 (qq, platform) 普通值传进线程，不共享主线程 Session。
    """
    session = get_session()
    try:
        stmt = select(Member).where(Member.platform != "")
        if qqs:
            stmt = stmt.where(Member.qq.in_(qqs))
        members = session.execute(stmt).scalars().all()
        # 提前把绑定平台提取成普通值，关闭 session 后再回填
        targets = [(m.qq, m.platform) for m in members]
    except Exception as e:
        logger.exception(f"后台回填失败: {e}")
        return
    finally:
        session.close()

    today = date.today()
    start = today - timedelta(days=BACKFILL_DAYS - 1)
    end = today + timedelta(days=1)
    for qq, platform in targets:
        try:
            n = await asyncio.to_thread(sync.sync_history, qq, platform, start, end)
            logger.info(f"回填 {qq}（{platform}）完成：{n} 天")
        except Exception as e:
            logger.warning(f"回填 {qq} 失败: {e}")


@bind_cmd.handle()
async def handle_bind(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    platform = args.extract_plain_text().strip().lower()

    # 除已接入平台外，其余均无开放接口 → 统一引导发截图
    if platform not in VALID_PLATFORMS:
        await bind_cmd.finish(
            "绑定支持：garmin（佳明）/ coros（高驰）\n"
            "其他平台没有开放接口，无需绑定，直接发运动截图即可，我会自动识别并记入今日数据和排行。"
        )

    qq = event.get_user_id()

    # COROS：发私密授权链接（私聊发送，不暴露在群里），无需管理员
    if platform == "coros":
        provider = get_provider("coros")
        try:
            url = await asyncio.to_thread(provider.start_auth, {"qq": qq})
        except Exception as e:
            logger.exception(f"发起 COROS 授权失败: {e}")
            await bind_cmd.finish(f"发起 COROS 授权失败：{e}")
        # NapCat 私聊偶发超时（retcode=1200）但消息可能已发出：失败不阻断、不误报
        try:
            await bot.send_private_msg(
                user_id=int(qq),
                message="请在浏览器打开以下链接完成 COROS 授权（约 5 分钟内有效，不会顶掉手机 App）：\n"
                f"{url}\n\n授权完成后，回群里发「绑定确认」完成绑定。",
            )
        except Exception as e:
            logger.warning(f"私聊发送授权链接异常（可能已发出）: {e}")
        await bind_cmd.finish(
            "已私聊你授权链接，请查收并完成授权后回群里发「绑定确认」；"
            "若没收到私聊，请先添加我为好友，再发一次「绑定 coros」（机器人私聊需互为好友）"
        )

    # Garmin：无 OAuth，引导私聊发邮箱密码（私聊需先互为好友，否则消息到不了机器人）
    await bind_cmd.finish(
        "佳明没有第三方授权接口，请**先添加我为好友**，再**私聊**我发送：\n"
        "「garmin绑定 邮箱 密码」\n"
        "例如：garmin绑定 abc@example.com MyPass123（邮箱和密码之间用一个空格隔开）\n"
        "我会按你的 QQ 私密保存并登录拉数，密码不会出现在群里。"
    )


@garmin_bind_cmd.handle()
async def handle_garmin_bind(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not isinstance(event, PrivateMessageEvent):
        await garmin_bind_cmd.finish(
            "请不要在群里发密码，请**先添加我为好友**，再私聊我「garmin绑定 邮箱 密码」"
        )

    parts = args.extract_plain_text().strip().split(None, 1)
    if len(parts) != 2:
        await garmin_bind_cmd.finish(
            "用法：garmin绑定 邮箱 密码\n"
            "例如：garmin绑定 abc@example.com MyPass123（邮箱和密码之间用一个空格隔开）"
        )
    email, password = parts[0], parts[1]

    qq = event.get_user_id()
    provider = get_provider("garmin")
    try:
        await asyncio.to_thread(provider.bind, qq, email, password)
    except RuntimeError as e:
        await garmin_bind_cmd.finish(str(e))
    except Exception as e:
        logger.exception(f"佳明绑定异常: {e}")
        await garmin_bind_cmd.finish(f"佳明绑定失败：{e}")

    session = get_session()
    try:
        member = get_or_create_member(session, qq, getattr(event.sender, "nickname", None))
        member.platform = "garmin"
        member.platform_account = ""
        # 清掉旧数据（截图/其它平台），改由佳明数据取代，避免旧数据残留进排行
        sync.clear_member_records(session, qq)
    finally:
        session.close()

    asyncio.create_task(_backfill_async([qq]))
    await garmin_bind_cmd.finish(
        f"佳明绑定成功 ✅\n已绑定账号：{email}\n"
        "之前的截图里程已由佳明数据取代，正在后台同步你的历史数据，稍后发「今日」即可查询"
    )


@my_bind_cmd.handle()
async def handle_my_bind(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    session = get_session()
    try:
        member = session.get(Member, qq)
        if member is None or not member.platform:
            await my_bind_cmd.finish(
                "你还没绑定平台，发「绑定 garmin」（佳明）/「绑定 coros」（高驰）选择数据来源；"
                "其他平台直接发运动截图即可"
            )
        label = _PLATFORM_LABELS.get(member.platform, member.platform)
        await my_bind_cmd.finish(f"你当前绑定的是：{label}，查询固定返回该平台数据")
    finally:
        session.close()


@confirm_cmd.handle()
async def handle_confirm(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    provider = get_provider("coros")
    try:
        result = await asyncio.to_thread(provider.finish_auth, qq)
    except RuntimeError as e:
        await confirm_cmd.finish(f"授权确认失败：{e}")
    if not result.get("authorized"):
        await confirm_cmd.finish("还没检测到授权，请先在浏览器完成授权，再发送「绑定确认」")

    session = get_session()
    try:
        member = get_or_create_member(session, qq, getattr(event.sender, "nickname", None))
        member.platform = "coros"
        member.platform_account = ""
        # 清掉旧数据（截图/其它平台），改由 coros 数据取代，避免旧数据残留进排行
        sync.clear_member_records(session, qq)
    finally:
        session.close()

    # 后台回填历史数据，供周榜/月榜（不阻塞绑定确认）
    asyncio.create_task(_backfill_async([qq]))
    await confirm_cmd.finish(
        "COROS 授权成功，已绑定 ✅ 之前的截图里程已由高驰数据取代，"
        "正在后台同步你的历史数据，稍后发「今日」即可查询"
    )


@unbind_cmd.handle()
async def handle_unbind(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    session = get_session()
    try:
        member = session.get(Member, qq)
        if member is None or not member.platform:
            await unbind_cmd.finish("你还没有绑定记录")
        platform = member.platform
        member.platform = ""
        member.platform_account = ""
        # 解绑后清掉该成员的平台运动明细，避免残留旧数据继续出现在排行
        sync.clear_member_records(session, qq)
    finally:
        session.close()

    # 清掉凭据/token，保证再次绑定需重新授权
    credentials.delete(qq, platform)
    await unbind_cmd.finish(
        "已解绑 ✅ 之前的平台数据已清除，现在可发运动截图重新记录；"
        "重新绑定时会再次要求授权/填写账号"
    )


@status_cmd.handle()
async def handle_status(bot: Bot, event: MessageEvent):
    session = get_session()
    try:
        counts = {
            p: (
                session.execute(
                    select(func.count()).select_from(Member).where(Member.platform == p)
                ).scalar()
                or 0
            )
            for p in VALID_PLATFORMS
        }
        n_manual = (
            session.execute(
                select(func.count())
                .select_from(ManualDistance)
                .join(Member, ManualDistance.member_qq == Member.qq)
                .where(Member.platform == "")
            ).scalar()
            or 0
        )
    finally:
        session.close()

    lines = [
        f"佳明 Garmin：{counts['garmin']} 人 · 私聊「garmin绑定 邮箱 密码」",
        f"高驰 COROS：{counts['coros']} 人 · 发「绑定 coros」私密授权",
        f"其他平台：{n_manual} 人靠截图记录 · 📷 发运动截图自动记录",
    ]
    await status_cmd.finish("📊 机器状态\n" + "━━━━━━━━━━━━\n" + "\n".join(lines))


@sync_all_cmd.handle()
async def handle_sync_all(bot: Bot, event: MessageEvent):
    if not _is_superuser(event):
        await sync_all_cmd.finish("仅管理员可执行")
    session = get_session()
    try:
        members = session.execute(select(Member).where(Member.platform != "")).scalars().all()
    finally:
        session.close()
    if not members:
        await sync_all_cmd.finish("还没有任何成员绑定平台")

    # 后台回填（逐日拉平台历史），避免长时间阻塞命令响应
    asyncio.create_task(_backfill_async())
    await sync_all_cmd.finish(
        f"已开始后台同步 {len(members)} 人的历史数据（近 {BACKFILL_DAYS} 天），"
        "完成后发「排行 / 周榜 / 月榜」即可看到正确数据"
    )


@aggregate_cmd.handle()
async def handle_aggregate(bot: Bot, event: MessageEvent):
    if not _is_superuser(event):
        await aggregate_cmd.finish("仅管理员可执行")
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    try:
        n_agg = await asyncio.to_thread(aggregator.aggregate_week, last_monday)
        n_del = await asyncio.to_thread(
            retention.cleanup_daily, today - timedelta(days=retention.RETENTION_DAYS)
        )
        await aggregate_cmd.finish(
            f"周聚合完成：聚合 {n_agg} 组，清理 {n_del} 条原始明细"
            f"（保留近 {retention.RETENTION_DAYS} 天供周榜/月榜）"
        )
    except ActionFailed:
        raise
    except Exception as e:
        logger.exception(f"周聚合失败: {e}")
        await aggregate_cmd.finish(f"周聚合失败：{e}")
