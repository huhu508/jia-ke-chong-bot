"""鼓励语与运动数据展示文案的集中库。

把原来散落在 image_ocr.py 里、与数据无关的几句随机开场/结尾，扩成
「依据识别结果 + 时段」上下文选取的池子，避免每条回复都一个味（尴尬）。

同时提供 ``format_pace`` 给查询 / 截图 / 排行三处复用配速展示。
"""

import random

from . import timeutil

# ---------------------------------------------------------------------------
# 通用开场 / 结尾（无具体数据可用时的兜底池）
# ---------------------------------------------------------------------------

_OPENERS = [
    "收到！",
    "漂亮！",
    "可以啊！",
    "状态在线！",
    "这波稳！",
    "冲得不错！",
    "好家伙，来活儿了！",
    "数据来了，今天不白练！",
    "有内味儿了！",
    "又拿下一天！",
]

_CLOSERS = [
    "继续保持，下一次更猛！💪",
    "数据不会说谎，你在变强。",
    "给自己点个赞，明天继续！",
    "明天同一时间，继续卷！😎",
    "不积跬步无以至千里，冲！",
    "今天已经赢过昨天的自己了。",
    "歇一歇，别让身体欠债。",
]

# 时段问候（开场时偶尔带一句，让语气更自然）
_GREETINGS = [
    (5, "早！"),
    (11, "上午好！"),
    (14, "中午好！"),
    (18, "下午好！"),
    (23, "晚上好！"),
    (24, "这么晚还在动，佩服！"),
]


def _greeting() -> str:
    h = timeutil.now().hour
    for until, text in _GREETINGS:
        if h < until:
            return text
    return _GREETINGS[-1][1]


# ---------------------------------------------------------------------------
# 按指标的评价句（有对应数据时优先用，比通用开场更贴切）
# ---------------------------------------------------------------------------


def _distance_note(km: float) -> str:
    if not km or km <= 0:
        return ""
    if km >= 20:
        return f"{km:.2f} km，大佬请收下我的膝盖 🙇"
    if km >= 10:
        return f"{km:.2f} km，这距离很顶！"
    if km >= 5:
        return f"{km:.2f} km，中长距离拿捏了！"
    if km >= 2:
        return f"{km:.2f} km，动起来就是胜利！"
    return ""


def _pace_note(sec_per_km: float) -> str:
    if not sec_per_km or sec_per_km <= 0:
        return ""
    p = format_pace(sec_per_km)
    if sec_per_km <= 300:
        return f"配速 {p}，飞起来了！🦅"
    if sec_per_km <= 360:
        return f"配速 {p}，很稳的输出！"
    if sec_per_km <= 420:
        return f"配速 {p}，节奏舒服！"
    return f"配速 {p}，主打一个舒坦！"


def _ascent_note(meters: float) -> str:
    if not meters or meters <= 0:
        return ""
    if meters >= 500:
        return f"爬升 {meters:.0f} m，真·越野战士 ⛰️"
    if meters >= 200:
        return f"爬升 {meters:.0f} m，今天没少爬！"
    if meters >= 50:
        return f"还带 {meters:.0f} m 爬升，腿子辛苦了！"
    return ""


def _calorie_note(kcal: int) -> str:
    if not kcal or kcal <= 0:
        return ""
    if kcal >= 1000:
        return f"{kcal} 千卡，晚饭可以放心炫了！"
    if kcal >= 500:
        return f"{kcal} 千卡，燃脂拉满！"
    return ""


def opener(data: dict) -> str:
    """按数据选一句开场（无数据时退回通用池，偶尔带时段问候）。"""
    note = (
        _distance_note(data.get("distance_km", 0))
        or _pace_note(data.get("avg_pace_sec_per_km", 0))
        or _ascent_note(data.get("ascent_meters", 0))
        or _calorie_note(data.get("calories", 0))
    )
    if note:
        return note
    if random.random() < 0.4:
        return _greeting()
    return random.choice(_OPENERS)


def closer(data: dict) -> str:
    """结尾通用池随机一句。"""
    return random.choice(_CLOSERS)


def format_pace(sec_per_km: float) -> str:
    """配速（秒/公里）→ ``X'XX"`` 展示；无值返回空串。"""
    if not sec_per_km or sec_per_km <= 0:
        return ""
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}'{s:02d}\""
