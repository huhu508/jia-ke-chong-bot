"""从 OCR 结果中提取运动指标。

主路径 ``parse_activity_from_boxes`` 用**框坐标做「标签—数值」空间关联**，
并支持不同运动 App 的字段差异（COROS / Sigma / Keep / 苹果健身等）：

- 标签定位：找字段标签（中英双语）→ 在其上方/下方/左侧邻域就近取数值；
- 单位定位：无标签时，找单位框（km / kcal / min）反查数值；
- 合并框：数值与单位同框（如 ``7.35KM``、``603KCAL``）直接提取。

纯文本版 ``parse_activity`` 保留作为无坐标场景的兜底。
"""

import re


def _normalize_number(raw: str) -> str:
    """把 OCR 数字串归一成可 ``float()`` / ``int()`` 的字符串。

    逗号有两种含义，需区分（部分 App 更新字体后小数点被渲染成逗号，见 Sigma「6,23」）：
      - 千分位「1,234」→「1234」：已有小数点，或逗号后恰好 3 位数字；
      - 小数逗号「6,23」→「6.23」：逗号后 1~2 位数字（欧式小数写法）。
    """
    raw = raw.strip()
    if "," not in raw:
        return raw
    if "." in raw:
        # 已有小数点，逗号只能是千分位：1,234.5 → 1234.5
        return raw.replace(",", "")
    last = raw.rsplit(",", 1)[1]
    if re.fullmatch(r"\d{3}", last):
        return raw.replace(",", "")  # 千分位：1,234 → 1234
    return raw.replace(",", ".")  # 小数逗号：6,23 → 6.23


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
        data["distance_km"] = float(_normalize_number(v))

    v = _nearby_number(text, ["步数", "steps"])
    if v is not None:
        data["steps"] = int(_normalize_number(v))

    v = _nearby_number(
        text, ["消耗", "卡路里", "千卡", "干卡", "大卡", "kcal", "kilocalorie", "calorie"]
    )
    if v is not None:
        data["calories"] = int(_normalize_number(v))

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
        data["ascent_meters"] = float(_normalize_number(v))

    seconds = _nearby_pace(text, ["配速", "平均配速", "pace", "average pace"])
    if seconds is not None:
        data["avg_pace_sec_per_km"] = seconds

    v = _nearby_number(text, ["平均心率", "avg hr", "average hr", "心率"])
    if v is not None:
        data["avg_hr"] = int(_normalize_number(v))

    return data


# ---------------------------------------------------------------------------
# 基于坐标的解析（主路径）
# ---------------------------------------------------------------------------

# 字段规格：labels=标签关键词（小写/中文），units=单位（小写），kind=提取方式。
_FIELD_SPECS = [
    {
        "key": "distance_km",
        "labels": ["距离", "总公里", "里程", "公里数", "总里程", "distance"],
        # 合并框/单位框匹配的单位（不含 m/米，避免把「150米」爬升误当距离）；
        # 值框换算见 factors（含 m/mi），如「1500 m」→1.5 km、「5 mi」→8.05 km。
        "units": ["km", "公里", "千米", "mi", "英里"],
        "factors": {
            "km": 1.0,
            "公里": 1.0,
            "千米": 1.0,
            "mi": 1.609,
            "英里": 1.609,
            "m": 0.001,
            "米": 0.001,
        },
        "kind": "decimal",
        "convert": lambda v: float(_normalize_number(v)),
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
        "convert": lambda v: int(_normalize_number(v)),
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
        "convert": lambda v: int(_normalize_number(v)),
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
        "convert": lambda v: float(_normalize_number(v)),
    },
    {
        "key": "avg_pace_sec_per_km",
        "labels": ["平均配速", "配速", "pace", "average pace", "avg pace"],
        "units": ["/km", "min/km"],
        "kind": "pace",
        "convert": lambda v: int(v),
    },
    {
        "key": "avg_hr",
        "labels": ["平均心率", "avg hr", "average hr", "average heart rate", "心率"],
        "units": ["bpm"],
        "kind": "int",
        "convert": lambda v: int(_normalize_number(v)),
    },
]

# 数值提取时排除含这些字符的框（时间「40:16」、配速「5'02"」、温度「24°℃」、湿度「48%」）。
_NUMBER_EXCLUDE = re.compile(r"[:'\"°℃%]")

# 短标签子串陷阱：如 avg_hr 的「心率」、avg_pace 的「配速」这类短兜底标签，
# 会误匹配「最大心率」「最佳配速」「静息心率」等非均值字段的框。
# 框文本命中这些修饰词时跳过该框（这些修饰词只会出现在 max/rest 等非目标字段）。
_QUALIFIER_WORDS = (
    "最大",
    "最高",
    "最低",
    "最佳",
    "最快",
    "最慢",
    "峰值",
    "静息",
    "静止",
    "区间",
    "目标",
    "剩余",
)

# 食物等效 / 非运动数值标记：Sigma 等 App 会在「消耗大卡」旁附「=1.4 个苹果」这类
# 「相当于 N 个食物」的趣味数据。OCR/视觉偶发把它误读成消耗值（如「=1.4个」→ 1 千卡），
# 含这些标记的框一律不作为数值候选，避免「6 km 只消耗 1 千卡」的离谱结果。
_FOOD_EQUIVALENT_MARKERS = (
    # 等价符号/词
    "≈",
    "≒",
    "～",
    "~",
    "=",
    "约等于",
    "相当于",
    "等效",
    "约合",
    # 食物量词（运动指标框从不带这类量词）
    "个",
    "份",
    "碗",
    "杯",
    "根",
    "片",
    "块",
    "勺",
    "盘",
    "只",
    # 常见食物名
    "苹果",
    "香蕉",
    "米饭",
    "汉堡",
    "鸡蛋",
    "面包",
    "披萨",
    "蛋糕",
    "饼干",
    "巧克力",
    "可乐",
    "奶茶",
    "鸡胸",
    "牛肉",
    "薯条",
    "坚果",
    "酸奶",
    "牛奶",
)


def _has_food_marker(text: str) -> bool:
    """判断框文本是否含「食物等效」类标记（如「=1.4个」「≈2 碗米饭」）。"""
    return any(w in text for w in _FOOD_EQUIVALENT_MARKERS)


def _extract_number(text: str, allow_decimal: bool = True):
    """从框文本 search 提取数字串（排除时间/配速/温度/食物等效），返回字符串或 None。"""
    t = text.strip()
    if _NUMBER_EXCLUDE.search(t) or _has_food_marker(t):
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
        return int(_normalize_number(m.group(1)))
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
    return _make_scaled_extractor(spec)


def _make_scaled_extractor(spec):
    """decimal/int 字段提取器：提数字，识别单位并按 spec.factors 换算（返回字符串供 convert 归一）。

    仅当 factors 里命中非 1.0 的单位时才换算（如距离「1500 m」→1.5）；未命中/无 factors
    时原样返回数字串，行为与旧 _extract_number 一致。
    """
    kind = spec["kind"]
    factors = spec.get("factors", {})

    def _extract(text: str):
        t = text.strip()
        if _NUMBER_EXCLUDE.search(t) or _has_food_marker(t):
            return None
        pat = r"\d[\d,]*(?:\.\d+)?" if kind == "decimal" else r"\d[\d,]*"
        m = re.search(pat, t)
        if not m:
            return None
        raw = m.group(0)
        factor = 1.0
        for unit, f in sorted(factors.items(), key=lambda kv: -len(kv[0])):
            if re.search(
                r"\d[\d,]*(?:\.\d+)?\s*" + re.escape(unit) + r"(?![A-Za-z一-鿿])",
                t,
                re.IGNORECASE,
            ):
                factor = f
                break
        if factor == 1.0:
            return raw
        val = float(_normalize_number(raw)) * factor
        return f"{val:g}" if kind == "decimal" else str(int(round(val)))

    return _extract


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
      - 右侧同行（标签在左、值在右，如「距离 5.2」分框）
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
            (abs(dy) <= 40 and -350 <= dx <= -10)  # 左侧同行（单位在值右）
            or (abs(dy) <= 40 and 10 <= dx <= 350)  # 右侧同行（标签在左、值在右）
            or (
                -240 <= dy <= -15 and abs(dx) <= 350
            )  # 上方（含左上斜角：大字在单位左上，水平可偏 200+）
            or (15 <= dy <= 90 and abs(dx) <= 350)  # 下方
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
            t = it["text"].strip().lower()
            if kw not in t:
                continue
            # 短标签子串陷阱：避免「心率」误匹配「最大心率」等带修饰词的框
            if any(q in t for q in _QUALIFIER_WORDS):
                continue
            # 标签框自身含数字 → 是「距离 6.23 公里」「海拔爬升为 0」这类描述/合并句，
            # 不是纯标签。当作锚点会把相邻句的数值交叉关联（距离取到爬升的 0、爬升取到
            # 距离的 6.23）。真正的纯标签（距离/配速/心率…）从不含数字，跳过安全。
            if re.search(r"\d", t):
                continue
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


# ---------------------------------------------------------------------------
# 识别结果合理性校验（落库前的最后一道防线）
# ---------------------------------------------------------------------------

# 单次运动各字段的合理范围（跑步口径）。OCR 偶发把步数 / 卡路里 / 统计页总量
# 误识别成某字段，超范围的值视为识别错误。距离是核心指标，超上限整体拒绝；
# 其余字段各自校验，超范围仅丢弃该字段。
_VALID_RANGES = {
    "distance_km": (0.05, 100.0),  # 单次 50 米 ~ 100 公里（百公里越野上限）
    "avg_pace_sec_per_km": (120, 900),  # 2:00 ~ 15:00 /km
    "avg_hr": (40, 220),  # bpm
    "active_minutes": (1, 1440),  # 1 分钟 ~ 24 小时
    "calories": (0, 20000),  # 千卡（百公里越野可上万）
    "ascent_meters": (0, 10000),  # 米
    "steps": (0, 200000),  # 步（百公里约 12 万步）
    "sleep_hours": (0, 24),  # 小时
}

# 距离 + 时长交叉校验的速度上限（km/h），超过即「距离/时长」至少一个识别错。
_MAX_SPEED_KMH = 30.0

# 卡路里与距离的合理比值区间（千卡/公里）。跑步/骑行/徒步等每公里消耗在此量级：
# 低于下限多为「食物等效」数被误读成消耗（如 6 km 读成 1 千卡 = 0.16 千卡/km），
# 高于上限多为把整日/汇总总量误读成单次消耗。超区间仅丢弃 calories（非核心字段）。
_MIN_KCAL_PER_KM = 10.0
_MAX_KCAL_PER_KM = 400.0


def sanitize_activity(data: dict) -> tuple[dict, str | None]:
    """对 OCR 识别结果做合理性校验，返回 ``(清洗后的数据, 拒绝原因或 None)``。

    距离是核心指标：
      - 超出单次运动合理范围上限（如把月汇总总量 / 步数当成距离）→ 整体拒绝；
      - 距离与时长推出的速度超出人类跑步极限 → 温和丢弃时长、保留距离（时长更易 OCR 误读）；
      - 距离过小 / 为零 → 仅丢弃该字段（可能是纯步数/心率截图），其余字段照常。
    其余字段超范围仅丢弃该字段，不拖累整张截图。
    """
    if not data:
        return {}, None

    cleaned = dict(data)
    reject: str | None = None

    dist = cleaned.get("distance_km")
    if dist is not None:
        lo, hi = _VALID_RANGES["distance_km"]
        if dist > hi:
            reject = f"识别出的距离 {dist:g} km 超出单次运动合理范围上限 {hi:g} km，疑似识别错误"
        elif dist < lo:
            # 距离过小/为零：丢弃该字段，其余字段照常（不整体拒绝）
            cleaned.pop("distance_km", None)
            dist = None

    # 交叉校验：距离与时长同时存在时，速度不能超过人类跑步极限。
    # 距离是核心指标且已通过范围校验，超限更可能是时长 OCR 误读（如「1:30」被读成 1 分钟），
    # 故温和丢弃时长、保留距离，而非整体拒绝（否则合法「5.2km / 1h30m」会被整图拒收）。
    if reject is None and dist is not None and cleaned.get("active_minutes"):
        speed_kmh = dist / (cleaned["active_minutes"] / 60.0)
        if speed_kmh > _MAX_SPEED_KMH:
            cleaned.pop("active_minutes", None)

    # 其余字段超范围仅丢弃
    for key, (lo, hi) in _VALID_RANGES.items():
        if key == "distance_km":
            continue
        v = cleaned.get(key)
        if v is not None and not (lo <= v <= hi):
            cleaned.pop(key, None)

    # 卡路里 vs 距离交叉校验：仅当两者都存在时比较每公里消耗是否落在合理区间，
    # 兜底拦截「食物等效」数被误读成消耗这类离谱值（OCR/视觉都可能犯）。只丢弃
    # calories，不拖累距离等核心字段。
    if cleaned.get("calories") is not None and cleaned.get("distance_km") is not None:
        kcal_per_km = cleaned["calories"] / cleaned["distance_km"]
        if not (_MIN_KCAL_PER_KM <= kcal_per_km <= _MAX_KCAL_PER_KM):
            cleaned.pop("calories", None)

    return cleaned, reject


# ---------------------------------------------------------------------------
# 并行识别比对：本地 OCR 与视觉模型两套结果逐字段比对，找出分歧字段
# ---------------------------------------------------------------------------

# 各字段「相对误差」阈值：超过即判为分歧，触发视觉模型结合 OCR 结果二次看图。
# 核心指标（距离/配速）与整数类（步数/心率）从严 5%；卡路里/时长/爬升等 OCR 本身
# 更易误读的字段放宽到 10%，避免无谓多一次视觉调用。
_FIELD_CONFLICT_RATIO = {
    "distance_km": 0.05,
    "avg_pace_sec_per_km": 0.05,
    "steps": 0.05,
    "avg_hr": 0.05,
    "calories": 0.10,
    "active_minutes": 0.10,
    "ascent_meters": 0.10,
    "sleep_hours": 0.10,
}


def find_field_conflicts(a: dict, b: dict) -> list[str]:
    """比对两套已解析字段 dict，返回「两侧都有值且相对误差超阈值」的分歧字段名列表。

    只在同一字段两侧都存在且值有意义时才算分歧；只有一侧有值不算（走缺失补齐逻辑）。
    供「本地 OCR 与视觉模型并行识别」后判断是否需要视觉模型二次看图给出确定值。
    """
    conflicts: list[str] = []
    for key, ratio in _FIELD_CONFLICT_RATIO.items():
        va = a.get(key)
        vb = b.get(key)
        if va is None or vb is None:
            continue
        try:
            va = float(va)
            vb = float(vb)
        except (ValueError, TypeError):
            continue
        denom = max(abs(va), abs(vb), 1e-9)
        if abs(va - vb) / denom > ratio:
            conflicts.append(key)
    return conflicts
