import json
from datetime import date, timedelta

from nonebot.log import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models.daily_record import DailyRecord
from ..models.manual_distance import ManualDistance
from ..models.member import Member
from .providers import get_provider
from .providers.base import DailyStats


def _write_record(
    qq: str, d: date, platform: str, stats: DailyStats, session: Session
) -> DailyStats:
    """把一份 DailyStats upsert 到 daily_record（同成员+日期+平台覆盖）。"""
    rec = session.execute(
        select(DailyRecord).where(
            DailyRecord.member_qq == qq,
            DailyRecord.record_date == d,
            DailyRecord.platform == platform,
        )
    ).scalar_one_or_none()
    if rec is None:
        rec = DailyRecord(member_qq=qq, record_date=d, platform=platform)
        session.add(rec)

    rec.steps = stats.steps
    rec.distance_km = stats.distance_km
    rec.active_minutes = stats.active_minutes
    rec.calories = stats.calories
    rec.resting_hr = stats.resting_hr
    rec.sleep_hours = stats.sleep_hours
    rec.activities_count = stats.activities_count
    rec.ascent_meters = stats.ascent_meters
    rec.training_load = stats.training_load
    rec.avg_pace_sec_per_km = stats.avg_pace_sec_per_km
    rec.avg_hr = stats.avg_hr
    rec.max_activity_distance_km = stats.max_activity_distance_km
    rec.raw_json = json.dumps(stats.raw or {}, ensure_ascii=False)
    session.commit()
    return stats


def sync_daily(qq: str, platform: str, d: date) -> DailyStats:
    """从平台拉取某成员某天数据，并 upsert 到 daily_record。

    内部自开 session（阻塞的网络 + 数据库调用），供 asyncio.to_thread 直接执行，
    避免把主线程的 Session 传入线程（违反 SQLAlchemy 线程安全约定）。
    """
    provider = get_provider(platform)
    # 每用户凭据以 QQ 为键，fetch_daily 第一参传成员 QQ
    stats = provider.fetch_daily(qq, d)
    session = get_session()
    try:
        return _write_record(qq, d, provider.name, stats, session)
    finally:
        session.close()


def record_manual_activity(member: Member, d: date, data: dict, session: Session) -> DailyRecord:
    """把一张截图识别到的**单次运动**累加进当日明细（platform="manual"）。

    与平台接口不同，截图一次代表一次运动，因此：
      - distance / ascent / calories / active_minutes **累加**；
      - max_activity_distance_km 取历史与本次的**较大值**；
      - avg_pace/avg_hr 简单以本次覆盖（无更细数据可加权）。
    供每日排行把「未绑定平台、靠截图记录」的成员也纳入统计。
    为阻塞调用，需在 asyncio.to_thread 中执行。
    """
    rec = session.execute(
        select(DailyRecord).where(
            DailyRecord.member_qq == member.qq,
            DailyRecord.record_date == d,
            DailyRecord.platform == "manual",
        )
    ).scalar_one_or_none()
    if rec is None:
        rec = DailyRecord(member_qq=member.qq, record_date=d, platform="manual")
        session.add(rec)

    distance = float(data.get("distance_km") or 0.0)
    # 新建的 DailyRecord 尚未 flush，各列仍是 None（default=0.0 只在 INSERT 时生效），
    # 因此累加前必须用 `or 0` 兜底，否则 None + float 会抛 TypeError。
    rec.distance_km = round((rec.distance_km or 0.0) + distance, 2)
    rec.ascent_meters = round(
        (rec.ascent_meters or 0.0) + float(data.get("ascent_meters") or 0.0), 2
    )
    rec.calories = (rec.calories or 0) + int(data.get("calories") or 0)
    rec.active_minutes = (rec.active_minutes or 0) + int(data.get("active_minutes") or 0)
    rec.max_activity_distance_km = round(max(rec.max_activity_distance_km or 0.0, distance), 2)
    if data.get("avg_pace_sec_per_km"):
        rec.avg_pace_sec_per_km = float(data["avg_pace_sec_per_km"])
    if data.get("avg_hr"):
        rec.avg_hr = int(data["avg_hr"])
    rec.activities_count = (rec.activities_count or 0) + 1
    session.commit()
    return rec


def add_manual_distance(member: Member, distance_km: float, session: Session) -> float:
    """把识别到的距离累加到成员的累计里程，返回累加后的总值。

    未绑定平台的成员通过截图手动记录时使用；只存距离和，不留明细。
    为阻塞调用，需在 asyncio.to_thread 中执行。
    """
    rec = session.get(ManualDistance, member.qq)
    if rec is None:
        rec = ManualDistance(member_qq=member.qq, total_distance_km=0.0)
        session.add(rec)
    rec.total_distance_km = round(rec.total_distance_km + distance_km, 2)
    session.commit()
    return rec.total_distance_km


def get_manual_distance(member_qq: str, session: Session) -> float:
    """查询成员的累计里程（无记录返回 0.0）。"""
    rec = session.get(ManualDistance, member_qq)
    return rec.total_distance_km if rec else 0.0


def clear_member_records(session: Session, qq: str) -> int:
    """删除某成员的全部运动明细（各平台 + 截图）与累计里程。

    绑定/解绑平台时调用：旧数据应由新平台数据取代，避免残留旧数据（截图或
    上一个平台）继续混入排行、造成重复累加。返回删除的 daily_record 行数。
    """
    n = session.execute(delete(DailyRecord).where(DailyRecord.member_qq == qq)).rowcount or 0
    session.execute(delete(ManualDistance).where(ManualDistance.member_qq == qq))
    session.commit()
    return n


def sync_history(qq: str, platform: str, start: date, end: date) -> int:
    """把 [start, end) 区间的平台数据逐日回填进 daily_record（幂等 upsert）。

    绑定平台 / 管理员同步时调用，补齐历史明细，供周榜（7 天）/月榜（最多 31 天）
    聚合出正确结果。内部自开 session（阻塞的网络 + 数据库调用），供 asyncio.to_thread
    直接执行。逐日调用 provider.fetch_daily，单日失败不影响其它日期。返回成功回填天数。
    """
    provider = get_provider(platform)
    done = 0
    d = start
    session = get_session()
    try:
        while d < end:
            try:
                stats = provider.fetch_daily(qq, d)
                _write_record(qq, d, provider.name, stats, session)
                done += 1
            except Exception as e:
                logger.warning(f"回填 {qq} {d} 失败: {e}")
            d += timedelta(days=1)
        return done
    finally:
        session.close()


def sync_today_all() -> int:
    """同步**所有已绑定成员**的今日数据，返回成功同步的人数。

    榜单播报 / 手动「排行」在计算榜单前调用，保证当天数据新鲜——否则绑定 COROS 后
    榜单会因为还没回填到今天而显示空。逐成员调用 sync_daily（各自自开 session），
    单成员失败不影响其它成员。阻塞（网络 + DB），供 asyncio.to_thread 直接执行。
    """
    session = get_session()
    try:
        targets = [
            (m.qq, m.platform)
            for m in session.execute(select(Member).where(Member.platform != "")).scalars().all()
        ]
    finally:
        session.close()

    today = date.today()
    ok = 0
    for qq, platform in targets:
        try:
            sync_daily(qq, platform, today)
            ok += 1
        except Exception as e:
            logger.warning(f"同步 {qq}（{platform}）今日数据失败: {e}")
    return ok
