"""个人运动汇总：按时间范围聚合**单个成员自己**的 daily_record，输出周/月数据总结。

与 ranking.py（跨成员三榜）不同，这里只关心「一个人」的各项指标总和/均值，
供「周数据」「月数据」这类个人查询，以及「总结」（AI 解读）复用。
"""

from datetime import date

from sqlalchemy import select

from ..db import get_session
from ..models.daily_record import DailyRecord


def compute_member_summary(qq: str, start: date, end: date) -> dict:
    """聚合 [start, end) 区间某成员的全部 daily_record（任一平台/截图）。

    内部自开 session（阻塞 DB 调用），供 asyncio.to_thread 直接执行。返回 dict：
    active_days / activities / distance_km / ascent_meters / active_minutes / calories /
    training_load / max_activity_distance_km / avg_pace_sec_per_km / avg_hr。
    平均配速按距离加权、平均心率按时长加权；无任何数据时各项为 0 / 空。
    """
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
        if (r.distance_km or 0) > 0 or (r.active_minutes or 0) > 0 or (r.calories or 0) > 0:
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
