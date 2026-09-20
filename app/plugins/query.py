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
from ..services import sync
from ..services.cheers import format_pace
from ..services.providers.base import DailyStats

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


@query_cmd.handle()
async def handle_query(bot: Bot, event: MessageEvent):
    qq = event.get_user_id()
    name = getattr(event.sender, "nickname", None) or qq
    today = date.today()
    session = get_session()
    try:
        member = session.get(Member, qq)
        # 优先用库里的显示昵称（自定义 > QQ 昵称），保证「今日」与榜单显示一致
        if member is not None:
            name = member.display_name

        # 已绑定平台 → 走平台接口同步
        if member is not None and member.platform:
            stats = await asyncio.to_thread(sync.sync_daily, member.qq, member.platform, today)
            await query_cmd.finish(f"{name} 今日运动数据：\n{_format_stats(stats)}")

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
            await query_cmd.finish(
                "你还没有任何运动数据，试试下面任一方式：\n"
                "① 绑定平台：发「绑定 garmin」（佳明，私聊填账号）或「绑定 coros」（高驰，私密授权链接）\n"
                "② 其他平台（无开放接口的 App）：直接发运动截图，我会自动识别并记入今日数据和排行\n"
                "绑定后发「今日」即可查询当日数据"
            )

        await query_cmd.finish(_format_manual(name, today, rec, total))
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
    finally:
        session.close()
