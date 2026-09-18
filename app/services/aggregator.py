from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models.daily_record import DailyRecord
from ..models.weekly_stat import WeeklyStat


def aggregate_week(week_start: date) -> int:
    """把 [week_start, week_start+7) 的 daily_record 聚合成 weekly_stat。

    返回聚合的 (成员, 平台) 组数。幂等：重复执行会覆盖更新同一周。
    """
    week_end = week_start + timedelta(days=7)
    session: Session = get_session()
    try:
        rows = (
            session.execute(
                select(DailyRecord).where(
                    DailyRecord.record_date >= week_start, DailyRecord.record_date < week_end
                )
            )
            .scalars()
            .all()
        )

        grouped: dict[tuple[str, str], list[DailyRecord]] = {}
        for r in rows:
            grouped.setdefault((r.member_qq, r.platform), []).append(r)

        for (qq, platform), recs in grouped.items():
            existing = session.execute(
                select(WeeklyStat).where(
                    WeeklyStat.member_qq == qq,
                    WeeklyStat.week_start == week_start,
                    WeeklyStat.platform == platform,
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = WeeklyStat(member_qq=qq, week_start=week_start, platform=platform)
                session.add(existing)

            existing.total_steps = sum(r.steps for r in recs)
            existing.total_distance_km = round(sum(r.distance_km for r in recs), 2)
            existing.total_active_minutes = sum(r.active_minutes for r in recs)
            existing.total_calories = sum(r.calories for r in recs)

            hrs = [r.resting_hr for r in recs if r.resting_hr > 0]
            existing.avg_resting_hr = round(sum(hrs) / len(hrs), 1) if hrs else 0.0

            sleeps = [r.sleep_hours for r in recs if r.sleep_hours > 0]
            existing.avg_sleep_hours = round(sum(sleeps) / len(sleeps), 2) if sleeps else 0.0

            existing.active_days = len(recs)
            existing.total_activities = sum(r.activities_count for r in recs)

        session.commit()
        return len(grouped)
    finally:
        session.close()
