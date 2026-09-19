"""从 OCR 结果中提取运动指标。

主路径 ``parse_activity_from_boxes`` 用**框坐标做「标签—数值」空间关联**，
并支持不同运动 App 的字段差异（COROS / Sigma / Keep / 苹果健身等）：

- 标签定位：找字段标签（中英双语）→ 在其上方/下方/左侧邻域就近取数值；
- 单位定位：无标签时，找单位框（km / kcal / min）反查数值；
- 合并框：数值与单位同框（如 ``7.35KM``、``603KCAL``）直接提取。

纯文本版 ``parse_activity`` 保留作为无坐标场景的兜底。
"""

import re

# ---------------------------------------------------------------------------
# 纯文本解析（兜底）
# ---------------------------------------------------------------------------


def _nearby_number(text: str, keywords, max_window: int = 20):
    """在任一关键词之后 max_window 字符内取第一个数字串，返回字符串或 None。"""
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            tail = text[m.end() : m.end() + max_window]
            nm = re.search(r"\d[\d,]*(?:\.\d+)?", tail)
            if nm:
                return nm.group(0)
    return None


# 时长 pattern：顺序即优先级（三冒号先于单冒号，避免「1:24:00」被单冒号截断）。
_DURATION_PATTERNS = [
    (r"(\d+):(\d{1,2}):(\d{1,2})", lambda m: int(m.group(1)) * 60 + int(m.group(2))),  # 时:分:秒
    (
        r"(\d+):(\d{1,2})",
        lambda m: int(m.group(1)),
    ),  # 分:秒（运动时长 <1h 时 COROS 显示「40:16」= 40分16秒）
    (r"(\d+)\s*小时\s*(\d+)\s*分", lambda m: int(m.group(1)) * 60 + int(m.group(2))),  # X小时Y分
    (
        r"(\d+)\s*[Hh]\s*(\d+)\s*[Mm][Ii][Nn]",
        lambda m: int(m.group(1)) * 60 + int(m.group(2)),
    ),  # Xh Ymin
    (r"(\d+)\s*(?:分钟|[Mm][Ii][Nn])", lambda m: int(m.group(1))),  # X分钟 / X min
]


def _match_duration_minutes(text: str):
    """把**单个框文本**解析成分钟数（fullmatch）；解析不出返回 None。"""
    t = text.strip()
    for pat, fn in _DURATION_PATTERNS:
        m = re.fullmatch(pat, t)
        if m:
            return fn(m)
    return None


def _nearby_duration(text: str, keywords, max_window: int = 30):
    """在关键词之后提取时长，返回分钟数；识别不出返回 None。"""
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            tail = text[m.end() : m.end() + max_window]
            for pat, fn in _DURATION_PATTERNS:
                m = re.search(pat, tail)
                if m:
                    return fn(m)
    return None


def _nearby_pace(text: str, keywords, max_window: int = 30):
    """在关键词之后提取配速（秒/公里），识别不出返回 None。"""
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            tail = text[m.end() : m.end() + max_window]
            pm = re.search(r"(\d+)\s*['′:]\s*(\d{1,2})", tail)
            if pm:
                return int(pm.group(1)) * 60 + int(pm.group(2))
    return None


def parse_activity(text: str) -> dict:
    """纯文本提取运动指标（兜底），返回 {字段: 值}，缺省字段不出现。"""
    if not text:
        return {}
    data: dict = {}

    v = _nearby_number(text, ["距离", "总公里", "里程", "distance"])
    if v is None:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:公里|千米|[Kk][Mm])\b", text)
        if m:
            v = m.group(1)
    if v is not None:
        data["distance_km"] = float(v.replace(",", ""))

    v = _nearby_number(text, ["步数", "steps"])
    if v is not None:
        data["steps"] = int(v.replace(",", ""))

    v = _nearby_number(
        text, ["消耗", "卡路里", "千卡", "干卡", "大卡", "kcal", "kilocalorie", "calorie"]
    )
    if v is not None:
        data["calories"] = int(v.replace(",", ""))

    minutes = _nearby_duration(
        text, ["运动时长", "运动时间", "时长", "时间", "duration", "workout time"]
    )
    if minutes is not None:
        data["active_minutes"] = minutes

    minutes = _nearby_duration(text, ["睡眠", "sleep"])
    if minutes is not None:
        data["sleep_hours"] = round(minutes / 60, 2)

    v = _nearby_number(
        text, ["爬升", "累计爬升", "爬升高度", "elevation gain", "elevation", "ascent", "climb"]
    )
    if v is not None:
        data["ascent_meters"] = float(v.replace(",", ""))

    seconds = _nearby_pace(text, ["配速", "平均配速", "pace", "average pace"])
    if seconds is not None:
        data["avg_pace_sec_per_km"] = seconds

    v = _nearby_number(text, ["平均心率", "avg hr", "average hr", "心率"])
    if v is not None:
        data["avg_hr"] = int(v.replace(",", ""))

    return data


# ---------------------------------------------------------------------------
# 基于坐标的解析（主路径）
# ---------------------------------------------------------------------------

# 字段规格：labels=标签关键词（小写/中文），units=单位（小写），kind=提取方式。
_FIELD_SPECS = [
    {
        "key": "distance_km",
        "labels": ["距离", "总公里", "里程", "公里数", "总里程", "distance"],
        "units": ["km", "公里", "千米"],
        "kind": "decimal",
        "convert": lambda v: float(v.replace(",", "")),
    },
    {
        "key": "calories",
        "labels": [
            "total kilocalorie",
            "总消耗",
            "总卡路里",
            "消耗",
            "卡路里",
            "千卡",
            "干卡",
            "大卡",
            "消耗大卡",
            "kcal",
            "kilocalorie",
            "kilocalories",
            "calorie",
            "calories",
            "energy",
        ],
        "units": ["kcal", "千卡", "大卡", "干卡"],
        "kind": "int",
        "convert": lambda v: int(v.replace(",", "")),
    },
    {
        "key": "active_minutes",
        "labels": [
            "运动时长",
            "运动时间",
            "时长",
            "活动时间",
            "总时长",
            "workout time",
            "duration",
            "elapsed time",
        ],
        "units": ["min", "分钟", "mins"],
        "kind": "duration",
        "convert": lambda v: int(v),
    },
    {
        "key": "steps",
        "labels": ["步数", "steps", "step count", "total steps"],
        "units": [],
        "kind": "int",
        "convert": lambda v: int(v.replace(",", "")),
    },
    {
        "key": "sleep_hours",
        "labels": ["睡眠", "睡眠时长", "sleep"],
        "units": [],
        "kind": "duration",
        "convert": lambda v: round(v / 60, 2),
    },
    {
        "key": "ascent_meters",
        "labels": [
            "爬升",
            "累计爬升",
            "爬升高度",
            "总爬升",
            "elevation gain",
            "elevation",
            "ascent",
        ],
        "units": ["m", "米"],
        "kind": "decimal",
        "convert": lambda v: float(v.replace(",", "")),
    },
    {
        "key": "avg_pace_sec_per_km",
        "labels": ["配速", "平均配速", "pace", "average pace", "avg pace"],
        "units": ["/km", "min/km"],
        "kind": "pace",
        "convert": lambda v: int(v),
    },
    {
        "key": "avg_hr",
        "labels": ["平均心率", "avg hr", "average hr", "average heart rate", "心率"],
        "units": ["bpm"],
        "kind": "int",
        "convert": lambda v: int(v.replace(",", "")),
    },
]

# 数值提取时排除含这些字符的框（时间「40:16」、配速「5'02"」、温度「24°℃」、湿度「48%」）。
_NUMBER_EXCLUDE = re.compile(r"[:'\"°℃%]")


def _extract_number(text: str, allow_decimal: bool = True):
    """从框文本 search 提取数字串（排除时间/配速/温度），返回字符串或 None。"""
    t = text.strip()
    if _NUMBER_EXCLUDE.search(t):
        return None
    pat = r"\d[\d,]*(?:\.\d+)?" if allow_decimal else r"\d[\d,]*"
    m = re.search(pat, t)
    return m.group(0) if m else None


def _extract_duration(text: str):
    """从框文本提取时长（分钟），返回 int 或 None。"""
    t = text.strip()
    # 冒号格式：时:分:秒 或 分:秒
    m = re.search(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", t)
    if m:
        if m.group(3) is not None:
            return int(m.group(1)) * 60 + int(m.group(2))
        return int(m.group(1))
    # 文字/单位格式
    for pat, fn in _DURATION_PATTERNS:
        m = re.search(pat, t)
        if m:
            return fn(m)
    # 纯数字（配合「min/分钟」单位框，如 Keep 顶部「112」）
    m = re.fullmatch(r"(\d[\d,]*)", t)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _extract_pace(text: str):
    """从框文本提取配速（秒/公里）。支持 5'02" / 5:02 / 6.5 等写法。"""
    t = text.strip()
    if not t:
        return None
    m = re.search(r"(\d+)\s*['′:]\s*(\d{1,2})\s*[\"″]?", t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)", t)
    if m:
        return int(round(float(m.group(1)) * 60))
    return None


def _make_extractor(spec):
    kind = spec["kind"]
    if kind == "duration":
        return _extract_duration
    if kind == "pace":
        return _extract_pace
    return lambda t: _extract_number(t, allow_decimal=(kind == "decimal"))


def _iter_items(result) -> list:
    """把 OCR result 规范成带坐标的 dict 列表（按文本非空过滤）。"""
    items = []
    for box, text, _score in result:
        t = str(text).strip()
        if not t:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append(
            {
                "text": t,
                "cx": sum(xs) / len(xs),
                "cy": sum(ys) / len(ys),
            }
        )
    return items


def _value_near(items, anchor, extractor):
    """在 anchor 框附近找能提取出值的框，返回提取出的值或 None。

    候选框与 anchor 的空间关系：
      - 上方（COROS/Sigma：大数字在上、标签在下）
      - 下方（苹果健身：标签在上、值在下）
      - 左侧同行（单位在值右，如「15.02 km」分框）
    多个候选取离 anchor 最近的。
    """
    best = None
    best_dist = 1e9
    for it in items:
        if it is anchor:
            continue
        v = extractor(it["text"])
        if v is None:
            continue
        dx = it["cx"] - anchor["cx"]
        dy = it["cy"] - anchor["cy"]
        ok = (
            (abs(dy) <= 40 and -350 <= dx <= -10)  # 左侧同行
            or (-240 <= dy <= -15 and abs(dx) <= 150)  # 上方
            or (15 <= dy <= 90 and abs(dx) <= 150)  # 下方
        )
        if not ok:
            continue
        dist = abs(dy) + abs(dx) * 0.3
        if dist < best_dist:
            best_dist = dist
            best = v
    return best


def _has_unit_token(text: str, units) -> bool:
    """判断框文本是否以「数字+单位」形式包含某个单位（整词匹配）。

    按单位长度降序匹配，并在单位后加「不得紧跟字母/汉字」的边界，避免：
      - 爬升单位「m」误匹配「km」（``"m" in "km"`` 为真的子串陷阱）；
      - 爬升单位「米」误匹配「千米 / 公里」。
    只在合并框（数字与单位同框）定位时使用；独立单位框走精确匹配，不受影响。
    """
    for u in sorted(units, key=len, reverse=True):
        if re.search(
            r"\d[\d,]*(?:\.\d+)?\s*" + re.escape(u) + r"(?![A-Za-z一-鿿])",
            text,
            re.IGNORECASE,
        ):
            return True
    return False


def _extract_field(items, spec):
    extractor = _make_extractor(spec)
    labels = spec["labels"]
    units = spec["units"]

    # 1) 标签定位（labels 顺序即优先级，更具体的标签排前面）
    for kw in labels:
        for it in items:
            if kw in it["text"].lower():
                v = _value_near(items, it, extractor)
                if v is not None:
                    return v

    # 2) 单位框定位（「km」「kcal」「min」等独立单位框）
    for it in items:
        if it["text"].strip().lower() in units:
            v = _value_near(items, it, extractor)
            if v is not None:
                return v

    # 3) 合并框（数字与单位同框，如「7.35KM」「603KCAL」）
    for it in items:
        t = it["text"].strip()
        if t[:1].isdigit() and _has_unit_token(t, units):
            v = extractor(t)
            if v is not None:
                return v

    return None


def parse_activity_from_boxes(result) -> dict:
    """基于框坐标提取运动指标，返回 {字段: 值}，缺省字段不出现。"""
    if not result:
        return {}
    items = _iter_items(result)
    data: dict = {}
    for spec in _FIELD_SPECS:
        v = _extract_field(items, spec)
        if v is not None:
            try:
                data[spec["key"]] = spec["convert"](v)
            except (ValueError, TypeError):
                continue
    return data


# ---------------------------------------------------------------------------
# 页面类型识别：区分「单次运动详情」与「统计/列表」页
# ---------------------------------------------------------------------------

# 统计/列表页的标题或入口关键词（强信号）
_SUMMARY_TITLE_KEYWORDS = (
    "筛选",
    "历史记录",
    "运动记录",
    "活动记录",
    "月报",
    "周报",
    "年报",
    "数据总览",
)

# 弱信号标题词：单字「统计」也出现在佳明单次详情页底部的 tab（概述/统计/计圈/图表），
# 单独出现不足以判为统计页，降为 1 分，需配合其他强信号（「N 次运动」、多条活动条目等）。
_WEAK_TITLE_KEYWORDS = ("统计",)


def detect_page_kind(text: str) -> str:
    """判断截图是「单次运动详情」还是「统计/列表」页。

    统计/列表页（月汇总、活动列表、历史记录等）展示的是**多条**运动的聚合，
    不适合作为「一次运动」写入，返回 ``"summary"``；否则返回 ``"activity"``。

    采用打分制（阈值 3），基于 OCR 纯文本的强信号，避免误伤单条详情：
      1. 标题/入口关键词（筛选、历史记录、月报…）—— 每条 3 分；
      1b. 弱信号标题词「统计」（佳明单次详情页底部也有此 tab）—— 仅 1 分；
      2. 汇总计数「N 次运动 / 训练 / 锻炼」—— 3 分；
      3. 多条活动条目（≥2 个「星期X/周X」或 ≥3 个 km 距离值）—— 各 2 分；
      4. 年月头（如「2026年9月」）—— 弱信号 1 分。
    """
    t = (text or "").strip()
    if not t:
        return "activity"

    score = 0
    # 1) 标题/入口关键词（强信号）
    if any(kw in t for kw in _SUMMARY_TITLE_KEYWORDS):
        score += 3
    # 1b) 弱信号标题词（如佳明单次详情页底部的「统计」tab），单独不足以判统计页
    if any(kw in t for kw in _WEAK_TITLE_KEYWORDS):
        score += 1
    # 2) 汇总计数：N 次运动 / N 次训练 / N 次锻炼
    if re.search(r"\d+\s*次\s*(?:运动|训练|锻炼)", t):
        score += 3
    # 3) 多条活动条目
    if len(re.findall(r"星期[一二三四五六日天]|周[一二三四五六日天]", t)) >= 2:
        score += 2
    if len(re.findall(r"\d+(?:\.\d+)?\s*km\b", t, re.IGNORECASE)) >= 3:
        score += 2
    # 4) 年月头（弱信号）
    if re.search(r"\d{4}\s*年\s*\d{1,2}\s*月", t):
        score += 1

    return "summary" if score >= 3 else "activity"
