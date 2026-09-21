"""运动排行：把 daily_record 按时间范围聚合成三榜（日榜/周榜/月榜共用）。

三榜口径：
  - 运动距离榜：范围内所有记录的距离之和；
  - 爬升榜：范围内所有记录的爬升之和；
  - 单次运动距离榜：范围内单条记录的最大距离。

未绑定平台、靠截图记录（platform="manual"）的成员也纳入统计。
"""

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models.daily_record import DailyRecord
from ..models.manual_distance import ManualDistance
from ..models.member import Member

_TOP_N = 10
_MEDALS = ["🥇", "🥈", "🥉"]


def compute_range_rankings(
    start: date,
    end: date,
    session: Session | None = None,
    top_n: int = _TOP_N,
    scope: str = "day",
) -> dict:
    """聚合 [start, end) 区间的 daily_record，返回三榜。

    形如 {distance: [(昵称, 值), ...], ascent: [...], max_single: [...]}。

    scope 取值（决定未绑定成员的距离榜数据源）：
      - "day"：日榜，聚合所有 daily_record（含截图 manual 明细）；
      - "week"：周榜，未绑定成员的距离榜改用 ManualDistance.week_distance_km（本周累计），
        不依赖可能已被 retention 清理的明细；绑定成员仍按明细聚合；
      - "month"：月榜，同 "day"。
    """
    own_session = session is None
    if own_session:
        session = get_session()
    try:
        rows = session.execute(
            select(DailyRecord, Member)
            .join(Member, DailyRecord.member_qq == Member.qq, isouter=True)
            .where(DailyRecord.record_date >= start, DailyRecord.record_date < end)
        ).all()

        distance = defaultdict(float)
        ascent = defaultdict(float)
        max_single = defaultdict(float)
        active_days: dict[str, set] = defaultdict(set)
        nicknames: dict[str, str] = {}

        for rec, member in rows:
            qq = rec.member_qq
            nicknames.setdefault(qq, member.display_name if member else qq)
            distance[qq] += rec.distance_km or 0.0
            ascent[qq] += rec.ascent_meters or 0.0
            max_single[qq] = max(max_single[qq], rec.max_activity_distance_km or 0.0)
            # 运动天数：同一天只要「有数据」就记 1 天（口径同 summary.py），用 set 按日期去重
            if (rec.distance_km or 0.0) > 0 or (rec.active_minutes or 0) > 0 or (rec.calories or 0) > 0:
                active_days[qq].add(rec.record_date)

        # 周榜：未绑定成员的距离榜改用「本周累计」（ManualDistance 只存未绑定成员的累加值，
        # 绑定时会被 clear_member_records 清空，故这里取到的必是当前未绑定成员）。
        if scope == "week":
            md_rows = session.execute(
                select(ManualDistance, Member)
                .join(Member, ManualDistance.member_qq == Member.qq)
                .where(ManualDistance.week_distance_km > 0)
            ).all()
            for md, member in md_rows:
                qq = md.member_qq
                nicknames.setdefault(qq, member.display_name)
                # week_distance_km 只在「本周有新增截图」时被 add_manual_distance 重置为本周值；
                # 若 week_start 不是本周一（=start），说明该成员本周尚未记录，week_distance_km
                # 仍是上周旧值，不能计入本周 → 跳过，distance[qq] 保留 daily_record 的本周聚合。
                if md.week_start == start:
                    distance[qq] = round(md.week_distance_km, 2)

        def _rank(agg: dict) -> list[tuple[str, float]]:
            return [
                (nicknames[qq], round(v, 2))
                for qq, v in sorted(
                    ((q, val) for q, val in agg.items() if val > 0),
                    key=lambda kv: kv[1],
                    reverse=True,
                )[:top_n]
            ]

        return {
            "distance": _rank(distance),
            "ascent": _rank(ascent),
            "max_single": _rank(max_single),
            "active_days": _rank({qq: len(ds) for qq, ds in active_days.items()}),
        }
    finally:
        if own_session:
            session.close()


def _fmt_rank(items: list[tuple[str, float]], unit: str, fmt: str) -> str:
    if not items:
        return "（暂无数据）"
    lines = []
    for i, (name, val) in enumerate(items):
        medal = _MEDALS[i] if i < 3 else f"{i + 1}."
        lines.append(f"{medal} {name}  {fmt.format(val)} {unit}")
    return "\n".join(lines)


def daily_title(d: date) -> str:
    return f"🏆 今日运动排行（{d.month}月{d.day}日）"


def weekly_title(start: date, end: date) -> str:
    """end 为本周最后一天（含）。"""
    return f"🏆 本周运动排行（{start.month}月{start.day}日~{end.month}月{end.day}日）"


def monthly_title(d: date) -> str:
    return f"🏆 本月运动排行（{d.month}月）"


def format_leaderboards(rankings: dict, title: str, with_active_days: bool = False) -> str:
    """把三榜渲染为消息文本；title 为完整标题（如「🏆 今日运动排行（9月17日）」）。

    with_active_days=True 时末尾追加「运动天数排行」（仅月榜有意义——日榜天数退化为 0/1，
    周榜最多 7，均无排行价值）。
    """
    parts = [
        f"{title}\n",
        "━━━━━━━━━━━━\n",
        f"📏 运动距离排行\n{_fmt_rank(rankings.get('distance', []), 'km', '{:.2f}')}\n\n",
        f"⛰️ 爬升排行\n{_fmt_rank(rankings.get('ascent', []), 'm', '{:.0f}')}\n\n",
        f"🚀 单次运动距离排行\n{_fmt_rank(rankings.get('max_single', []), 'km', '{:.2f}')}",
    ]
    if with_active_days:
        parts.append(f"\n\n📅 运动天数排行\n{_fmt_rank(rankings.get('active_days', []), '天', '{:.0f}')}")
    return "".join(parts)
