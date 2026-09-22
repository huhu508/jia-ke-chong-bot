"""跑步能力 / 训练诊断：基于 daily_record 聚合，用运动科学方法给确定性建议。

设计原则（务必遵守）：
1. 日常聚合数据（每日平均配速 / 平均心率）**不能可靠反推** VO₂max 或预测比赛成绩
   ——这些「精确」指标只在用户主动提供最近一次比赛成绩时，才用 VDOT / Riegel 公式计算；
2. 日常数据只用于它擅长的：训练负荷（ACWR，基于周跑量）、跑量趋势、恢复信号
   （睡眠 / 静息心率）、配速相对趋势；
3. 所有结论都是「提示 / 估算」，标注不确定性，不冒充医学诊断，不编造医学结论。

公式出处：
- VO₂max：Daniels & Gilbert（1979）速度回归，
  ``VO₂max = -4.60 + 0.182258·v + 0.000104·v²``（v 为比赛速度 m/min）。
  仅适用于竭尽全力的比赛成绩（约 1500m ~ 全马），不适用于日常训练配速。
- 成绩预测：Riegel，``T₂ = T₁·(D₂/D₁)^1.06``。
- 训练配速：由 VO₂max 反推各强度（%VO₂max），参考 Daniels 训练体系近似值。
- 负荷比：急性:慢性负荷比（ACWR，Gabbett 等），作为受伤风险的提示信号之一。
"""

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.daily_record import DailyRecord
from .cheers import format_pace
from . import timeutil
from .checkin import is_active_day

# 诊断需要的最少周数（配速趋势、慢性负荷都依赖它）
_WEEKS = 4

# 常见比赛距离（km）
_RACE_DISTANCES = [("5k", 5.0), ("10k", 10.0), ("半马", 21.0975), ("全马", 42.195)]


# ---------------------------------------------------------------------------
# 比赛成绩解析（「诊断 5k 25:00」/「诊断 半马 1:45:00」）
# ---------------------------------------------------------------------------

_DIST_ALIASES = {
    "半马": 21.0975,
    "半程": 21.0975,
    "半": 21.0975,
    "全马": 42.195,
    "全程": 42.195,
    "全": 42.195,
    "马拉松": 42.195,
    "1k": 1.0,
    "3k": 3.0,
    "5k": 5.0,
    "10k": 10.0,
    "15k": 15.0,
    "21k": 21.0975,
    "42k": 42.195,
}


def _parse_distance(token: str) -> float | None:
    """解析距离 token（如 5k / 10km / 21.0975 / 半马 / 全马），返回公里数。"""
    t = token.strip().lower()
    if t in _DIST_ALIASES:
        return _DIST_ALIASES[t]
    m = re.match(r"^([\d.]+)\s*(km|k|公里|m|米)?$", t)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2) or ""
    if unit in ("m", "米"):
        return val / 1000.0
    # 数字默认按公里计（5 -> 5km）
    return val


def _parse_time(token: str) -> int | None:
    """解析成绩 token（25:00 / 1:45:00 / 50分 / 1500秒），返回秒数。"""
    t = token.strip()
    parts = t.split(":")
    if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
        secs = 0
        for p in parts:
            secs = secs * 60 + int(p)
        return secs
    m = re.match(r"^([\d.]+)\s*(分|分钟|min|秒|s|sec)?$", t, re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("秒", "s", "sec"):
        return int(round(val))
    return int(round(val * 60))  # 默认按分钟


def parse_race(text: str) -> tuple[float, int] | None:
    """从「<距离> <成绩>」解析出 (distance_km, time_sec)；失败返回 None。"""
    parts = text.strip().split()
    if len(parts) < 2:
        return None
    dist = _parse_distance(parts[0])
    if dist is None or dist <= 0:
        return None
    # 成绩优先整体解析（容忍「1:45:00」内部无空格），失败再退回第二段
    secs = _parse_time("".join(parts[1:]))
    if secs is None:
        secs = _parse_time(parts[1])
    if secs is None or secs <= 0:
        return None
    return dist, secs


# ---------------------------------------------------------------------------
# 运动科学计算（仅基于「比赛成绩」的精确指标）
# ---------------------------------------------------------------------------


def vo2max_from_race(distance_km: float, time_sec: int) -> float:
    """由竭尽全力的比赛成绩估算 VO₂max（Daniels & Gilbert 回归）。"""
    v = distance_km * 1000.0 / (time_sec / 60.0)  # m/min
    return -4.60 + 0.182258 * v + 0.000104 * v * v


def predict_race_time(distance_km: float, time_sec: int, target_km: float) -> int:
    """Riegel 公式：由已知比赛成绩预测另一距离的成绩（秒）。"""
    return int(round(time_sec * (target_km / distance_km) ** 1.06))


def _velocity_at_fraction(vo2max: float, frac: float) -> float:
    """反解 VO₂max 回归，求某 %VO₂max 强度对应的速度（m/min）。"""
    a, b = 0.000104, 0.182258
    c = -4.60 - frac * vo2max
    return (-b + (b * b - 4 * a * c) ** 0.5) / (2 * a)


# 各训练强度对应的 %VO₂max（Daniels 训练体系近似）
_PACE_ZONES = [
    ("轻松跑 E", 0.65),
    ("马拉松 M", 0.80),
    ("阈值跑 T", 0.88),
    ("间歇跑 I", 0.98),
]


def training_paces(vo2max: float) -> dict[str, float]:
    """由 VO₂max 导出各强度训练配速（秒/公里）。"""
    return {name: 60000.0 / _velocity_at_fraction(vo2max, frac) for name, frac in _PACE_ZONES}


def _fmt_duration(sec: float) -> str:
    """秒 -> ``MM:SS`` 或 ``H:MM:SS``。"""
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# 基于日常数据的确定性诊断
# ---------------------------------------------------------------------------


def _recent_records(session: Session, qq: str, days: int) -> list[DailyRecord]:
    start = timeutil.today() - timedelta(days=days - 1)
    return (
        session.execute(
            select(DailyRecord).where(
                DailyRecord.member_qq == qq,
                DailyRecord.record_date >= start,
                DailyRecord.record_date <= timeutil.today(),
            )
        )
        .scalars()
        .all()
    )


def _acwr_level(acwr: float) -> tuple[str, str]:
    """把 ACWR 映射成（等级, 建议）。仅作提示信号，非诊断结论。"""
    if acwr < 0.8:
        return "偏低", "近期负荷低于平均水平，可逐步加量（每周 +5~10% 为宜）。"
    if acwr <= 1.3:
        return "合理", "负荷平稳，处于相对安全的区间，保持节奏即可。"
    if acwr <= 1.5:
        return "略偏高", "加量偏快，注意安排恢复日，避免连续高强度。"
    return "偏高", "近期加量过猛，建议适当减量或增加恢复，降低受伤风险。"


def compute_diagnosis(session: Session, qq: str, race: tuple[float, int] | None = None) -> dict:
    """汇总诊断所需的全部指标。

    返回 dict：records / weekly / acwr / recovery / pace_trend / race（含 vo2max、
    predictions、paces）。数据不足处用 None 标记，由 format 层决定是否展示。
    """
    today = timeutil.today()
    records = _recent_records(session, qq, _WEEKS * 7)

    # 近 4 周按 7 天桶（bucket[0] 为最近一周）统计跑量 / 配速
    buckets = []
    for i in range(_WEEKS):
        end = today - timedelta(days=7 * i)
        start = end - timedelta(days=6)
        in_bucket = [r for r in records if start <= r.record_date <= end]
        dist = round(sum(r.distance_km or 0.0 for r in in_bucket), 2)
        paces = [r.avg_pace_sec_per_km for r in in_bucket if r.avg_pace_sec_per_km]
        active_days = sum(
            1 for r in in_bucket if is_active_day(r.distance_km, r.active_minutes, r.calories)
        )
        buckets.append(
            {
                "distance": dist,
                "pace": round(sum(paces) / len(paces)) if paces else None,
                "active_days": active_days,
            }
        )

    # 训练负荷：急性（近7天跑量） vs 慢性（近4周周均跑量）
    acute = buckets[0]["distance"]
    chronic_weekly = round(sum(b["distance"] for b in buckets) / _WEEKS, 2)
    acwr = round(acute / chronic_weekly, 2) if chronic_weekly > 0 else None
    acwr_verdict = _acwr_level(acwr) if acwr is not None else None

    # 恢复信号：近 7 天睡眠均值 + 静息心率变化（相对前一周）
    last7 = [r for r in records if r.record_date > today - timedelta(days=7)]
    prev7 = [
        r
        for r in records
        if today - timedelta(days=14) < r.record_date <= today - timedelta(days=7)
    ]
    sleeps = [r.sleep_hours for r in last7 if (r.sleep_hours or 0) > 0]
    hrs_now = [r.resting_hr for r in last7 if (r.resting_hr or 0) > 0]
    hrs_prev = [r.resting_hr for r in prev7 if (r.resting_hr or 0) > 0]
    recovery = {
        "sleep_avg": round(sum(sleeps) / len(sleeps), 1) if sleeps else None,
        "resting_hr": round(sum(hrs_now) / len(hrs_now)) if hrs_now else None,
        "resting_hr_delta": (
            round(sum(hrs_now) / len(hrs_now) - sum(hrs_prev) / len(hrs_prev))
            if hrs_now and hrs_prev
            else None
        ),
    }

    # 配速趋势：最近一周 vs 三周前（buckets[0]=最近 7 天，buckets[3]=三周前）
    pace_trend = None
    if buckets[0]["pace"] and buckets[3]["pace"]:
        delta = buckets[0]["pace"] - buckets[3]["pace"]
        pace_trend = {
            "latest": buckets[0]["pace"],
            "three_weeks_ago": buckets[3]["pace"],
            "delta": delta,
        }

    result = {
        "records": len(records),
        "total_distance": round(sum(b["distance"] for b in buckets), 2),
        "weekly": buckets,
        "acute": acute,
        "chronic_weekly": chronic_weekly,
        "acwr": acwr,
        "acwr_verdict": acwr_verdict,
        "recovery": recovery,
        "pace_trend": pace_trend,
        "race": None,
    }

    # 比赛成绩（仅在用户提供时计算精确指标）
    if race:
        dist, secs = race
        vo2 = vo2max_from_race(dist, secs)
        result["race"] = {
            "distance_km": dist,
            "time_sec": secs,
            "vo2max": round(vo2, 1),
            "predictions": [
                (label, predict_race_time(dist, secs, target)) for label, target in _RACE_DISTANCES
            ],
            "paces": training_paces(vo2),
        }

    return result


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _pace_trend_verdict(delta: int) -> str:
    if delta <= -10:
        return "配速在进步 📈（同距离平均配速更快了）"
    if delta >= 10:
        return "配速有所下降 📉，可能疲劳或训练结构变化，注意恢复"
    return "配速基本稳定 ➡️"


def format_diagnosis(result: dict, name: str) -> str:
    """把诊断结果渲染成群消息文本（确定性，不依赖 LLM）。"""
    lines: list[str] = [f"🏃 训练诊断 —— {name}", "━━━━━━━━━━━━"]

    weekly = result["weekly"]
    active_days_total = sum(b["active_days"] for b in weekly)

    # 跑量
    lines.append("📊 近 4 周跑量")
    lines.append(f"· 近 7 天：{result['acute']} km")
    lines.append(
        f"· 近 4 周合计：{result['total_distance']} km"
        f"（周均 {result['chronic_weekly']} km，活跃 {active_days_total} 天）"
    )

    # 负荷（ACWR）
    lines.append("")
    lines.append("⚖️ 训练负荷（ACWR，基于周跑量）")
    if result["acwr"] is None or result["chronic_weekly"] < 3:
        lines.append("· 近 4 周跑量太少，暂无法判断负荷比，先积累几周再看")
    else:
        level, advice = result["acwr_verdict"]
        lines.append(
            f"· 急性 {result['acute']} km / 慢性 {result['chronic_weekly']} km/周"
            f" = ACWR {result['acwr']}"
        )
        lines.append(f"· 结论：{level} —— {advice}")
        lines.append("·（提示：ACWR 只是受伤风险的参考信号，个体差异大，非诊断结论）")

    # 恢复信号
    rec = result["recovery"]
    lines.append("")
    lines.append("🛌 恢复信号")
    if rec["sleep_avg"] is None and rec["resting_hr"] is None:
        lines.append("· 暂无睡眠 / 静息心率数据（Garmin 需开启同步，COROS 默认有）")
    if rec["sleep_avg"] is not None:
        note = "偏低，注意保证睡眠（<7h 恢复会打折）" if rec["sleep_avg"] < 7 else "充足"
        lines.append(f"· 近 7 天平均睡眠：{rec['sleep_avg']} 小时（{note}）")
    if rec["resting_hr"] is not None:
        hr_line = f"· 静息心率：{rec['resting_hr']} bpm"
        delta = rec["resting_hr_delta"]
        if delta is not None and delta >= 5:
            hr_line += f"（较前一周 +{delta}，可能未充分恢复）"
        elif delta is not None and delta <= -3:
            hr_line += f"（较前一周 {delta}，恢复良好）"
        lines.append(hr_line)

    # 配速趋势
    pt = result["pace_trend"]
    if pt:
        lines.append("")
        lines.append("📈 配速趋势（近 4 周）")
        lines.append(
            f"· 三周前 {format_pace(pt['three_weeks_ago'])} → 最近 {format_pace(pt['latest'])}/km"
        )
        lines.append(f"· {_pace_trend_verdict(pt['delta'])}")

    # 比赛成绩解锁的精确指标
    race = result["race"]
    if race:
        lines.append("")
        lines.append("🎯 有氧能力（基于你给的比赛成绩，估算值）")
        lines.append(f"· VO₂max ≈ {race['vo2max']} ml/kg/min（±2 量级误差，供参考）")
        lines.append("· 成绩预测：")
        for label, secs in race["predictions"]:
            lines.append(f"    {label}：{_fmt_duration(secs)}")
        lines.append("· 训练配速：")
        for label, pace in race["paces"].items():
            lines.append(f"    {label}：{format_pace(pace)}/km")
        lines.append("·（基于 Riegel / VDOT 公式，仅对竭尽全力的比赛成绩有效）")
    else:
        lines.append("")
        lines.append("💡 想解锁 VO₂max / 成绩预测 / 训练配速？")
        lines.append("· 发「诊断 5k 25:00」告诉我最近一次比赛成绩（距离 + 用时）即可")

    lines.append("━━━━━━━━━━━━")
    lines.append("· 建议：80% 跑量留在轻松强度、20% 上强度（极化训练），加量每周不超过 ~10%")

    return "\n".join(lines)
