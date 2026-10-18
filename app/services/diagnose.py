"""跑步能力 / 训练诊断：基于 daily_record 聚合，用运动科学方法给确定性建议。

设计原则（务必遵守）：
1. 日常聚合数据（每日平均配速 / 平均心率）**不能可靠反推** VO₂max 或预测比赛成绩
   ——这些「精确」指标只在用户主动提供最近一次比赛成绩时，才用 VDOT / Riegel 公式计算；
   若用历史训练配速自动估算（estimate_best_race），必须明确标注「非比赛成绩」且精度更低；
2. 日常数据只用于它擅长的：训练负荷（ACWR，基于周跑量；CTL/ATL/TSB，基于跑量 EWMA）、
   跑量趋势、恢复信号（睡眠 / 静息心率）、配速相对趋势、心率区间（Karvonen）；
3. 所有结论都是「提示 / 估算」，标注不确定性，不冒充医学诊断，不编造医学结论。

公式出处：
- VO₂max：Daniels & Gilbert（1979）速度回归，
  ``VO₂max = -4.60 + 0.182258·v + 0.000104·v²``（v 为比赛速度 m/min）。
  仅适用于竭尽全力的比赛成绩（约 1500m ~ 全马），不适用于日常训练配速。
- 成绩预测：Riegel，``T₂ = T₁·(D₂/D₁)^1.06``。
- 训练配速：由 VO₂max 反推各强度（%VO₂max），参考 Daniels 训练体系近似值。
- 负荷比：急性:慢性负荷比（ACWR，Gabbett 等），作为受伤风险的提示信号之一。
- 最大心率：Tanaka 等（J Am Coll Cardiol 2001），``HRmax = 208 − 0.7×年龄``，
  为人群平均公式、个体误差约 ±7~10 bpm，仅作估算。
- 心率区间：Karvonen 储备心率法，目标心率 = 静息心率 + 百分比×(HRmax − 静息心率)。
- CTL/ATL/TSB：Banister 冲量-响应模型的简化应用（TrainingPeaks 等采用的
  EWMA 负荷追踪），CTL 时间常数 42 天、ATL 7 天、TSB = CTL − ATL；
  本实现以**日跑量**作为负荷输入（不含强度信息，结论只作趋势参考）。
"""

import math
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.checkin_log import CheckinLog
from ..models.daily_record import DailyRecord
from ..models.member import Member
from . import timeutil
from .checkin import is_active_day
from .cheers import format_pace

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


# ---------------------------------------------------------------------------
# 心率区间（Karvonen 储备心率法）
# ---------------------------------------------------------------------------

# 五区间模型（%HRR）：与主流可穿戴设备的 5 区模型一致。
_HR_ZONES = [
    ("Z1 热身/恢复", 0.50, 0.60, "恢复/热身强度，训练刺激有限——适合恢复跑，别指望它涨能力"),
    ("Z2 有氧基础", 0.60, 0.70, "轻松跑甜区，打有氧地基——大部分跑量应该落在这一区"),
    ("Z3 有氧节奏", 0.70, 0.80, "节奏强度，提升有氧能力——效果好但疲劳积累快，别每天都是它"),
    ("Z4 乳酸阈", 0.80, 0.90, "阈强度，高效刺激乳酸阈——每周 1~2 次足够"),
    ("Z5 最大摄氧", 0.90, 1.00, "高强度区间，刺激 VO₂max——量要少而精"),
]


def hr_max_from_age(age: int) -> int:
    """最大心率估算（Tanaka 2001：208 − 0.7×年龄）。

    人群平均公式，个体误差约 ±7~10 bpm；仅用于心率区间估算，不是精确值。
    """
    return int(round(208 - 0.7 * age))


def heart_rate_zones(age: int, resting_hr: int) -> list[tuple[str, int, int, str]]:
    """按 Karvonen 储备心率法计算五区间，返回 [(区间名, 下限bpm, 上限bpm, 说明), ...]。

    HR = 静息心率 + %HRR × (HRmax − 静息心率)，HRmax 用 Tanaka 公式估算。
    """
    hr_max = hr_max_from_age(age)
    hrr = hr_max - resting_hr
    out = []
    for label, lo_pct, hi_pct, note in _HR_ZONES:
        low = int(round(resting_hr + lo_pct * hrr))
        high = int(round(resting_hr + hi_pct * hrr))
        out.append((label, low, high, note))
    return out


def zone_for_avg_hr(zones: list, avg_hr: int) -> tuple[int, str]:
    """判断平均心率落在哪个区间，返回 (区间下标, 结论句)。

    注意：平均心率是整次运动的均值，落在某区 ≠ 全程都在该区；低于 Z2 下限视为
    「训练刺激偏弱」（回答「这次是不是白练了」）。
    """
    if not zones or not avg_hr:
        return -1, ""
    if avg_hr < zones[0][1]:
        return -1, (
            f"{avg_hr} bpm 低于 Z1 下限（{zones[0][1]} bpm）——接近散步/恢复强度，"
            "训练刺激很小（除非今天是刻意安排的恢复跑）"
        )
    for i, (label, low, high, note) in enumerate(zones):
        if low <= avg_hr <= high:
            return i, f"{avg_hr} bpm 落在 {label}（{low}~{high} bpm）——{note}"
    return -1, f"{avg_hr} bpm 超出 Z5 上限——心率读数可能异常，建议检查设备"


# ---------------------------------------------------------------------------
# 训练负荷状态（CTL / ATL / TSB，跑量 EWMA）
# ---------------------------------------------------------------------------

_CTL_DAYS = 42  # 慢性负荷时间常数（天）
_ATL_DAYS = 7  # 急性负荷时间常数（天）
_LOAD_HISTORY_DAYS = 60  # 回看窗口（覆盖 CTL 时间常数后仍有余量）


def _ewma(series: list[float], tau: int) -> float:
    """指数加权移动平均（首值作为种子）。

    CTL_t = CTL_{t-1} + (load_t − CTL_{t-1}) × (1 − e^(−1/τ))。
    """
    alpha = 1 - math.exp(-1.0 / tau)
    ctl = series[0]
    for x in series[1:]:
        ctl += alpha * (x - ctl)
    return ctl


def compute_load_status(session: Session, qq: str, today=None) -> dict | None:
    """按日跑量计算 CTL/ATL/TSB（Banister 冲量-响应模型的简化应用）。

    回看近 60 天，缺失日按 0 跑量补齐（休息日本就该是 0）。数据不足 7 天返回 None。
    返回 {ctl, atl, tsb, ratio, verdict, days}；ratio = TSB/CTL 做无量纲化，
    阈值是经验参考区间（跑量负荷不含强度信息，只作趋势参考，非诊断结论）。
    """
    today = today or timeutil.today()
    start = today - timedelta(days=_LOAD_HISTORY_DAYS - 1)
    rows = session.execute(
        select(DailyRecord.record_date, DailyRecord.distance_km).where(
            DailyRecord.member_qq == qq,
            DailyRecord.record_date >= start,
            DailyRecord.record_date <= today,
        )
    ).all()
    km_by_day = {d: (km or 0.0) for d, km in rows}
    series = [km_by_day.get(start + timedelta(days=i), 0.0) for i in range(_LOAD_HISTORY_DAYS)]
    active_days = sum(1 for x in series if x > 0)
    if active_days < 7:
        return None

    ctl = _ewma(series, _CTL_DAYS)
    atl = _ewma(series, _ATL_DAYS)
    tsb = ctl - atl
    ratio = tsb / ctl if ctl > 0 else 0.0

    if ratio >= 0.15:
        verdict = "状态新鲜，适合上强度（负荷已被消化，是安排高质量训练或比赛的好时机）"
    elif ratio >= 0.05:
        verdict = "恢复良好，可以正常训练"
    elif ratio > -0.05:
        verdict = "正常训练状态，保持当前节奏"
    elif ratio > -0.25:
        verdict = "偏疲劳，注意安排恢复日，别连续上强度"
    else:
        verdict = "明显疲劳，建议主动减量恢复，降低受伤风险"

    return {
        "ctl": round(ctl, 1),
        "atl": round(atl, 1),
        "tsb": round(tsb, 1),
        "ratio": round(ratio, 2),
        "verdict": verdict,
        "days": active_days,
    }


# ---------------------------------------------------------------------------
# 历史最快配速自动估算比赛成绩（减少输入摩擦，置信度不足则不展示）
# ---------------------------------------------------------------------------

_ESTIMATE_WINDOW_DAYS = 30  # 只取最近 30 天（当前体能状态相关；太久远的成绩不代表现在）
# 估算目标：距离窗口收紧到目标距离的 0.9~1.4 倍——外推距离越近误差越小。
# 5k 用 4.5~7 km、10k 用 9~13 km 的单次训练配速推算；窗口外（太短=冲刺配速失真、
# 太长=不是目标距离的能力）一律不采用，宁可少展示也不给置信度低的数字。
_ESTIMATE_TARGETS = [("5k", 5.0, 4.5, 7.0), ("10k", 10.0, 9.0, 13.0)]
# 配速合理范围（秒/公里）：排除 OCR/接口脏数据
_EST_PACE_MIN = 150
_EST_PACE_MAX = 600


def estimate_best_race(session: Session, qq: str, today=None) -> tuple[float, int] | None:
    """从历史记录里挑「最快单次配速」估算 5k/10k 成绩，返回 (distance_km, time_sec)。

    候选来源（均须是**单次运动**，避免多活动混合配速失真）：
      - 截图打卡明细（checkin_log：单次距离+时长算配速）；
      - 平台日明细（daily_record：仅当日 activities_count == 1 时，日均配速才等于单次配速）。
    置信度门槛（不满足返回 None，调用方不给用户反馈估算数字）：
      - 最近 30 天内、距离落在目标的 0.9~1.4 倍窗口内、配速 2:30~10:00/km。
    这是**训练配速的线性外推**，不是竭尽全力成绩：训练配速通常慢于比赛配速，
    实际成绩往往比估算更快；必须在展示层明确标注「基于历史记录的估算，非比赛成绩」。
    """
    today = today or timeutil.today()
    start = today - timedelta(days=_ESTIMATE_WINDOW_DAYS - 1)

    # 候选：(distance_km, pace_sec_per_km)；两类来源并集
    candidates: list[tuple[float, float]] = []
    logs = session.execute(
        select(CheckinLog.distance_km, CheckinLog.active_minutes).where(
            CheckinLog.member_qq == qq, CheckinLog.record_date >= start
        )
    ).all()
    for km, mins in logs:
        km = km or 0.0
        if km > 0 and (mins or 0) > 0:
            candidates.append((km, mins * 60.0 / km))
    days = session.execute(
        select(
            DailyRecord.distance_km,
            DailyRecord.avg_pace_sec_per_km,
            DailyRecord.activities_count,
        ).where(DailyRecord.member_qq == qq, DailyRecord.record_date >= start)
    ).all()
    for km, pace, count in days:
        # 仅单次运动的日均配速可用；多活动日混合了不同强度的配速，不可作为单次候选
        if (km or 0.0) > 0 and pace and (count or 0) == 1:
            candidates.append((km, float(pace)))

    for _label, target_km, lo, hi in _ESTIMATE_TARGETS:
        paces = [
            p for km, p in candidates if lo <= km <= hi and _EST_PACE_MIN <= p <= _EST_PACE_MAX
        ]
        if paces:
            best = min(paces)
            return target_km, int(round(best * target_km))
    return None


def compute_diagnosis(
    session: Session,
    qq: str,
    race: tuple[float, int] | None = None,
    race_estimated: bool = False,
) -> dict:
    """汇总诊断所需的全部指标。

    返回 dict：records / weekly / acwr / recovery / pace_trend / hr_zones /
    load_status / race（含 vo2max、predictions、paces；race_estimated=True 表示
    成绩来自历史配速估算而非比赛）。数据不足处用 None 标记，由 format 层决定是否展示。
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
        "hr_zones": None,
        "load_status": None,
        "race": None,
    }

    # 心率区间（Karvonen）：需要年龄（Tanaka 估算最大心率）+ 静息心率；
    # 平均心率取近 7 天最新一条有值的记录。
    member = session.get(Member, qq)
    if member is not None and member.age:
        resting = recovery["resting_hr"]
        if resting:
            zones = heart_rate_zones(member.age, resting)
            latest_hr = next((r.avg_hr for r in reversed(records) if (r.avg_hr or 0) > 0), 0)
            idx, verdict = zone_for_avg_hr(zones, latest_hr)
            result["hr_zones"] = {
                "age": member.age,
                "hr_max": hr_max_from_age(member.age),
                "resting_hr": resting,
                "zones": zones,
                "avg_hr": latest_hr or None,
                "zone_index": idx,
                "verdict": verdict,
            }

    # 负荷状态（CTL/ATL/TSB，跑量 EWMA）
    result["load_status"] = compute_load_status(session, qq, today)

    # 比赛成绩（仅在用户提供或历史估算时计算精确指标）
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
            "estimated": race_estimated,
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

    # 负荷状态（CTL/ATL/TSB）
    ls = result["load_status"]
    lines.append("")
    lines.append("📉 负荷状态（CTL/ATL/TSB，按日跑量 EWMA 估算）")
    if ls is None:
        lines.append("· 近 60 天有效训练不足 7 天，暂无法计算负荷状态")
    else:
        lines.append(
            f"· CTL（42 天慢性负荷）≈ {ls['ctl']} km/天，ATL（7 天急性负荷）≈ {ls['atl']} km/天"
        )
        tsb_str = f"+{ls['tsb']}" if ls["tsb"] >= 0 else str(ls["tsb"])
        lines.append(f"· TSB = {tsb_str} km —— {ls['verdict']}")
        lines.append("·（跑量负荷不含强度信息，阈值是经验参考区间，仅作趋势提示）")

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

    # 心率区间（Karvonen）
    hz = result["hr_zones"]
    lines.append("")
    lines.append("❤️ 心率区间（Karvonen 储备心率法）")
    if hz is None:
        lines.append(
            "· 设置年龄并积累静息心率数据后可计算 Z1~Z5 心率区间：发「年龄 25」设置年龄"
            "（最大心率按 Tanaka 公式估算）"
        )
    else:
        lines.append(
            f"· 按年龄 {hz['age']} 岁估算最大心率 ≈ {hz['hr_max']} bpm"
            f"（静息 {hz['resting_hr']} bpm，个体误差 ±7~10）"
        )
        for label, low, high, _note in hz["zones"]:
            lines.append(f"    {label}：{low}~{high} bpm")
        if hz["avg_hr"]:
            lines.append(f"· 最近平均心率 {hz['avg_hr']} bpm → {hz['verdict']}")
            lines.append("·（平均心率是整次运动的均值，落在某区不等于全程都在该区）")

    # 比赛成绩解锁的精确指标
    race = result["race"]
    if race:
        if race.get("estimated"):
            lines.append("")
            lines.append("🎯 有氧能力（基于历史最快配速的估算，非比赛成绩）")
            # 训练配速推算的误差远大于比赛成绩回推：给宽区间，避免误导
            lo = max(10, race["vo2max"] - 4)
            hi = race["vo2max"] + 4
            lines.append(f"· VO₂max ≈ {race['vo2max']}（参考区间 {lo}~{hi}）ml/kg/min")
        else:
            lines.append("")
            lines.append("🎯 有氧能力（基于你给的比赛成绩，估算值）")
            lines.append(f"· VO₂max ≈ {race['vo2max']} ml/kg/min（±2 量级误差，供参考）")
        lines.append("· 成绩预测：")
        for label, secs in race["predictions"]:
            lines.append(f"    {label}：{_fmt_duration(secs)}")
        lines.append("· 训练配速：")
        for label, pace in race["paces"].items():
            lines.append(f"    {label}：{format_pace(pace)}/km")
        if race.get("estimated"):
            lines.append(
                "·（由最近 30 天最快单次训练配速推算，训练配速通常慢于比赛配速，"
                "实际成绩往往比估算更快；只作能力量级参考）"
            )
        else:
            lines.append("·（基于 Riegel / VDOT 公式，仅对竭尽全力的比赛成绩有效）")
    else:
        lines.append("")
        lines.append("💡 想解锁 VO₂max / 成绩预测 / 训练配速？")
        lines.append("· 发「分析 5k 25:00」告诉我最近一次比赛成绩（距离 + 用时）即可")
        lines.append("· 历史里若有接近 5k/10k 的单次训练记录，会自动给出保守估算")

    lines.append("━━━━━━━━━━━━")
    lines.append("· 建议：80% 跑量留在轻松强度、20% 上强度（极化训练），加量每周不超过 ~10%")

    return "\n".join(lines)
