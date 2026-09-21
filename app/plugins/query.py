import asyncio
from datetime import date

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.exception import ActionFailed, FinishedException
from nonebot.log import logger
from sqlalchemy import select

from ..db import get_session
from ..models.daily_record import DailyRecord
from ..models.member import Member
from ..services import checkin, llm, sync, timeutil
from ..services.cheers import format_pace
from ..services.providers.base import DailyStats
from .admin import notify_gift_claim

query_cmd = on_command("今日", aliases={"步数", "今日运动", "运动"}, priority=5, block=True)


def _format_stats(s: DailyStats) -> str:
    # 顺序参考主流运动 App「用户最关心」：距离 / 配速 / 爬升 / 时长 / 消耗 / 心率 / 负荷。
    # 静息心率已移除；值 >0 才显示，避免刷屏 0 值。步数也不再无条件展示——
    # COROS 等跑步场景步数无意义（群里反馈「为什么一定要说步数」），有值才显示。
    lines = [f"📅 {s.date}"]
    if s.steps:
        lines.append(f"👟 步数：{s.steps}")
    if s.distance_km:
        lines.append(f"📏 距离：{s.distance_km} km")
    if s.avg_pace_sec_per_km:
        lines.append(f"🏃 平均配速：{format_pace(s.avg_pace_sec_per_km)} /km")
    if s.ascent_meters:
        lines.append(f"⛰️ 爬升：{s.ascent_meters:.0f} m")
    if s.active_minutes:
        lines.append(f"⏱ 活动时长：{s.active_minutes} 分钟")
    if s.calories:
        lines.append(f"🔥 活动消耗：{s.calories} 千卡")
    if s.avg_hr:
        lines.append(f"💓 平均心率：{s.avg_hr} bpm")
    if s.training_load:
        lines.append(f"⚡ 运动负荷：{s.training_load:.0f}")
    if s.max_activity_distance_km:
        lines.append(f"🏆 单次最长：{s.max_activity_distance_km} km")
    if s.sleep_hours:
        lines.append(f"😴 睡眠：{s.sleep_hours} 小时")
    if len(lines) == 1:
        lines.append("今日暂无运动记录")
    return "\n".join(lines)


def _format_manual(name: str, d: date, rec, total_km: float) -> str:
    """未绑定成员：把当日截图记录（platform="manual"）+ 累计里程格式化。

    rec 为 None 表示今日暂无新截图，只展示累计里程并引导发截图。
    """
    lines = [f"📅 {d}"]
    if rec is not None:
        if rec.steps:
            lines.append(f"👟 步数：{rec.steps}")
        if rec.distance_km:
            lines.append(f"📏 距离：{rec.distance_km} km")
        if rec.avg_pace_sec_per_km:
            lines.append(f"🏃 平均配速：{format_pace(rec.avg_pace_sec_per_km)} /km")
        if rec.ascent_meters:
            lines.append(f"⛰️ 爬升：{rec.ascent_meters:.0f} m")
        if rec.active_minutes:
            lines.append(f"⏱ 活动时长：{rec.active_minutes} 分钟")
        if rec.calories:
            lines.append(f"🔥 活动消耗：{rec.calories} 千卡")
        if rec.avg_hr:
            lines.append(f"💓 平均心率：{rec.avg_hr} bpm")
        if rec.max_activity_distance_km:
            lines.append(f"🏆 单次最长：{rec.max_activity_distance_km} km")
        if rec.activities_count:
            lines.append(f"🏷️ 今日已记录 {rec.activities_count} 次运动")
    if total_km:
        lines.append(f"📈 累计里程：{total_km} km")

    head = f"{name} 今日运动数据（截图记录）：\n"
    if rec is None:
        return head + "\n".join(lines) + "\n（今日暂无新记录，发一张运动截图即可记录）"
    return head + "\n".join(lines)


def _checkin_badge(qq: str) -> str:
    """生成「本学期第 x 次打卡」一行。用独立 session 读，避免与 sync_daily 的
    跨 session 快照不一致（SQLite WAL 下长事务快照冻结，读不到刚提交的打卡日）。"""
    s = get_session()
    try:
        return f"🎓 本学期第 {checkin.total_days(qq, s)} 次打卡"
    finally:
        s.close()


async def _milestone_cheers(qq: str, nickname: str, bot=None, gid=None) -> str:
    """若本次查询正好跨过打卡里程碑，返回祝贺彩蛋；否则空串。

    同时在第 GIFT_DAYS 天自动领取礼物（先到先得）并通知团长。
    幂等由 CheckinState 保证——截图路径已触发过的里程碑/礼物此处不再重复（返回空）。
    """
    s = get_session()
    ms: list[int] = []
    gift_rank: int | None = None
    try:
        total = checkin.total_days(qq, s)
        ms = checkin.crossed_milestones(qq, total, s)
        gift_rank = checkin.auto_gift(qq, total, s)
        if ms or gift_rank is not None:
            s.commit()
    except Exception as e:
        logger.exception(f"打卡里程碑检测失败: {e}")
    finally:
        s.close()

    parts: list[str] = []
    if gift_rank is not None:
        parts.append(
            f"🎁 恭喜 {nickname}！你是第 {gift_rank} 位达成第 {checkin.GIFT_DAYS} 天打卡的跑友，"
            "自动领取「小红书惊喜小礼物」，已通知团长安排发货～"
        )
        if bot is not None:
            await notify_gift_claim(bot, qq, nickname, gift_rank, gid)
    for m in ms:
        cheer = await asyncio.to_thread(llm.milestone_cheer, nickname, m)
        parts.append(cheer or f"🎉 达成第 {m} 次打卡里程碑，坚持就是胜利！")
    if not parts:
        return ""
    return "\n\n" + "\n".join(parts)


async def build_today(qq: str, nickname: str, bot=None, gid=None) -> str:
    """构建「今日」查询结果文本（命令 handler 与自然语言路由共用，不直接 finish）。

    已绑定平台走接口同步；未绑定读当日截图记录 + 累计里程。
    """
    today = timeutil.today()
    session = get_session()
    try:
        member = session.get(Member, qq)
        if member is not None:
            nickname = member.display_name

        # 已绑定平台 → 走平台接口同步
        if member is not None and member.platform:
            stats = await asyncio.to_thread(sync.sync_daily, member.qq, member.platform, today)
            text = f"{nickname} 今日运动数据：\n{_format_stats(stats)}"
            text += f"\n\n{_checkin_badge(qq)}"
            text += await _milestone_cheers(qq, nickname, bot, gid)
            return text

        # 未绑定 → 读截图记录（当日明细 + 累计里程）
        rec = session.execute(
            select(DailyRecord).where(
                DailyRecord.member_qq == qq,
                DailyRecord.record_date == today,
                DailyRecord.platform == "manual",
            )
        ).scalar_one_or_none()
        total = sync.get_manual_distance(qq, session)

        if rec is None and total <= 0:
            return (
                "你还没有任何运动数据，试试下面任一方式：\n"
                "① 绑定平台：发「绑定 garmin」（佳明，私聊填账号）或「绑定 coros」（高驰，私密授权链接）\n"
                "② 其他平台（无开放接口的 App）：直接发运动截图，我会自动识别并记入今日数据和排行\n"
                "绑定后发「今日」即可查询当日数据"
            )

        text = _format_manual(nickname, today, rec, total)
        text += f"\n\n{_checkin_badge(qq)}"
        text += await _milestone_cheers(qq, nickname)
        return text
    finally:
        session.close()


@query_cmd.handle()
async def handle_query(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    name = getattr(event.sender, "nickname", None) or qq
    try:
        text = await build_today(qq, name, bot, getattr(event, "group_id", None))
        await query_cmd.finish(text)
    except (FinishedException, ActionFailed):
        # finish() 正常终止 / 发送超时（NapCat 偶发）会抛此异常，不属于查询失败，直接放行
        raise
    except KeyError as e:
        await query_cmd.finish(str(e))
    except RuntimeError as e:
        await query_cmd.finish(str(e))
    except Exception as e:
        logger.exception(f"查询失败: {e}")
        await query_cmd.finish(f"查询失败：{e}")
