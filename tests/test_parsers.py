"""页面类型识别 + 文本解析 + 合理性校验的纯函数测试。"""

from app.services.parsers import (
    detect_page_kind,
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


def test_sanitize_reject_impossible_speed():
    # 100 km 但 30 分钟 → 200 km/h，超出跑步极限 → 拒绝
    cleaned, reject = sanitize_activity({"distance_km": 100.0, "active_minutes": 30})
    assert reject is not None


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
