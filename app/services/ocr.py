"""OCR 图片文字识别（本地 RapidOCR，数据不出本机）。

底层是 rapidocr-onnxruntime，默认 PP-OCRv4 mobile 中文模型。
RapidOCR 官方评测中 PP-OCRv5 中文精确匹配率反而更低（0.7355 vs 0.8323），
故保持默认模型，不做无收益的模型替换；真正的优化在「按坐标关联标签与数值」。
"""

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
    return _engine


def recognize_boxes(img_bytes: bytes) -> list:
    """识别图片文字，返回带坐标的结构化结果。

    每个元素为 ``[box, text, score]``，其中 box 是四点坐标 ``[[x, y], ...]``，
    text/score 可能是 str。返回空列表表示没识别到任何文字。

    阻塞调用（首次会加载模型，较慢），需在 ``asyncio.to_thread`` 中执行。
    """
    engine = _get_engine()
    result, _ = engine(img_bytes)
    return result or []


def recognize(img_bytes: bytes) -> str:
    """识别图片文字，返回按行拼接的纯文本（兼容旧接口，无坐标信息）。"""
    return "\n".join(str(item[1]) for item in recognize_boxes(img_bytes))
