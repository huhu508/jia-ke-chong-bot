"""训练处方校验 / 模板 / 渲染的纯函数测试（不触网、不碰数据库）。"""

from app.services import prescription


def test_validate_ok():
    raw = {
        "focus": "有氧打底",
        "sessions": [
            {"kind": "轻松跑", "distance_km": 8, "pace_sec_per_km": 400, "purpose": "有氧"},
            {"kind": "节奏跑", "distance_km": 6, "pace_sec_per_km": 330, "purpose": "阈值"},
            {"kind": "休息", "purpose": "恢复"},
        ],
        "caution": "别硬顶",
    }
    p, reason = prescription.parse_and_validate(raw, base_km=20.0)
    assert reason == ""
    assert p is not None
    assert p.source == "model"
    assert [s.kind for s in p.sessions] == ["轻松跑", "节奏跑", "休息"]
    # 休息日强制归零距离
    assert p.sessions[2].distance_km == 0


def test_validate_none_input():
    p, reason = prescription.parse_and_validate(None, 20.0)
    assert p is None and reason


def test_validate_rejects_unknown_kind():
    raw = {
        "focus": "x",
        "sessions": [
            {"kind": "游泳", "distance_km": 1},
            {"kind": "轻松跑", "distance_km": 5},
        ],
    }
    p, reason = prescription.parse_and_validate(raw, 20.0)
    assert p is None and "未知训练类型" in reason


def test_validate_rejects_too_many_sessions():
    sessions = [{"kind": "轻松跑", "distance_km": 2} for _ in range(6)]
    p, reason = prescription.parse_and_validate({"focus": "x", "sessions": sessions}, 20.0)
    assert p is None and "训练次数" in reason


def test_validate_rejects_absurd_total():
    sessions = [
        {"kind": "长距离", "distance_km": 50},
        {"kind": "长距离", "distance_km": 50},
    ]
    p, reason = prescription.parse_and_validate({"focus": "x", "sessions": sessions}, 20.0)
    assert p is None and "总量" in reason


def test_validate_rejects_bad_pace():
    sessions = [
        {"kind": "轻松跑", "distance_km": 5, "pace_sec_per_km": 60},
        {"kind": "轻松跑", "distance_km": 5},
    ]
    p, reason = prescription.parse_and_validate({"focus": "x", "sessions": sessions}, 20.0)
    assert p is None and "配速" in reason


def test_validate_rejects_blank_focus():
    p, reason = prescription.parse_and_validate({"focus": "  ", "sessions": []}, 20.0)
    assert p is None and "重点" in reason


def test_validate_accepts_small_base():
    """近 7 天跑量很小时不按倍数校验，只给绝对上限，避免新手被误拒。"""
    raw = {
        "focus": "x",
        "sessions": [
            {"kind": "轻松跑", "distance_km": 5},
            {"kind": "轻松跑", "distance_km": 5},
        ],
    }
    p, reason = prescription.parse_and_validate(raw, base_km=0.5)
    assert p is not None, reason


def test_template_fatigued_reduces_load():
    result = {
        "load_status": {"verdict": "明显疲劳，建议主动减量恢复", "ratio": -0.3},
        "acwr": 1.6,
        "weekly": [{"pace": 360}],
    }
    p = prescription.template_prescription(result, base_km=20.0)
    assert p.source == "template"
    assert "减量" in p.focus


def test_template_normal_has_three_sessions():
    result = {
        "load_status": {"verdict": "正常训练状态", "ratio": 0.0},
        "acwr": 1.0,
        "weekly": [{"pace": 360}],
    }
    p = prescription.template_prescription(result, base_km=20.0)
    assert len(p.sessions) == 3
    assert p.source == "template"


def test_render_contains_sections():
    p = prescription.Prescription(
        focus="打底",
        sessions=[prescription.Session("轻松跑", 6.0, 390, "有氧")],
        caution="注意恢复",
        source="template",
    )
    out = prescription.render(p, "测试")
    assert "下周训练处方" in out
    assert "6 km" in out
    assert "6'30\"" in out
    assert "规则生成" in out
    assert "注意恢复" in out
