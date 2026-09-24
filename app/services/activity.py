"""活动服务：跑团日历图生成 + 活动前一天提醒 + 播报目标解析。

三个职责：
1. resolve_targets —— 从排行播报抽出来的目标解析（bots 按 self_id 去重、群按白名单过滤），
   排行播报与活动播报共用。
2. 跑团日历图 —— 每月末生成下月日历图发到群，有活动的日期画红圈、格内标注活动名。
3. 活动提醒 —— 活动前一天 23:00（搭榜单播报那一刻）提醒「明天有活动」+ 时间/地点。
"""

import asyncio
import datetime
import io
from calendar import monthrange
from dataclasses import dataclass
from pathlib import Path

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.log import logger
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from ..config import settings
from ..db import get_session
from ..models.activity import Activity
from ..models.checkin_state import CheckinState
from ..models.group import Group

# 日历图保存目录（data/ 已在 .gitignore，不入库）
EVENTS_DIR = Path("data/events")

# 跑团团徽背景图：存在则低透明度叠加为日历背景（data/events/logo.jpeg，随 data 部署）
LOGO_PATH = EVENTS_DIR / "logo.jpeg"


@dataclass
class DayEvent:
    """日历某一天要显示的活动：名称 + 时间 + 地点。"""

    name: str
    time: str = ""
    location: str = ""


# 固定活动：每周三「例训」、每周五「例跑」，默认都在西北田径场。
# weekday() 返回值：0=周一 … 2=周三 4=周五。
WEEKLY_EVENTS: dict[int, DayEvent] = {
    2: DayEvent(name="例训", time="16:00", location="西北田径场"),
    4: DayEvent(name="例跑", time="20:00", location="西北田径场"),
}


def resolve_month_events(
    year: int, month: int, manual_acts: list[Activity], cancel_acts: list[Activity]
) -> dict[int, DayEvent]:
    """合并某月活动：固定活动（每周三例训/周五例跑）打底，手动活动覆盖，取消则当天不显示。

    返回 {日: DayEvent}，每天最多一个活动（优先手动）。
    """
    events: dict[int, DayEvent] = {}
    for day in range(1, monthrange(year, month)[1] + 1):
        wd = datetime.date(year, month, day).weekday()
        if wd in WEEKLY_EVENTS:
            events[day] = WEEKLY_EVENTS[wd]
    for a in cancel_acts:
        events.pop(a.date.day, None)
    for a in manual_acts:
        events[a.date.day] = DayEvent(name=a.name, time=a.time, location=a.location)
    return events


def _discover_groups() -> list[int]:
    """返回已记录的群号列表（机器人出现过的群）。阻塞 DB 调用，放线程执行。"""
    session = get_session()
    try:
        return [int(g.group_id) for g in session.execute(select(Group)).scalars().all()]
    finally:
        session.close()


async def resolve_targets() -> tuple[list[Bot], list[int]]:
    """解析播报目标：按 self_id 去重拿 bots；按 broadcast_groups / 自动发现 + allowed_groups 白名单拿群。

    返回 (bots, groups)；任一为空时调用方应放弃播报。
    """
    # 按 self_id 去重，避免 NapCat 重复反向 WS 连接导致同一 bot 播报两遍
    bots: list[Bot] = []
    seen: set[str] = set()
    for b in get_bots().values():
        if isinstance(b, Bot) and b.self_id not in seen:
            seen.add(b.self_id)
            bots.append(b)

    # 去重，避免 .env 手写重复群号导致同群播报两遍
    groups = list(dict.fromkeys(settings.broadcast_groups))
    if not groups:
        try:
            groups = await asyncio.to_thread(_discover_groups)
        except Exception as e:
            logger.exception(f"发现播报群失败: {e}")
            groups = []
        groups = list(dict.fromkeys(groups))
    # 群白名单：仅在 allowed_groups 内的群播报（未配置则不限制）
    if settings.allowed_groups:
        allowed = {int(g) for g in settings.allowed_groups}
        groups = [g for g in groups if g in allowed]
    return bots, groups


# ---------------------------------------------------------------------------
# 中文字体探测：日历图标题/活动名是中文，需加载一个含 CJK 的字体。
# 优先 data/fonts/（部署时连同 data/ 一起放，不进 git），再探测常见系统路径，
# 最后兜底 load_default（日期数字仍能显示，中文会变方块）。
# ---------------------------------------------------------------------------

_FONT_PATH: str | None = None
_FONT_SEARCHED = False


def _find_font() -> str | None:
    global _FONT_PATH, _FONT_SEARCHED
    if _FONT_SEARCHED:
        return _FONT_PATH
    _FONT_SEARCHED = True
    candidates: list[Path] = []
    fonts_dir = Path("data/fonts")
    if fonts_dir.is_dir():
        for pat in ("*.ttf", "*.ttc", "*.otf"):
            candidates.extend(sorted(fonts_dir.glob(pat)))
    candidates += [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for p in candidates:
        if p.is_file():
            _FONT_PATH = str(p)
            return _FONT_PATH
    return None


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _find_font()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            logger.warning(f"加载字体 {path} 失败: {e}")
    return ImageFont.load_default()


def _truncate_label(text: str, draw: ImageDraw.ImageDraw, font, max_w: float) -> str:
    """按最大宽度截断活动名：逐字累加，超出即停，多余字符直接舍弃、不加省略号。"""
    out = ""
    for ch in text:
        if draw.textlength(out + ch, font=font) > max_w:
            break
        out += ch
    return out


def _load_logo_bg(base: Image.Image) -> Image.Image:
    """把团徽以很低的不透明度居中叠加到背景（很淡，不干扰文字阅读）。"""
    if not LOGO_PATH.exists():
        return base
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
    except Exception as e:
        logger.warning(f"加载团徽背景失败: {e}")
        return base
    # 缩放到不超过画布 65%（保持比例），居中放置
    logo.thumbnail((int(base.width * 0.65), int(base.height * 0.65)), Image.Resampling.LANCZOS)
    # 整体不透明度压到 ~15%，只留淡影
    alpha = logo.getchannel("A").point(lambda a: int(a * 0.15))
    logo.putalpha(alpha)
    base_rgba = base.convert("RGBA")
    base_rgba.alpha_composite(
        logo, ((base.width - logo.width) // 2, (base.height - logo.height) // 2)
    )
    return base_rgba.convert("RGB")


def generate_calendar_image(
    year: int, month: int, manual_acts: list[Activity], cancel_acts: list[Activity] | None = None
) -> bytes:
    """生成某月「跑团日历」PNG：周一开头、有活动的日期画红圈、格内标注活动名。

    manual_acts: 手动活动（kind=manual）；cancel_acts: 取消固定活动的标记（kind=cancel），可空。
    固定活动（每周三例训/周五例跑）自动展开；同日手动活动覆盖固定活动。
    """
    from calendar import monthcalendar

    events = resolve_month_events(year, month, manual_acts, cancel_acts or [])

    weeks = monthcalendar(year, month)
    n_rows = len(weeks)

    # 2x 高清缩放：整体放大画布与字号，提升在手机高 DPI 下的清晰度
    S = 2
    W = 760 * S
    MARGIN = 30 * S
    col_w = (W - 2 * MARGIN) / 7  # 200
    ROW_H = col_w  # 正方形格子
    GRID_Y0 = 164 * S
    H = int(GRID_Y0 + n_rows * ROW_H + 30 * S)

    img = Image.new("RGB", (W, H), "white")
    img = _load_logo_bg(img)
    draw = ImageDraw.Draw(img)

    main_font = _load_font(42 * S)
    sub_font = _load_font(16 * S)
    month_font = _load_font(20 * S)
    weekday_font = _load_font(18 * S)
    day_font = _load_font(26 * S)
    name_font = _load_font(20 * S)

    # 主标题 + 副标题 + 月份行
    draw.text((W / 2, 42 * S), "甲壳虫月历", font=main_font, anchor="mm", fill="black")
    draw.text(
        (W / 2, 80 * S),
        "runner calendar 一起跑，向未来",
        font=sub_font,
        anchor="mm",
        fill="#888888",
    )
    draw.text((W / 2, 108 * S), f"{year}年{month}月", font=month_font, anchor="mm", fill="black")

    # 星期行（统一黑色）
    for i, wd in enumerate("一二三四五六日"):
        draw.text(
            (MARGIN + i * col_w + col_w / 2, 140 * S),
            wd,
            font=weekday_font,
            anchor="mm",
            fill="black",
        )

    # 网格线（按实际周数画，不产生空行）
    for r in range(n_rows + 1):
        y = GRID_Y0 + r * ROW_H
        draw.line([(MARGIN, y), (W - MARGIN, y)], fill="#dddddd", width=S)
    for c in range(8):
        x = MARGIN + c * col_w
        draw.line([(x, GRID_Y0), (x, GRID_Y0 + n_rows * ROW_H)], fill="#dddddd", width=S)

    # 活动名最大宽度 ≈ 5 个中文字符宽，超出直接舍弃（不加省略号）
    max_label_w = draw.textlength("字" * 5, font=name_font)

    # 日期数字 + 红圈 + 活动名
    for r, week in enumerate(weeks):
        for c, day in enumerate(week):
            if day == 0:
                continue
            cx = MARGIN + c * col_w + col_w / 2
            cy = GRID_Y0 + r * ROW_H + 34 * S
            draw.text((cx, cy), str(day), font=day_font, anchor="mm", fill="black")

            ev = events.get(day)
            if not ev:
                continue
            # 红色空心圆圈住日期数字
            draw.ellipse(
                [cx - 21 * S, cy - 19 * S, cx + 21 * S, cy + 19 * S], outline="red", width=3 * S
            )
            # 格内下方标注活动名（截断到约 5 个中文字宽）
            label = _truncate_label(ev.name, draw, name_font, max_label_w)
            draw.text(
                (cx, GRID_Y0 + r * ROW_H + 68 * S),
                label,
                font=name_font,
                anchor="mm",
                fill="#c0392b",
            )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 幂等标记 key（复用 checkin_state 状态表）
# ---------------------------------------------------------------------------


def _remind_key(act_id: int) -> str:
    return f"activity_remind:{act_id}"


def _calendar_key(year: int, month: int) -> str:
    return f"activity_calendar:{year:04d}-{month:02d}"


async def remind_tomorrow(today: datetime.date) -> None:
    """提醒「明天有活动」：只查明天的手动活动（固定例训/例跑不提醒），逐条发到目标群，成功后打幂等标记。"""
    tomorrow = today + datetime.timedelta(days=1)
    session = get_session()
    try:
        acts = list(
            session.execute(
                select(Activity)
                .where(Activity.kind == "manual", Activity.date == tomorrow)
                .order_by(Activity.time, Activity.id)
            )
            .scalars()
            .all()
        )
        if not acts:
            return
        keys = [_remind_key(a.id) for a in acts]
        done = set(
            session.execute(select(CheckinState.key).where(CheckinState.key.in_(keys)))
            .scalars()
            .all()
        )
    finally:
        session.close()

    bots, groups = await resolve_targets()
    if not bots or not groups:
        logger.warning("明天有活动但无可播报的群或 bot，跳过提醒")
        return

    for a in acts:
        key = _remind_key(a.id)
        if key in done:
            continue
        text = (
            "📅 明天有活动！\n"
            f"🏃 {a.name}\n"
            f"🕐 时间：{a.time or '待定'}\n"
            f"📍 地点：{a.location or '待定'}"
        )
        sent = False
        for bot in bots:
            for gid in groups:
                try:
                    await bot.send_group_msg(group_id=gid, message=text)
                    sent = True
                except Exception as e:
                    logger.warning(f"向群 {gid} 提醒活动「{a.name}」失败: {e}")
        if sent:
            session = get_session()
            try:
                session.add(CheckinState(key=key))
                session.commit()
            finally:
                session.close()


def _cleanup_old_calendars(keep: int = 3) -> None:
    """只保留最近 keep 张日历图，删除更早的，避免图片长期累积占用磁盘。"""
    try:
        files = sorted(
            EVENTS_DIR.glob("calendar_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return
    for f in files[keep:]:
        try:
            f.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"清理旧日历图失败 {f}: {e}")


def build_calendar_image(year: int, month: int) -> Path:
    """生成某月跑团日历图并写盘，返回 PNG 路径（含手动活动 + 固定活动 + 取消标记）。"""
    first_day = datetime.date(year, month, 1)
    end = first_day.replace(day=monthrange(year, month)[1]) + datetime.timedelta(days=1)

    session = get_session()
    try:
        manual_acts = list(
            session.execute(
                select(Activity).where(
                    Activity.kind == "manual", Activity.date >= first_day, Activity.date < end
                )
            )
            .scalars()
            .all()
        )
        cancel_acts = list(
            session.execute(
                select(Activity).where(
                    Activity.kind == "cancel", Activity.date >= first_day, Activity.date < end
                )
            )
            .scalars()
            .all()
        )
    finally:
        session.close()

    png = generate_calendar_image(year, month, manual_acts, cancel_acts)
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = EVENTS_DIR / f"calendar_{year:04d}-{month:02d}.png"
    path.write_bytes(png)
    _cleanup_old_calendars()
    return path


async def send_next_month_calendar(first_day: datetime.date) -> None:
    """生成并发送某月「跑团日历」图；同月只发一次（幂等 key 保证）。"""
    year, month = first_day.year, first_day.month
    key = _calendar_key(year, month)

    session = get_session()
    try:
        if session.get(CheckinState, key) is not None:
            return
    finally:
        session.close()

    path = build_calendar_image(year, month)

    bots, groups = await resolve_targets()
    if not bots or not groups:
        logger.warning(f"无群可发 {year}-{month} 跑团日历，跳过")
        return

    msg = MessageSegment.image(file=path)
    sent = False
    for bot in bots:
        for gid in groups:
            try:
                await bot.send_group_msg(group_id=gid, message=msg)
                sent = True
                logger.info(f"已向群 {gid} 发送 {year}-{month} 跑团日历")
            except Exception as e:
                logger.warning(f"向群 {gid} 发送跑团日历失败: {e}")

    if sent:
        session = get_session()
        try:
            session.add(CheckinState(key=key))
            session.commit()
        finally:
            session.close()
