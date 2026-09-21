"""打卡体系：天数统计 / 里程碑 / 节日彩蛋 / 群抽奖 / 礼物，全数据驱动、可泛化。

统一口径（与用户确认）：
  - 打卡单位 = 天：同一成员同一天多次运动/截图只算 1 天；
  - 覆盖范围 = 绑定平台 + 截图：绑定 Garmin/COROS 当天有平台数据即算，未绑定发截图算；
  - 计数起点 = SEMESTER_START（默认 2026-09-22）：之前的记录不统计。

数据源是 CheckinDay 表（每成员每天一行，永不清理）。触发幂等靠 CheckinState 表，
重复打卡/重复查询不会重复发彩蛋。所有触发规则数据驱动，加新能力只改本文件顶部常量、
不改代码逻辑。
"""

import random
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models.checkin_day import CheckinDay
from ..models.checkin_state import CheckinState
from . import timeutil

# —— 个人打卡里程碑：累计打卡天数达这些值时触发鼓励彩蛋（可自行增删）——
MILESTONES = [10, 66, 88, 99, 100, 200, 300, 365]

# —— 第 N 天礼物（先到先得）——
GIFT_DAYS = 100
GIFT_QUOTA = 3  # 全群先到先得名额

# —— 群抽奖：全群累计打卡天数突破这些值时触发一次（每次抽 LOTTERY_WINNERS 名）——
LOTTERY_THRESHOLDS = [200, 400]
LOTTERY_PRIZE = "小红书赞助的帽子"
LOTTERY_WINNERS = 1

# —— 节日 + 特殊距离彩蛋：日期命中且打卡距离≈对应 km 时触发（date=(月,日)）——
FESTIVALS = [
    {"date": (10, 1), "name": "国庆", "distance_km": 10.01},
    {"date": (1, 1), "name": "元旦", "distance_km": 1.01},
    {"date": (5, 1), "name": "五一", "distance_km": 5.01},
    {"date": (6, 1), "name": "六一", "distance_km": 6.01},
]
FESTIVAL_DIST_TOL = 0.05  # 距离匹配容差（km）


def counting_start() -> date:
    """打卡计数起点（本学期起点）。之前的记录不统计。"""
    try:
        return date.fromisoformat(settings.semester_start)
    except ValueError:
        # 配置写错时退回今天，避免整条链路崩溃（最多统计从今天开始）
        return timeutil.today()


def ensure_day(qq: str, d: date, session: Session) -> bool:
    """记录一个打卡日（幂等）。d 早于计数起点时不记录。返回是否新增。

    不自行 commit——复用调用方的 session，随其 commit 落库（与 sync 写库路径一致）。
    """
    if d < counting_start():
        return False
    exists = session.execute(
        select(CheckinDay.id).where(
            CheckinDay.member_qq == qq, CheckinDay.record_date == d
        )
    ).scalar_one_or_none()
    if exists is not None:
        return False
    session.add(CheckinDay(member_qq=qq, record_date=d))
    return True


def total_days(qq: str, session: Session) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(CheckinDay).where(CheckinDay.member_qq == qq)
        )
        or 0
    )


def month_days(qq: str, start: date, end: date, session: Session) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(CheckinDay)
            .where(
                CheckinDay.member_qq == qq,
                CheckinDay.record_date >= start,
                CheckinDay.record_date < end,
            )
        )
        or 0
    )


def global_total(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(CheckinDay)) or 0


def _mark_state(key: str, session: Session) -> bool:
    """写一条触发状态（幂等），返回是否为首次写入。"""
    if session.get(CheckinState, key) is not None:
        return False
    session.add(CheckinState(key=key))
    return True


def crossed_milestones(qq: str, total: int, session: Session) -> list[int]:
    """返回本次新跨越的里程碑值列表（可能一次跨多个，如回填后跳级）。"""
    hit = []
    for m in sorted(MILESTONES):
        if total >= m and _mark_state(f"milestone:{m}:{qq}", session):
            hit.append(m)
    return hit


def check_lottery(session: Session) -> int | None:
    """若全群累计打卡突破某个抽奖阈值，写状态并返回该阈值；否则 None。"""
    total = global_total(session)
    for t in sorted(LOTTERY_THRESHOLDS):
        if total >= t and _mark_state(f"lottery:{t}", session):
            return t
    return None


def draw_lottery(session: Session) -> list[str]:
    """从所有打卡日中抽 LOTTERY_WINNERS 个中奖成员（每行=1 张票，多打卡多票）。

    random.sample 按行抽样（同一成员多天会占多行，权重自然更高），结果去重保序——
    同一成员即使多行被抽中，也只中一次。
    """
    rows = session.execute(select(CheckinDay.member_qq)).scalars().all()
    if not rows:
        return []
    winners = random.sample(rows, min(LOTTERY_WINNERS, len(rows)))
    return list(dict.fromkeys(winners))


def match_festival(d: date, distance_km: float) -> dict | None:
    """日期命中节日且打卡距离≈对应特殊距离时返回节日 dict，否则 None。"""
    for f in FESTIVALS:
        if (d.month, d.day) == f["date"] and abs((distance_km or 0.0) - f["distance_km"]) <= FESTIVAL_DIST_TOL:
            return f
    return None


def gift_claims(session: Session) -> int:
    """当前已领取第 GIFT_DAYS 天礼物的人数。"""
    return (
        session.scalar(
            select(func.count())
            .select_from(CheckinState)
            .where(CheckinState.key.like(f"gift:{GIFT_DAYS}:%"))
        )
        or 0
    )


def claim_gift(qq: str, session: Session) -> bool:
    """尝试领取第 GIFT_DAYS 天礼物（先到先得）。返回是否为首次领取。"""
    return _mark_state(f"gift:{GIFT_DAYS}:{qq}", session)


def auto_gift(qq: str, total: int, session: Session) -> int | None:
    """累计打卡首次达到 GIFT_DAYS 且名额未满时自动领取礼物，返回领取序位（1 起）；否则 None。

    幂等由 CheckinState（gift:<GIFT_DAYS>:<qq>）保证，重复触发不会重复领取。
    供截图/今日路径在成员跨过 GIFT_DAYS 时自动触发，无需成员手动发「领礼物」。
    """
    if total < GIFT_DAYS:
        return None
    if session.get(CheckinState, f"gift:{GIFT_DAYS}:{qq}") is not None:
        return None
    before = gift_claims(session)
    if before >= GIFT_QUOTA:
        return None
    claim_gift(qq, session)
    return before + 1
