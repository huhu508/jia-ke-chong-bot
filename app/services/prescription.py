"""训练处方：把模型的「结构化建议」经本地校验后渲染成一周安排。

设计原则（与项目既有约束一致）：
- 模型**只提出建议**，不接触数据库、不写库、不参与任何统计口径；
- 模型输出必须通过本模块的确定性校验（次数、类型白名单、跑量总量、配速区间），
  不合格一律丢弃并回落到规则生成的模板——避免模型编出不切实际的计划；
- 阈值全部写死在此处并注明来源，改动需同步更新测试。

数据流：``llm.training_prescription`` → ``parse_and_validate`` → ``render``；
校验失败（或模型不可用）时用 ``template_prescription`` 兜底。
"""

from dataclasses import dataclass, field

from .cheers import format_pace

# 允许的训练类型白名单：模型只能从这些里选，避免自造术语污染输出
SESSION_KINDS = ("轻松跑", "节奏跑", "间歇跑", "长距离", "交叉训练", "休息")

# 计划合理性阈值
_MIN_SESSIONS = 2
_MAX_SESSIONS = 5
_MAX_SESSION_KM = 60.0
# 单次配速合理区间（秒/公里）：2:00 ~ 15:00，超出视为生成错误
_PACE_RANGE = (120, 900)
# 一周跑量总量相对「近 7 天跑量」的倍数区间：
# 下限 0.5 拦住「几乎不练」，上限 1.3 拦住「一周暴涨」
# （比 +10% 加量规则宽松，给恢复期/新手留余地，但足以拦住离谱的量）
_MIN_KM_RATIO = 0.5
_MAX_KM_RATIO = 1.3
# 近 7 天跑量过小时不按倍数校验，只给绝对上限
_SMALL_BASE_KM = 5.0
_SMALL_BASE_MAX_KM = 40.0
# 文案长度上限（避免刷屏）
_MAX_FOCUS_CHARS = 60
_MAX_CAUTION_CHARS = 60
_MAX_PURPOSE_CHARS = 40


@dataclass
class Session:
    """一次训练安排。distance_km 为 0 表示休息/交叉训练。"""

    kind: str
    distance_km: float = 0.0
    pace_sec_per_km: int | None = None
    purpose: str = ""


@dataclass
class Prescription:
    focus: str
    sessions: list[Session] = field(default_factory=list)
    caution: str = ""
    source: str = "model"  # model / template


def _as_float(value) -> float | None:
    """宽容转 float：拒绝 bool（模型偶发把布尔当数值），转不动返回 None。"""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_and_validate(raw: dict | None, base_km: float) -> tuple[Prescription | None, str]:
    """校验模型返回的处方，返回 ``(处方, 拒绝原因)``；原因非空即视为不可用。

    ``base_km`` 为近 7 天跑量，用作「下周总量是否离谱」的锚点。
    任何一项不通过都整体拒绝（不局部修补），由调用方回落到确定性的规则模板。
    """
    if not isinstance(raw, dict):
        return None, "模型未返回结构化结果"

    focus = str(raw.get("focus") or "").strip()
    if not focus:
        return None, "缺少本周重点"
    if len(focus) > _MAX_FOCUS_CHARS:
        return None, "本周重点过长"

    raw_sessions = raw.get("sessions")
    if not isinstance(raw_sessions, list):
        return None, "缺少训练安排"
    if not _MIN_SESSIONS <= len(raw_sessions) <= _MAX_SESSIONS:
        return None, f"训练次数应在 {_MIN_SESSIONS}~{_MAX_SESSIONS} 次"

    sessions: list[Session] = []
    for item in raw_sessions:
        if not isinstance(item, dict):
            return None, "训练安排格式错误"
        kind = str(item.get("kind") or "").strip()
        if kind not in SESSION_KINDS:
            return None, f"未知训练类型「{kind}」"

        dist = _as_float(item.get("distance_km") or 0) or 0.0
        if dist < 0 or dist > _MAX_SESSION_KM:
            return None, f"单次距离 {dist:g} km 超出合理范围"
        if kind == "休息":
            dist = 0.0

        pace: int | None = None
        raw_pace = item.get("pace_sec_per_km")
        if raw_pace is not None:
            value = _as_float(raw_pace)
            if value is None or not (_PACE_RANGE[0] <= value <= _PACE_RANGE[1]):
                return None, "配速超出合理区间"
            pace = int(round(value))

        purpose = str(item.get("purpose") or "").strip()[:_MAX_PURPOSE_CHARS]
        sessions.append(
            Session(
                kind=kind,
                distance_km=round(dist, 1),
                pace_sec_per_km=pace,
                purpose=purpose,
            )
        )

    total = sum(s.distance_km for s in sessions)
    if base_km >= _SMALL_BASE_KM:
        lo, hi = base_km * _MIN_KM_RATIO, base_km * _MAX_KM_RATIO
        if not lo <= total <= hi:
            return None, (
                f"一周总量 {total:.1f} km 偏离近 7 天的 {base_km:.1f} km 太多"
                f"（允许 {lo:.1f}~{hi:.1f} km）"
            )
    elif total > _SMALL_BASE_MAX_KM:
        return None, f"一周总量 {total:.1f} km 偏多（近 7 天仅 {base_km:.1f} km）"

    caution = str(raw.get("caution") or "").strip()[:_MAX_CAUTION_CHARS]
    return Prescription(focus=focus, sessions=sessions, caution=caution, source="model"), ""


def _pace(base_pace: float, factor: float) -> int | None:
    """按近况平均配速推算某强度的目标配速；无基准配速时返回 None（展示层省略）。"""
    if not base_pace or base_pace <= 0:
        return None
    return int(round(base_pace * factor))


def _base_pace(result: dict) -> float:
    """取最近一周的平均配速作为配速推算基准；没有则返回 0。"""
    for bucket in result.get("weekly") or []:
        pace = bucket.get("pace")
        if pace:
            return float(pace)
    return 0.0


def template_prescription(result: dict, base_km: float) -> Prescription:
    """规则生成的一周安排（模型不可用 / 校验失败时的兜底，确定性、无网络）。"""
    ls = result.get("load_status") or {}
    verdict = str(ls.get("verdict") or "")
    ratio = ls.get("ratio")
    acwr = result.get("acwr")
    pace = _base_pace(result)

    fatigued = (
        "明显疲劳" in verdict
        or (isinstance(ratio, (int, float)) and ratio < -0.25)
        or (isinstance(acwr, (int, float)) and acwr >= 1.5)
    )
    rising = isinstance(acwr, (int, float)) and acwr < 0.8

    if fatigued:
        weekly_km = max(base_km * 0.7, 6.0)
        sessions = [
            Session("轻松跑", round(weekly_km * 0.5, 1), _pace(pace, 1.15), "降负荷，保住有氧底子"),
            Session(
                "轻松跑", round(weekly_km * 0.3, 1), _pace(pace, 1.20), "保持活动量，别完全不练"
            ),
            Session("休息", 0.0, None, "主动休息，把疲劳消化掉"),
        ]
        return Prescription(
            focus="减量恢复周：先把疲劳消化掉",
            sessions=sessions,
            caution="近期负荷偏高，本周不加量、不上强度",
            source="template",
        )

    if rising:
        weekly_km = max(base_km * 1.1, 10.0)
        sessions = [
            Session("轻松跑", round(weekly_km * 0.4, 1), _pace(pace, 1.15), "重建有氧基础"),
            Session("轻松跑", round(weekly_km * 0.3, 1), _pace(pace, 1.15), "维持跑量，保持节奏"),
            Session("节奏跑", round(weekly_km * 0.3, 1), _pace(pace, 0.95), "小幅加入节奏刺激"),
        ]
        return Prescription(
            focus="渐进加量周：以轻松跑为主，小幅加点强度",
            sessions=sessions,
            caution="每周加量别超过 10%，感觉疲劳就降回来",
            source="template",
        )

    weekly_km = max(base_km, 10.0)
    sessions = [
        Session("轻松跑", round(weekly_km * 0.4, 1), _pace(pace, 1.15), "打底的有氧跑"),
        Session("节奏跑", round(weekly_km * 0.3, 1), _pace(pace, 0.95), "提升乳酸阈值"),
        Session("长距离", round(weekly_km * 0.3, 1), _pace(pace, 1.25), "拉长有氧耐力"),
    ]
    return Prescription(
        focus="维持周：保持节奏，一次质量课 + 一次长距离",
        sessions=sessions,
        caution="质量课后安排轻松日，别连续上强度",
        source="template",
    )


def render(p: Prescription, name: str) -> str:
    """把处方渲染成群消息文本。"""
    lines = [f"🗓 下周训练处方 —— {name}", "━━━━━━━━━━━━", f"🎯 本周重点：{p.focus}"]
    for s in p.sessions:
        parts = [s.kind]
        if s.distance_km:
            parts.append(f"{s.distance_km:g} km")
        if s.pace_sec_per_km:
            parts.append(f"@ {format_pace(s.pace_sec_per_km)}/km")
        line = "· " + " ".join(parts)
        if s.purpose:
            line += f" —— {s.purpose}"
        lines.append(line)
    if p.caution:
        lines.append(f"⚠️ {p.caution}")
    source = (
        "模型生成，已通过本地校验" if p.source == "model" else "规则生成（模型不可用或未通过校验）"
    )
    lines.append(
        f"·（{source}；约束：{_MIN_SESSIONS}~{_MAX_SESSIONS} 次、总量不超过近 7 天的 "
        f"{_MAX_KM_RATIO:g} 倍、配速 {format_pace(_PACE_RANGE[0])}~{format_pace(_PACE_RANGE[1])}/km）"
    )
    return "\n".join(lines)
