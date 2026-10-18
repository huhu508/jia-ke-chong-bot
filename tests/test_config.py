"""配置层稳定性测试：新增的数值型配置留空时不能导致启动失败。"""

from app.config import Settings


def test_blank_llm_timeout_falls_back_to_default():
    """`.env` 里写成空的 LLM_TIMEOUT= 要退回 45s，而不是启动即 ValidationError。"""
    assert Settings(llm_timeout="").llm_timeout == 45.0
    assert Settings(llm_timeout="   ").llm_timeout == 45.0


def test_valid_llm_timeout_is_kept():
    assert Settings(llm_timeout="25").llm_timeout == 25.0
    assert Settings(llm_timeout=30).llm_timeout == 30.0
