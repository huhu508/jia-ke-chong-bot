"""llm 纯函数测试：Markdown 清洗、身份守护、JSON 提取、请求体组装、端点池故障转移。

全部不触发网络调用：故障转移用 monkeypatch 替换 ``_call_endpoint`` 来模拟成功/失败。
"""

from app.config import LlmEndpoint
from app.services import llm


def test_strip_markdown_bold():
    assert llm._strip_markdown("**加粗** 正常") == "加粗 正常"


def test_strip_markdown_heading_and_list():
    out = llm._strip_markdown("# 标题\n- 列表项\n1. 有序")
    assert "标题" in out and "列表项" in out and "有序" in out
    assert "#" not in out


def test_strip_markdown_link_and_code():
    assert llm._strip_markdown("[点击](http://x) `code`") == "点击 code"


def test_guard_identity_blocks_leak():
    msg = "我是 ChatGPT 模型"
    out = llm._guard_identity(msg)
    assert out != msg
    assert "甲壳虫" in out


def test_guard_identity_does_not_overblock():
    # 「文心」后紧跟汉字不触发（如「文心雕龙」），避免词边界误伤普通词汇
    assert llm._guard_identity("文心雕龙是一本书") == "文心雕龙是一本书"


def test_parse_json_obj():
    assert llm._parse_json_obj('{"intent": "today"}') == {"intent": "today"}


def test_parse_json_obj_with_noise():
    assert llm._parse_json_obj('前缀 {"intent": "ranking"} 后缀') == {"intent": "ranking"}


def test_parse_json_obj_invalid():
    assert llm._parse_json_obj("没有 JSON") is None


def _ep(model: str = "glm-5.3-flash", effort: str = "", vision: bool = False):
    """构造一个端点（测试用，不触网）。"""
    return LlmEndpoint(
        model=model,
        base_url="http://127.0.0.1:9/v1/chat/completions",
        api_key="test-key",
        reasoning_effort=effort,
        vision=vision,
    )


def test_build_payload_without_reasoning_effort():
    """未配置思考强度时不得下发该参数，保证对旧服务商完全向后兼容。"""
    payload = llm._build_payload(_ep(), [{"role": "user", "content": "hi"}], 100, 0.7)
    assert "reasoning_effort" not in payload
    assert payload["model"] == "glm-5.3-flash"
    assert payload["max_tokens"] == 100
    assert payload["temperature"] == 0.7


def test_build_payload_includes_reasoning_effort_when_configured():
    """配置了思考强度时才下发 reasoning_effort（GLM 短任务靠它压思考 token）。"""
    payload = llm._build_payload(_ep(effort="low"), [{"role": "user", "content": "hi"}], 100, 0.7)
    assert payload["reasoning_effort"] == "low"


def test_build_payload_strips_blank_reasoning_effort():
    """纯空白视为未配置，避免把空串发给服务商。"""
    payload = llm._build_payload(_ep(effort="   "), [{"role": "user", "content": "hi"}], 100, 0.7)
    assert "reasoning_effort" not in payload


def test_short_reply_tokens_leave_room_for_thinking():
    """短文案的输出上限必须留足思考 token 余量：思考常开的模型给 200 会被吃空。"""
    assert llm._SHORT_REPLY_TOKENS >= 600


def test_endpoints_primary_first_then_fallbacks(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_api_key", "primary-key")
    monkeypatch.setattr(llm.settings, "llm_fallbacks", [_ep("qwen3:4b")])
    models = [e.model for e in llm._endpoints()]
    assert models == [llm.settings.llm_model, "qwen3:4b"]


def test_endpoints_skip_fallback_without_api_key(monkeypatch):
    """备用端点没填 api_key 视为未配置，直接跳过（本地 Ollama 也要填占位值）。"""
    monkeypatch.setattr(llm.settings, "llm_api_key", "")
    monkeypatch.setattr(
        llm.settings,
        "llm_fallbacks",
        [
            LlmEndpoint(model="no-key", base_url="http://x", api_key=""),
            _ep("ok"),
        ],
    )
    assert [e.model for e in llm._endpoints()] == ["ok"]


def test_endpoints_vision_filter(monkeypatch):
    """视觉兜底只路由到声明支持图片的端点，纯文本模型不会被发图。"""
    monkeypatch.setattr(llm.settings, "llm_api_key", "")
    monkeypatch.setattr(
        llm.settings,
        "llm_fallbacks",
        [_ep("text-only", vision=False), _ep("vision-ok", vision=True)],
    )
    assert [e.model for e in llm._endpoints(vision=True)] == ["vision-ok"]
    assert len(llm._endpoints(vision=False)) == 2


def test_call_returns_none_without_any_endpoint(monkeypatch):
    """没有任何可用端点时直接返回 None，不发起网络请求。"""
    monkeypatch.setattr(llm.settings, "llm_api_key", "")
    monkeypatch.setattr(llm.settings, "llm_fallbacks", [])
    assert llm._call([{"role": "user", "content": "hi"}]) is None


def test_call_fails_over_to_next_endpoint(monkeypatch):
    """主端点失败后自动切到备用端点，并返回备用端点的结果。"""
    monkeypatch.setattr(llm.settings, "llm_api_key", "primary-key")
    monkeypatch.setattr(llm.settings, "llm_fallbacks", [_ep("fallback-model")])
    calls: list[str] = []

    def fake_call_endpoint(ep, *args, **kwargs):
        calls.append(ep.model)
        return None if ep.model == llm.settings.llm_model else "备用成功"

    monkeypatch.setattr(llm, "_call_endpoint", fake_call_endpoint)
    assert llm._call([{"role": "user", "content": "hi"}]) == "备用成功"
    assert calls == [llm.settings.llm_model, "fallback-model"]


def test_call_stops_at_first_success(monkeypatch):
    """主端点成功时不应再调用备用端点（避免无谓消耗额度）。"""
    monkeypatch.setattr(llm.settings, "llm_api_key", "primary-key")
    monkeypatch.setattr(llm.settings, "llm_fallbacks", [_ep("fallback-model")])
    calls: list[str] = []

    def fake_call_endpoint(ep, *args, **kwargs):
        calls.append(ep.model)
        return "主端点成功"

    monkeypatch.setattr(llm, "_call_endpoint", fake_call_endpoint)
    assert llm._call([{"role": "user", "content": "hi"}]) == "主端点成功"
    assert calls == [llm.settings.llm_model]
