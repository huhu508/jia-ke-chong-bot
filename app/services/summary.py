"""个人运动汇总：按时间范围聚合**单个成员自己**的 daily_record，输出周/月数据总结。

与 ranking.py（跨成员三榜）不同，这里只关心「一个人」的各项指标总和/均值，
供「周数据」「月数据」这类个人查询，以及「总结」（AI 解读）复用。
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.daily_record import DailyRecord


def compute_member_summary(session: Session, qq: str, start: date, end: date) -> dict:
    """聚合 [start, end) 区间某成员的全部 daily_record（任一平台/截图）。

    返回 dict：active_days / activities / distance_km / ascent_meters /
    active_minutes / calories / training_load / max_activity_distance_km /
    avg_pace_sec_per_km / avg_hr。无任何数据时各项为 0 / 空。
    """
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

    active_days = 0
    activities = 0
    distance = 0.0
    ascent = 0.0
    active_minutes = 0
    calories = 0
    load = 0.0
    max_single = 0.0
    paces: list[float] = []
    hrs: list[int] = []

    for r in rows:
        # 距离/时长/消耗任一项 >0 即视为「运动日」
        if (r.distance_km or 0) > 0 or (r.active_minutes or 0) > 0 or (r.calories or 0) > 0:
            active_days += 1
        activities += r.activities_count or 0
        distance += r.distance_km or 0.0
        ascent += r.ascent_meters or 0.0
        active_minutes += r.active_minutes or 0
        calories += r.calories or 0
        load += r.training_load or 0.0
        max_single = max(max_single, r.max_activity_distance_km or 0.0)
        if r.avg_pace_sec_per_km:
            paces.append(r.avg_pace_sec_per_km)
        if r.avg_hr:
            hrs.append(r.avg_hr)

    return {
        "active_days": active_days,
        "activities": activities,
        "distance_km": round(distance, 2),
        "ascent_meters": round(ascent, 2),
        "active_minutes": active_minutes,
        "calories": calories,
        "training_load": round(load, 2),
        "max_activity_distance_km": round(max_single, 2),
        "avg_pace_sec_per_km": round(sum(paces) / len(paces), 1) if paces else 0.0,
        "avg_hr": round(sum(hrs) / len(hrs)) if hrs else 0,
    }
