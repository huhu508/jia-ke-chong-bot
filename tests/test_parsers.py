"""页面类型识别 + 文本解析 + 合理性校验的纯函数测试。"""

from app.services.parsers import (
    detect_page_kind,
    find_field_conflicts,
    parse_activity,
    parse_activity_from_boxes,
    sanitize_activity,
)


def test_detect_activity_detail():
    text = "距离 8.00 km\n平均配速 5:02 /km\n平均心率 160 bpm"
    assert detect_page_kind(text) == "activity"


def test_detect_summary_via_count():
    # 「N 次运动」强信号（3 分）→ summary
    assert detect_page_kind("本月 12 次运动，累计 120 km") == "summary"


def test_detect_summary_via_title():
    # 标题词「历史记录」强信号（3 分）→ summary
    assert detect_page_kind("历史记录\n1. 5km\n2. 10km") == "summary"


def test_detect_activity_two_km_only():
    # 只有 2 个 km 值、无标题/计数，分数不足阈值 → activity
    assert detect_page_kind("跑步 8.00 km，骑行 20.00 km，配速 5:02") == "activity"


def test_parse_activity_fields():
    text = "距离 5.00 公里\n消耗 400 千卡\n步数 8000\n平均配速 6:00\n平均心率 140"
    data = parse_activity(text)
    assert data["distance_km"] == 5.0
    assert data["calories"] == 400
    assert data["steps"] == 8000
    assert data["avg_pace_sec_per_km"] == 360  # 6:00
    assert data["avg_hr"] == 140


def test_sanitize_ok():
    data = {"distance_km": 8.0, "active_minutes": 40, "avg_hr": 150}
    cleaned, reject = sanitize_activity(data)
    assert reject is None
    assert cleaned == data


def test_sanitize_reject_absurd_distance():
    # 月汇总总量 300 km 被误当成单次 → 整体拒绝
    cleaned, reject = sanitize_activity({"distance_km": 300.0})
    assert reject is not None


def test_sanitize_drop_impossible_speed_duration():
    # 距离在单次合理范围内但速度超限（时长 OCR 误读，如「1:30」读成 1 分钟）→ 温和丢弃时长、保留距离
    cleaned, reject = sanitize_activity({"distance_km": 5.2, "active_minutes": 1})
    assert reject is None
    assert "active_minutes" not in cleaned
    assert cleaned["distance_km"] == 5.2


def test_sanitize_keeps_long_run_drops_duration():
    # 100 km 百公里越野 + 30 分钟（200 km/h 不可能）→ 保留距离（合法越野）、丢弃误读的时长
    cleaned, reject = sanitize_activity({"distance_km": 100.0, "active_minutes": 30})
    assert reject is None
    assert "active_minutes" not in cleaned
    assert cleaned["distance_km"] == 100.0


def test_sanitize_drop_tiny_distance():
    # 0.01 km 过小 → 丢弃距离字段，其余字段保留
    cleaned, reject = sanitize_activity({"distance_km": 0.01, "steps": 8000})
    assert reject is None
    assert "distance_km" not in cleaned
    assert cleaned["steps"] == 8000


def test_sanitize_drop_out_of_range_fields():
    # 心率 999、配速 9999 超范围 → 丢弃；距离正常保留
    cleaned, reject = sanitize_activity(
        {"distance_km": 5.0, "avg_hr": 999, "avg_pace_sec_per_km": 9999}
    )
    assert reject is None
    assert cleaned == {"distance_km": 5.0}


def _box(cx, cy, w=30.0, h=20.0):
    """按中心点构造一个 OCR 框（四点坐标），供 parse_activity_from_boxes 测试。"""
    return [
        [cx - w / 2, cy - h / 2],
        [cx + w / 2, cy - h / 2],
        [cx + w / 2, cy + h / 2],
        [cx - w / 2, cy + h / 2],
    ]


def test_parse_boxes_distance_diagonal_unit():
    # 悦跑圈布局：距离大字「4.03」在单位「公里」的**左上方**（dx≈-216, dy≈-45），
    # 成对角线排列。历史上 _value_near 的「上方」规则 abs(dx)<=150 过窄导致距离读不出。
    result = [
        (_box(222.0, 395.0, w=60, h=50), "4.03", 0.99),  # 距离大字（左上）
        (_box(438.5, 440.5), "公里", 0.99),  # 单位（右下）
        (_box(741.0, 1541.5, w=90), "264千卡", 0.99),  # 消耗（合并框）
        (_box(102.5, 1483.5), "训练时长", 0.99),  # 时长标签
        (_box(155.0, 1544.5, w=80), "00:24:52", 0.99),  # 时长值（标签下方）
        (_box(714.0, 1695.0, w=70), "150米", 0.99),  # 爬升（合并框）
    ]
    data = parse_activity_from_boxes(result)
    assert data["distance_km"] == 4.03
    assert data["calories"] == 264
    assert data["active_minutes"] == 24
    assert data["ascent_meters"] == 150.0


def test_parse_boxes_distance_m_to_km():
    # 「距离」标签 + 右侧「1500 m」值框 → 单位换算 m→km，且走右侧同行候选区
    result = [
        (_box(100.0, 200.0), "距离", 0.99),
        (_box(240.0, 200.0), "1500 m", 0.99),
    ]
    data = parse_activity_from_boxes(result)
    assert data["distance_km"] == 1.5


def test_parse_boxes_distance_mi_to_km():
    # 合并框「5 mi」→ mi→km 换算
    result = [
        (_box(200.0, 200.0), "5 mi", 0.99),
    ]
    data = parse_activity_from_boxes(result)
    assert abs(data["distance_km"] - 8.045) < 1e-9


def test_parse_boxes_label_boundary_max_hr():
    # 「最大心率」框不应被 avg_hr 的「心率」短标签误匹配（否定词过滤）
    result = [
        (_box(200.0, 200.0), "最大心率", 0.99),
        (_box(320.0, 200.0), "180", 0.99),
    ]
    data = parse_activity_from_boxes(result)
    assert "avg_hr" not in data


def test_parse_boxes_label_avg_hr_ok():
    # 「平均心率」标签正常匹配右侧数值
    result = [
        (_box(200.0, 200.0), "平均心率", 0.99),
        (_box(320.0, 200.0), "160", 0.99),
    ]
    data = parse_activity_from_boxes(result)
    assert data["avg_hr"] == 160


def test_parse_boxes_distance_comma_decimal():
    # Sigma 换字体后小数点被 OCR 读成逗号：「6,23」应解析为 6.23 km，而非 623 km。
    result = [
        (_box(200.0, 300.0, w=60, h=50), "6,23", 0.99),  # 距离大字
        (_box(200.0, 360.0), "km", 0.99),  # 单位（下方）
    ]
    data = parse_activity_from_boxes(result)
    assert data["distance_km"] == 6.23


def test_parse_boxes_int_thousands_comma():
    # 千分位逗号（如消耗「1,234」）不应被误当小数点 → 仍为 1234
    result = [
        (_box(200.0, 200.0), "消耗", 0.99),
        (_box(320.0, 200.0), "1,234", 0.99),
    ]
    data = parse_activity_from_boxes(result)
    assert data["calories"] == 1234


def test_parse_boxes_prose_label_not_anchor():
    # 截图里混入「距离 6.23 公里」「海拔爬升为 0」这类描述句时，不应把它们当标签锚点
    # 做空间关联，否则会交叉污染：距离取到爬升的 0、爬升取到距离的 6.23。
    # 真正的「6.23 km」合并框应胜出，爬升不应被误提取。
    result = [
        (_box(1022.0, 229.0, w=80), "6.23 km", 0.99),  # 真正的距离（合并框）
        (_box(428.0, 608.0, w=600), "距离 6.23 公里", 0.99),  # 描述句（含「距离」+数字）
        (_box(334.5, 643.5, w=500), "海拔爬升为 0", 0.99),  # 描述句（含「爬升」+数字）
    ]
    data = parse_activity_from_boxes(result)
    assert data.get("distance_km") == 6.23
    assert "ascent_meters" not in data


def test_find_field_conflicts_agree():
    a = {"distance_km": 6.23, "calories": 400, "avg_hr": 160}
    b = {"distance_km": 6.2, "calories": 405, "avg_hr": 161}
    assert find_field_conflicts(a, b) == []


def test_find_field_conflicts_disagree_distance():
    # OCR 读成 623（逗号被当小数点前的旧 bug），视觉读成 6.23 → 距离判分歧
    assert find_field_conflicts({"distance_km": 623.0}, {"distance_km": 6.23}) == ["distance_km"]


def test_find_field_conflicts_one_side_only():
    # 只有一侧有值 → 不算分歧（走缺失补齐逻辑）
    assert find_field_conflicts({"distance_km": 6.23}, {}) == []
    assert find_field_conflicts({}, {"distance_km": 6.23}) == []


def test_find_field_conflicts_zero_vs_nonzero():
    # 一侧读成 0、另一侧非 0 → 判分歧（0 是 OCR 的真实读数，值得复核）
    assert find_field_conflicts({"distance_km": 0.0}, {"distance_km": 6.23}) == ["distance_km"]


def test_find_field_conflicts_tolerance_boundary():
    # 距离 5% 阈值：100 vs 105（4.76%）不判分歧；100 vs 106（5.66%）判分歧
    assert find_field_conflicts({"distance_km": 100.0}, {"distance_km": 105.0}) == []
    assert find_field_conflicts({"distance_km": 100.0}, {"distance_km": 106.0}) == ["distance_km"]


def test_find_field_conflicts_calories_looser_tolerance():
    # 卡路里 10% 容差：400 vs 430（约 7%）不判分歧；400 vs 450（约 11%）判分歧
    assert find_field_conflicts({"calories": 400}, {"calories": 430}) == []
    assert find_field_conflicts({"calories": 400}, {"calories": 450}) == ["calories"]


def test_parse_boxes_food_equivalent_not_calories():
    # Sigma 详情页：消耗大卡标签旁有「=1.4个」（≈1.4 个苹果）食物等效框，
    # 不应被当作消耗值；真正的「346」大卡应胜出。
    result = [
        (_box(100.0, 200.0), "消耗大卡", 0.99),  # 标签（左）
        (_box(260.0, 200.0), "346", 0.99),  # 消耗值（右，同行）
        (_box(260.0, 260.0), "=1.4个", 0.99),  # 食物等效（下方，须被排除）
    ]
    data = parse_activity_from_boxes(result)
    assert data["calories"] == 346


def test_parse_boxes_food_equivalent_alone_is_ignored():
    # 只有「=1.4个」没有真实消耗值时，不应把 1.4 当消耗
    result = [
        (_box(100.0, 200.0), "消耗大卡", 0.99),
        (_box(260.0, 200.0), "=1.4个", 0.99),
    ]
    data = parse_activity_from_boxes(result)
    assert "calories" not in data


def test_sanitize_drop_absurd_calories_per_km():
    # 6.23 km 只消耗 1 千卡（食物等效误读）→ 丢弃 calories，保留距离
    cleaned, reject = sanitize_activity({"distance_km": 6.23, "calories": 1})
    assert reject is None
    assert "calories" not in cleaned
    assert cleaned["distance_km"] == 6.23


def test_sanitize_keeps_plausible_calories():
    cleaned, reject = sanitize_activity({"distance_km": 6.23, "calories": 346})
    assert reject is None
    assert cleaned["calories"] == 346


def test_sanitize_calories_without_distance_kept():
    # 无距离无法做比值校验，calories 应原样保留（如纯步数/心率截图）
    cleaned, reject = sanitize_activity({"calories": 300})
    assert reject is None
    assert cleaned["calories"] == 300
