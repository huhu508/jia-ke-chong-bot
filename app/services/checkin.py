"""打卡体系：天数统计 / 里程碑 / 节日彩蛋 / 群抽奖 / 礼物，全数据驱动、可泛化。

统一口径（与用户确认）：
  - 打卡单位 = 天：同一成员同一天多次运动/截图只算 1 天；
  - 覆盖范围 = 绑定平台 + 截图：绑定 Garmin/COROS 当天有平台数据即算，未绑定发截图算；
  - 计数起点 = SEMESTER_START（默认 2026-09-20）：之前的记录不统计。

数据源是 CheckinDay 表（每成员每天一行，永不清理）。触发幂等靠 CheckinState 表，
重复打卡/重复查询不会重复发彩蛋。所有触发规则数据驱动，加新能力只改本文件顶部常量、
不改代码逻辑。
"""

import random
from collections import Counter
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
GIFT_QUOTA = 10  # 全群先到先得名额（满足条件先后顺序，满额后不再触发）

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


def is_active_day(distance_km, active_minutes, calories) -> bool:
    """「有运动数据」的统一口径：距离 / 时长 / 消耗任一项 >0。

    供 sync._write_record / summary / ranking / diagnose 统一复用，避免各处口径漂移
    （历史上曾出现缺 calories 判定导致漏记）。calories 一律为「运动消耗」，不含基础代谢。
    """
    return (distance_km or 0) > 0 or (active_minutes or 0) > 0 or (calories or 0) > 0


def ensure_day(qq: str, d: date, session: Session) -> bool:
    """记录一个打卡日（幂等）。d 早于计数起点时不记录。返回是否新增。

    不自行 commit——复用调用方的 session，随其 commit 落库（与 sync 写库路径一致）。
    """
    if d < counting_start():
        return False
    exists = session.execute(
        select(CheckinDay.id).where(CheckinDay.member_qq == qq, CheckinDay.record_date == d)
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


def next_milestone(total: int) -> int | None:
    """返回下一个未达成的里程碑值（全部达成返回 None）。

    供「打卡」命令展示「距下一个里程碑还差 X 天」，把被动彩蛋变成主动目标——
    里程碑彩蛋是「达成后被动触发」，这里补「达成前主动引导」，二者互补。
    """
    for m in MILESTONES:
        if total < m:
            return m
    return None


def global_total(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(CheckinDay)) or 0


def _milestone_key(m: int, qq: str) -> str:
    """里程碑触发状态 key：milestone:<天数>:<qq>。"""
    return f"milestone:{m}:{qq}"


def _lottery_key(t: int) -> str:
    """群抽奖触发状态 key：lottery:<阈值>。"""
    return f"lottery:{t}"


def _gift_prefix() -> str:
    """礼物状态 key 前缀：gift:<天数>:（用于按前缀统计已领取人数）。"""
    return f"gift:{GIFT_DAYS}:"


def _gift_key(qq: str) -> str:
    """礼物领取状态 key：gift:<天数>:<qq>。"""
    return _gift_prefix() + qq


def _festival_key(name: str, year: int, qq: str) -> str:
    """节日彩蛋触发状态 key：festival:<节日名>:<年份>:<qq>。"""
    return f"festival:{name}:{year}:{qq}"


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
        if total >= m and _mark_state(_milestone_key(m, qq), session):
            hit.append(m)
    return hit


def check_lottery(session: Session) -> int | None:
    """若全群累计打卡突破某个抽奖阈值，写状态并返回该阈值；否则 None。"""
    total = global_total(session)
    for t in sorted(LOTTERY_THRESHOLDS):
        if total >= t and _mark_state(_lottery_key(t), session):
            return t
    return None


def draw_lottery(session: Session) -> list[str]:
    """从所有打卡日中抽 LOTTERY_WINNERS 个中奖成员，多打卡多票（按天数加权）。

    用 Counter 统计每个成员的打卡天数作为权重，random.sample 的 counts 参数按权重
    无放回抽取，保证恰好抽满 LOTTERY_WINNERS 个不同成员（同一成员不重复中奖）。
    """
    rows = session.execute(select(CheckinDay.member_qq)).scalars().all()
    if not rows:
        return []
    weights = Counter(rows)  # qq -> 打卡天数（票数）
    qqs = list(weights)
    k = min(LOTTERY_WINNERS, len(qqs))
    return random.sample(qqs, k, counts=[weights[q] for q in qqs])


def match_festival(d: date, distance_km: float) -> dict | None:
    """日期命中节日且打卡距离≈对应特殊距离时返回节日 dict，否则 None（纯判定，无副作用）。"""
    for f in FESTIVALS:
        if (d.month, d.day) == f["date"] and abs(
            (distance_km or 0.0) - f["distance_km"]
        ) <= FESTIVAL_DIST_TOL:
            return f
    return None


def check_festival(qq: str, d: date, distance_km: float, session: Session) -> dict | None:
    """节日 + 特殊距离彩蛋（幂等）：日期命中且距离≈特殊距离时触发。

    与里程碑/礼物/抽奖一致走 CheckinState，每个成员每年每个节日只触发一次；
    同名节日次年（不同年份）会再次触发。调用方随自己的 session 一起 commit。
    """
    fest = match_festival(d, distance_km)
    if fest is None:
        return None
    if not _mark_state(_festival_key(fest["name"], d.year, qq), session):
        return None
    return fest


def gift_claims(session: Session) -> int:
    """当前已领取第 GIFT_DAYS 天礼物的人数。"""
    return (
        session.scalar(
            select(func.count())
            .select_from(CheckinState)
            .where(CheckinState.key.like(_gift_prefix() + "%"))
        )
        or 0
    )


def claim_gift(qq: str, session: Session) -> bool:
    """尝试领取第 GIFT_DAYS 天礼物（先到先得）。返回是否为首次领取。"""
    return _mark_state(_gift_key(qq), session)


def auto_gift(qq: str, total: int, session: Session) -> int | None:
    """累计打卡首次达到 GIFT_DAYS 且名额未满时自动领取礼物，返回领取序位（1 起）；否则 None。

    幂等由 CheckinState（gift:<GIFT_DAYS>:<qq>）保证，重复触发不会重复领取。
    供截图/今日路径在成员跨过 GIFT_DAYS 时自动触发，无需成员手动发「领礼物」。
    """
    if total < GIFT_DAYS:
        return None
    if session.get(CheckinState, _gift_key(qq)) is not None:
        return None
    before = gift_claims(session)
    if before >= GIFT_QUOTA:
        return None
    claim_gift(qq, session)
    return before + 1
