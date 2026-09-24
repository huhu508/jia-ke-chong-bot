"""活动管理命令（仅管理员，且不进任何帮助/提示文案，命令本身不对外暴露）。

管理员私下记录跑团活动，机器人据此自动发下月日历图 + 活动前一天提醒：
- 添加活动 名称 日期 [时间] 地点    例：添加活动 接力长城 10月7日 浙水院
- 活动列表 [YYYY-MM]                列出活动（含已取消的固定活动标记）
- 删除活动 名称 日期 [地点]         删手动活动；或取消某天固定活动（例训/例跑）
- 删除活动 活动id                   按 id 删除

固定活动：每周三「例训」16:00、每周五「例跑」20:00，默认西北田径场，自动出现在每月日历，
不参与前一天提醒；手动活动同日覆盖固定活动。
"""

import re
from datetime import date, timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from sqlalchemy import select

from ..db import get_session
from ..models.activity import Activity
from ..services import timeutil
from ..services.activity import WEEKLY_EVENTS, build_calendar_image
from ..services.permission import is_superuser

list_cmd = on_command("活动列表", aliases={"活动"}, priority=5, block=True)
add_cmd = on_command("添加活动", aliases={"新增活动"}, priority=5, block=True)
del_cmd = on_command("活动删除", aliases={"删除活动"}, priority=5, block=True)
preview_cmd = on_command("预览月历", aliases={"月历预览"}, priority=5, block=True)

# 时间片段：16:00 / 16：00 / 16点 / 16点半 / 16点30分
_TIME_RE = re.compile(r"(\d{1,2}[:：]\d{2}|\d{1,2}点(?:半|\d{1,2}分)?)")


def _arg(args: Message) -> str:
    return args.extract_plain_text().strip()


def _parse_date(s: str) -> date | None:
    """解析日期：YYYY-MM-DD / YYYY.M.D / YYYY年M月D日 直接解析；
    MM-DD / M.D / M月D日 补当前年（若已过则补下一年）。"""
    s = s.strip()
    today = timeutil.today()
    m = re.fullmatch(r"(\d{4})[-./年](\d{1,2})[-./月](\d{1,2})日?", s)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[-./月](\d{1,2})日?", s)
    if m:
        mo, d = map(int, m.groups())
        try:
            dt = date(today.year, mo, d)
        except ValueError:
            return None
        if dt < today:
            dt = date(today.year + 1, mo, d)
        return dt
    return None


def _split_time_location(rest: str) -> tuple[str, str]:
    """从「时间 地点」片段拆出可选时间；识别不出时间则整段视为地点。"""
    rest = rest.strip()
    if not rest:
        return "", ""
    m = _TIME_RE.search(rest)
    if not m:
        return "", rest
    time_s = m.group(1)
    location = (rest[: m.start()] + " " + rest[m.end() :]).strip()
    return time_s, location


@add_cmd.handle()
async def handle_add(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_superuser(event):
        await add_cmd.finish("仅管理员可执行")
    parts = _arg(args).split(None, 2)
    if len(parts) < 2:
        await add_cmd.finish(
            "用法：添加活动 活动名 日期 [时间] 地点（日期如 10月7日 / 9.23 / 2026-10-05）"
        )
    name, date_s = parts[0], parts[1]
    rest = parts[2] if len(parts) > 2 else ""
    time_s, location = _split_time_location(rest)

    if len(name) > 64:
        await add_cmd.finish("活动名太长（最多 64 字）")
    d = _parse_date(date_s)
    if d is None:
        await add_cmd.finish("日期格式不对，如 10月7日 / 9.23 / 2026-10-05")
    if len(time_s) > 16:
        await add_cmd.finish("时间太长（如 16:00）")
    if len(location) > 128:
        await add_cmd.finish("地点太长（最多 128 字）")

    session = get_session()
    try:
        session.add(Activity(kind="manual", name=name, date=d, time=time_s, location=location))
        session.commit()
    finally:
        session.close()
    detail = f" {time_s}" if time_s else ""
    detail += f" @{location}" if location else ""
    await add_cmd.finish(f"✅ 已添加活动「{name}」：{d}{detail}")


@list_cmd.handle()
async def handle_list(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_superuser(event):
        await list_cmd.finish("仅管理员可执行")
    arg = _arg(args)
    session = get_session()
    try:
        if arg:
            m = re.fullmatch(r"(\d{4})[-./](\d{1,2})", arg)
            if not m:
                await list_cmd.finish("月份格式：YYYY-MM（如 2026-10）")
            y, mo = map(int, m.groups())
            start, end = date(y, mo, 1), _next_month(date(y, mo, 1))
            title = f"{y}年{mo}月"
        else:
            start, end = timeutil.today(), None
            title = "未来"
        q = select(Activity).order_by(Activity.date, Activity.time, Activity.id)
        if end is not None:
            q = q.where(Activity.date >= start, Activity.date < end)
        else:
            q = q.where(Activity.date >= start)
        acts = list(session.execute(q).scalars().all())
    finally:
        session.close()

    if not acts:
        await list_cmd.finish(f"📅 {title}暂无活动")
    lines = []
    for a in acts:
        if a.kind == "cancel":
            lines.append(f"⛔ #{a.id} 已取消 {a.date} 固定活动「{a.name}」")
        else:
            lines.append(
                f"· #{a.id} {a.name}｜{a.date} {a.time or '时间待定'}｜{a.location or '地点待定'}"
            )
    await list_cmd.finish(f"📅 {title}活动\n" + "━━━━━━━━━━━━\n" + "\n".join(lines))


def _next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


async def _del_name_date(name: str, d: date, location: str) -> None:
    """删除某天活动：优先删手动活动；无手动活动且是固定活动（例训/例跑）则取消该天。"""
    session = get_session()
    try:
        acts = list(
            session.execute(
                select(Activity).where(
                    Activity.kind == "manual", Activity.name == name, Activity.date == d
                )
            )
            .scalars()
            .all()
        )
        if location:
            acts = [a for a in acts if a.location == location]
        if acts:
            act = acts[0]
            session.delete(act)
            session.commit()
            await del_cmd.finish(f"✅ 已删除活动「{name}」（{d}）")

        wd = d.weekday()
        if wd in WEEKLY_EVENTS and WEEKLY_EVENTS[wd].name == name:
            dup = (
                session.execute(
                    select(Activity).where(
                        Activity.kind == "cancel", Activity.name == name, Activity.date == d
                    )
                )
                .scalars()
                .first()
            )
            if dup:
                await del_cmd.finish(f"「{name}」（{d}）已是取消状态")
            loc = location or WEEKLY_EVENTS[wd].location
            session.add(Activity(kind="cancel", name=name, date=d, time="", location=loc))
            session.commit()
            await del_cmd.finish(f"⛔ 已取消 {d} 的固定活动「{name}」")

        await del_cmd.finish(f"找不到可删除的活动「{name}」（{d}）")
    finally:
        session.close()


async def _del_name(name: str) -> None:
    session = get_session()
    try:
        acts = list(
            session.execute(
                select(Activity)
                .where(Activity.kind == "manual", Activity.name == name)
                .order_by(Activity.date)
            )
            .scalars()
            .all()
        )
        if not acts:
            await del_cmd.finish(f"活动「{name}」不存在")
        if len(acts) > 1:
            lines = [
                f"· #{a.id} {a.name}｜{a.date} {a.time or '时间待定'}｜{a.location or '地点待定'}"
                for a in acts
            ]
            await del_cmd.finish("同名活动有多个，请带日期或按 id 删除：\n" + "\n".join(lines))
        act = acts[0]
        session.delete(act)
        session.commit()
        await del_cmd.finish(f"✅ 已删除活动「{act.name}」（{act.date}）")
    finally:
        session.close()


@del_cmd.handle()
async def handle_del(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_superuser(event):
        await del_cmd.finish("仅管理员可执行")
    arg = _arg(args)
    if not arg:
        await del_cmd.finish("用法：删除活动 活动id 或 删除活动 活动名 日期 [地点]")

    if arg.isdigit():
        session = get_session()
        try:
            act = session.get(Activity, int(arg))
            if act is None:
                await del_cmd.finish(f"活动 #{arg} 不存在")
            session.delete(act)
            session.commit()
            await del_cmd.finish(f"✅ 已删除「{act.name}」（{act.date}）")
        finally:
            session.close()

    parts = arg.split(None, 2)
    name = parts[0]
    d = _parse_date(parts[1]) if len(parts) >= 2 else None
    if d:
        await _del_name_date(name, d, parts[2].strip() if len(parts) > 2 else "")
    else:
        await _del_name(name)


@preview_cmd.handle()
async def handle_preview(bot: Bot, event: MessageEvent):
    if not is_superuser(event):
        await preview_cmd.finish("仅管理员可执行")
    today = timeutil.today()
    next_first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    try:
        path = build_calendar_image(next_first.year, next_first.month)
    except Exception as e:
        logger.warning(f"生成 {next_first.year}-{next_first.month} 预览月历失败: {e}")
        await preview_cmd.finish("生成月历失败，请稍后再试")
    msg = MessageSegment.image(file=path)
    try:
        await bot.send(event, msg)
    except Exception as e:
        logger.warning(f"发送预览月历失败: {e}")
        await preview_cmd.finish(f"发送失败：{e}")
    await preview_cmd.finish()
