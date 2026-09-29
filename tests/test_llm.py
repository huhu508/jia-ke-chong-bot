"""llm 纯函数测试：Markdown 清洗、身份守护、JSON 提取（均不触发网络调用）。"""

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
