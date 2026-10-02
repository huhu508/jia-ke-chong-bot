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
from ..services import checkin, cheers, llm, ranking, sync, timeutil
from ..services.providers.base import DailyStats
from .admin import notify_gift_claim

query_cmd = on_command("今日", aliases={"步数", "今日运动", "运动"}, priority=5, block=True)


def _format_stats(s: DailyStats) -> str:
    # 值 >0 才显示（统一在 cheers.format_stat_lines 内处理），避免刷屏 0 值。
    lines = [f"📅 {s.date}"] + cheers.format_stat_lines(s)
    if len(lines) == 1:
        lines.append("今日暂无运动记录")
    return "\n".join(lines)


def _format_manual(name: str, d: date, rec, total_km: float, week_km: float = 0.0) -> str:
    """未绑定成员：把当日截图记录（platform="manual"）+ 累计里程格式化。

    rec 为 None 表示今日暂无新截图，只展示累计里程并引导发截图。
    """
    lines = [f"📅 {d}"]
    if rec is not None:
        lines += cheers.format_stat_lines(rec)
        if rec.activities_count:
            lines.append(f"🏷️ 今日已记录 {rec.activities_count} 次运动")
    if week_km:
        lines.append(f"🗓 本周累计：{week_km} km")
    if total_km:
        lines.append(f"📈 累计里程：{total_km} km")

    head = f"{name} 今日运动数据（截图记录）：\n"
    if rec is None:
        return head + "\n".join(lines) + "\n（今日暂无新记录，发一张运动截图即可记录）"
    return head + "\n".join(lines)


def _checkin_badge(qq: str) -> str:
    """生成「本学期第 x 次打卡 + 连续打卡 + 今日名次」交互徽章区。用独立 session 读，
    避免与 sync_daily 的跨 session 快照不一致（SQLite WAL 下长事务快照冻结，读不到刚提交的打卡日）。"""
    s = get_session()
    try:
        total = checkin.total_days(qq, s)
        streak = checkin.current_streak(qq, s)
        rank = ranking.today_distance_rank(qq, s)
    finally:
        s.close()
    lines = [f"🎓 本学期第 {total} 次打卡"]
    if streak:
        note = cheers.streak_note(streak)
        line = f"🔥 连续打卡 {streak} 天"
        if note:
            line += f"（{note}）"
        lines.append(line)
    if rank is not None:
        lines.append(f"🏅 今日群内第 {rank} 名")
    return "\n".join(lines)


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


async def _daily_cheer(nickname: str, data) -> str:
    """每日打卡后附带一句鼓励：LLM 生成，失败降级到 cheers 模板；无运动数据返回空串。

    data 为 DailyStats（绑定平台）或 DailyRecord（截图记录），二者字段同名，统一 getattr 取值。
    仅在「有运动数据」时才鼓励，避免成员当天没动、仅同步到步数也被硬夸。
    """
    if data is None:
        return ""
    d = {
        "distance_km": getattr(data, "distance_km", 0) or 0,
        "avg_pace_sec_per_km": getattr(data, "avg_pace_sec_per_km", 0) or 0,
        "ascent_meters": getattr(data, "ascent_meters", 0) or 0,
        "calories": getattr(data, "calories", 0) or 0,
        "active_minutes": getattr(data, "active_minutes", 0) or 0,
        "avg_hr": getattr(data, "avg_hr", 0) or 0,
    }
    if not checkin.is_active_day(d["distance_km"], d["active_minutes"], d["calories"]):
        return ""
    cheer = await asyncio.to_thread(llm.checkin_cheer, nickname, d)
    if cheer:
        return f"\n\n💪 {cheer}"
    return f"\n\n{cheers.closer(d)}"


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
            # 「今日群内第 N 名」需全群当天数据齐全（截图 + 绑定成员同口径），
            # 补齐其它绑定成员（带节流）；失败不阻断查询，名次可能略旧。
            try:
                await asyncio.to_thread(sync.sync_today_all_throttled, exclude_qq=member.qq)
            except Exception as e:
                logger.warning(f"同步全员今日数据失败（今日名次可能不全）: {e}")
            text += f"\n\n{_checkin_badge(qq)}"
            text += await _milestone_cheers(qq, nickname, bot, gid)
            text += await _daily_cheer(nickname, stats)
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
        week = sync.get_week_distance(qq, session)

        if rec is None and total <= 0:
            return (
                "还没开张呢，动起来就有数据啦 💪 试试下面任一方式：\n"
                "① 绑定平台：发「绑定 garmin」（佳明，私聊填账号）或「绑定 coros」（高驰，私密授权链接）\n"
                "② 其他平台（无开放接口的 App）：直接发运动截图，我会自动识别并记入今日数据和排行\n"
                "绑定后发「今日」即可查询当日数据"
            )

        text = _format_manual(nickname, today, rec, total, week)
        text += f"\n\n{_checkin_badge(qq)}"
        text += await _milestone_cheers(qq, nickname)
        text += await _daily_cheer(nickname, rec)
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
