"""个人运动汇总：按时间范围聚合**单个成员自己**的 daily_record，输出周/月数据总结。

与 ranking.py（跨成员三榜）不同，这里只关心「一个人」的各项指标总和/均值，
供「周数据」「月数据」这类个人查询，以及「总结」（AI 解读）复用。
"""

import re
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models.daily_record import DailyRecord
from . import timeutil
from .checkin import is_active_day


def compute_member_summary(qq: str, start: date, end: date, session: Session | None = None) -> dict:
    """聚合 [start, end) 区间某成员的全部 daily_record（任一平台/截图）。

    默认内部自开 session（阻塞 DB 调用），供 asyncio.to_thread 直接执行；测试可
    传入 session 注入临时库。返回 dict：active_days / activities / distance_km /
    ascent_meters / active_minutes / calories / training_load /
    max_activity_distance_km / avg_pace_sec_per_km / avg_hr。
    平均配速按距离加权、平均心率按时长加权；无任何数据时各项为 0 / 空。
    """
    own_session = session is None
    if own_session:
        session = get_session()
    try:
        rows = (
            session.execute(
                select(DailyRecord).where(
                    DailyRecord.member_qq == qq,
                    DailyRecord.record_date >= start,
                    DailyRecord.record_date < end,
                )
            )
            .scalars()
            .all()
        )
    finally:
        if own_session:
            session.close()

    active_days = 0
    activities = 0
    distance = 0.0
    ascent = 0.0
    active_minutes = 0
    calories = 0
    load = 0.0
    max_single = 0.0
    pace_wsum = 0.0  # 配速 × 距离
    pace_w = 0.0  # 距离权重
    hr_wsum = 0.0  # 心率 × 时长
    hr_w = 0.0  # 时长权重

    for r in rows:
        # 距离/时长/消耗任一项 >0 即视为「运动日」
        if is_active_day(r.distance_km, r.active_minutes, r.calories):
            active_days += 1
        activities += r.activities_count or 0
        distance += r.distance_km or 0.0
        ascent += r.ascent_meters or 0.0
        active_minutes += r.active_minutes or 0
        calories += r.calories or 0
        load += r.training_load or 0.0
        max_single = max(max_single, r.max_activity_distance_km or 0.0)

        dist = r.distance_km or 0.0
        mins = r.active_minutes or 0
        # 平均配速按距离加权（长距离那天的配速占比更高）；平均心率按时长加权
        if r.avg_pace_sec_per_km and dist > 0:
            pace_wsum += r.avg_pace_sec_per_km * dist
            pace_w += dist
        if r.avg_hr and mins > 0:
            hr_wsum += r.avg_hr * mins
            hr_w += mins

    return {
        "active_days": active_days,
        "activities": activities,
        "distance_km": round(distance, 2),
        "ascent_meters": round(ascent, 2),
        "active_minutes": active_minutes,
        "calories": calories,
        "training_load": round(load, 2),
        "max_activity_distance_km": round(max_single, 2),
        "avg_pace_sec_per_km": round(pace_wsum / pace_w, 1) if pace_w > 0 else 0.0,
        "avg_hr": round(hr_wsum / hr_w) if hr_w > 0 else 0,
    }


def parse_range(text: str) -> tuple[date, date, str] | None:
    """把时间段文本解析成 (start, end, label)，end 为不含当天；无法识别返回 None。

    支持：近 N 天 / 近 N 个月 / X月（今年，未来月视为去年）/ YYYY年X月 /
    上月 / 本周 / 本月。刻意不含「年」粒度（一整年跨度太大，且明细只保留一年）。
    供「数据」「历史」命令复用。
    """
    t = (text or "").strip()
    if not t:
        return None
    today = timeutil.today()

    # 近 N 天：近30天 / 最近30天 / 过去7天
    m = re.search(r"(?:近|最近|过去)\s*(\d+)\s*天", t)
    if m:
        n = int(m.group(1))
        return today - timedelta(days=n - 1), today + timedelta(days=1), f"近{n}天"

    # 近 N 个月：近3个月 / 最近3个月
    m = re.search(r"(?:近|最近|过去)\s*(\d+)\s*个?\s*月", t)
    if m:
        n = int(m.group(1))
        y, mo = today.year, today.month - n
        while mo <= 0:
            mo += 12
            y -= 1
        return date(y, mo, 1), today + timedelta(days=1), f"近{n}个月"

    # 指定年月：8月 / 2026年8月 / 2026-8
    m = re.search(r"(?:(\d{4})\s*[年\-/])?\s*(\d{1,2})\s*月", t)
    if m:
        mo = int(m.group(2))
        if not 1 <= mo <= 12:
            return None
        y = int(m.group(1)) if m.group(1) else today.year
        # 只写「8月」且该月尚未到（未来）→ 视为去年（跨年查询，如今年 1 月查「12月」）
        if m.group(1) is None and mo > today.month:
            y -= 1
        start = date(y, mo, 1)
        end = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
        return start, end, f"{y}年{mo}月"

    # 快捷词（刻意不含「年」粒度：一整年跨度太大，且明细只保留最近一年）
    if "上月" in t:
        y, mo = today.year, today.month - 1
        if mo == 0:
            y, mo = y - 1, 12
        return date(y, mo, 1), date(today.year, today.month, 1), f"{y}年{mo}月"
    if "本周" in t:
        return today - timedelta(days=today.weekday()), today + timedelta(days=1), "本周"
    if "本月" in t:
        return date(today.year, today.month, 1), today + timedelta(days=1), "本月"

    return None


def compute_daily_list(
    qq: str, start: date, end: date, session: Session | None = None
) -> list[dict]:
    """返回某成员 [start, end) 每天的逐日运动数据，按日期升序；只含有运动的日期。

    同一成员同一天可能有多条 daily_record（多平台或截图），这里聚合成一天。
    每项：{date, distance_km, active_minutes, ascent_meters, activities_count,
    avg_pace_sec_per_km(距离加权)}。供「历史」逐日明细命令使用。
    """
    own_session = session is None
    if own_session:
        session = get_session()
    try:
        rows = (
            session.execute(
                select(DailyRecord).where(
                    DailyRecord.member_qq == qq,
                    DailyRecord.record_date >= start,
                    DailyRecord.record_date < end,
                )
            )
            .scalars()
            .all()
        )
    finally:
        if own_session:
            session.close()

    by_day: dict[date, dict] = {}
    for r in rows:
        d = r.record_date
        day = by_day.setdefault(
            d,
            {
                "distance_km": 0.0,
                "active_minutes": 0,
                "calories": 0,
                "ascent_meters": 0.0,
                "activities_count": 0,
                "pace_wsum": 0.0,
                "pace_w": 0.0,
            },
        )
        day["distance_km"] += r.distance_km or 0.0
        day["active_minutes"] += r.active_minutes or 0
        day["calories"] += r.calories or 0
        day["ascent_meters"] += r.ascent_meters or 0.0
        day["activities_count"] += r.activities_count or 0
        dist = r.distance_km or 0.0
        if r.avg_pace_sec_per_km and dist > 0:
            day["pace_wsum"] += r.avg_pace_sec_per_km * dist
            day["pace_w"] += dist

    result = []
    for d in sorted(by_day):
        day = by_day[d]
        if not is_active_day(day["distance_km"], day["active_minutes"], day["calories"]):
            continue  # 休息日跳过，避免占满列表
        pace = day["pace_wsum"] / day["pace_w"] if day["pace_w"] > 0 else 0.0
        result.append(
            {
                "date": d,
                "distance_km": round(day["distance_km"], 2),
                "active_minutes": day["active_minutes"],
                "ascent_meters": round(day["ascent_meters"], 2),
                "activities_count": day["activities_count"],
                "avg_pace_sec_per_km": round(pace, 1) if pace > 0 else 0.0,
            }
        )
    return result
