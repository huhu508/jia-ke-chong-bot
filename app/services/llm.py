"""AI 大模型服务：调用智谱 GLM（OpenAI 兼容）做运动数据解读/总结。

生态位（niche）设计：
  - LLM 是「锦上添花」的增强层，**绝不进核心数据链路**——OCR 识别、数据解析、
    排行聚合、平台同步都走确定性代码，不依赖 LLM；
  - 未配置 key 或调用失败时一律返回 None，由调用方降级到模板文案，机器人照常可用；
  - 只把「聚合后的数字」发给模型，不发送原始消息/截图/账号密码，保护隐私；
  - 抽象成独立 provider，未来换 DeepSeek / 通义千问 / Kimi 等 OpenAI 兼容 API，
    只需改 base_url + key，无需动调用方。

为阻塞网络调用，请在 asyncio.to_thread 中执行。
"""

import httpx
from nonebot.log import logger

from ..config import settings
from .cheers import format_pace

_TIMEOUT = 15.0

# 统一人格：所有 LLM 交互共用，保证「总结/鼓励/建议/打卡点评/问答」语气一致。
_PERSONA = "你是运动群机器人「甲壳虫」，一个热情、接地气、懂运动的搭子兼教练。"


def _chat(messages: list[dict], max_tokens: int = 400, timeout: float = _TIMEOUT) -> str | None:
    """调一次智谱 GLM。成功返回文本；未配置 key / 任何异常返回 None。"""
    if not settings.zhipu_api_key:
        return None
    try:
        resp = httpx.post(
            settings.llm_base_url,
            headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
            json={
                "model": settings.zhipu_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"智谱 GLM 调用失败（将降级模板文案）: {e}")
        return None


def summarize_sport(name: str, period: str, s: dict) -> str | None:
    """把个人周期汇总数据交给模型生成自然语言总结；失败返回 None（由调用方降级）。

    period 形如「本周」/「本月」；s 为 summary.compute_member_summary 的返回值。
    """
    system_prompt = (
        "你是运动群机器人「甲壳虫」的数据助手。请用 2~3 句中文总结用户的周期运动数据，"
        "语气轻松、带点鼓励，别啰嗦，不要编造未给出的数据。"
    )
    user_text = (
        f"{name} 的{period}数据：运动 {s.get('activities', 0)} 次（共 {s.get('active_days', 0)} 天），"
        f"总距离 {s.get('distance_km', 0)} km，爬升 {s.get('ascent_meters', 0)} m，"
        f"总时长 {s.get('active_minutes', 0)} 分钟，消耗 {s.get('calories', 0)} 千卡，"
        f"运动负荷 {s.get('training_load', 0)}，单次最长 {s.get('max_activity_distance_km', 0)} km。"
    )
    return _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
    )


def _sport_facts(name: str, s: dict) -> str:
    """把汇总 dict 拼成一句供 prompt 复用的数字描述。"""
    return (
        f"{name}：运动 {s.get('activities', 0)} 次（共 {s.get('active_days', 0)} 天），"
        f"距离 {s.get('distance_km', 0)} km，爬升 {s.get('ascent_meters', 0)} m，"
        f"时长 {s.get('active_minutes', 0)} 分钟，消耗 {s.get('calories', 0)} 千卡，"
        f"负荷 {s.get('training_load', 0)}，单次最长 {s.get('max_activity_distance_km', 0)} km"
    )


def encourage(name: str, s: dict) -> str | None:
    """根据周期数据生成一句走心的鼓励；失败返回 None（由调用方降级）。"""
    system_prompt = (
        "你是运动群机器人「甲壳虫」的教练。根据用户数据给 1~2 句简短、走心、不套话的鼓励，"
        "可以点出亮点或给个小建议，别编造数据。"
    )
    return _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _sport_facts(name, s) + "。请给我一句鼓励。"},
        ],
        max_tokens=300,
    )


def answer_question(question: str) -> str | None:
    """运动知识自由问答；失败返回 None（由调用方降级）。

    范围严格限定在运动领域：遇到求职、学习辅导等无关话题，或暴力、违法等不当请求，
    一律礼貌拒绝并说明「只聊运动」，不展开回答（群里测试过「教我英语/简历优化/教我怎么打人」）。
    """
    system_prompt = (
        "你是运动群机器人「甲壳虫」的运动教练，只回答跑步、骑行、越野、健身、训练恢复等运动问题。"
        "用简洁中文，2~4 句，给出实用建议，不要编造医学结论。"
        "遇到与运动无关的话题（求职、英语、学习辅导等）或暴力、违法、骚扰等不当请求，"
        "一律礼貌拒绝并说明「我只聊运动相关」，不要展开、不要配合。"
    )
    return _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question.strip()},
        ],
        max_tokens=500,
    )


def comment_checkin(name: str, data: dict) -> str | None:
    """针对单次打卡数据给一句短点评；失败返回 None（调用方降级为纯数据回显）。"""
    bits = []
    if data.get("distance_km"):
        bits.append(f"距离 {data['distance_km']} km")
    if data.get("avg_pace_sec_per_km"):
        bits.append(f"配速 {format_pace(data['avg_pace_sec_per_km'])}/km")
    if data.get("ascent_meters"):
        bits.append(f"爬升 {data['ascent_meters']:.0f} m")
    if data.get("calories"):
        bits.append(f"消耗 {data['calories']} 千卡")
    if data.get("active_minutes"):
        bits.append(f"时长 {data['active_minutes']} 分钟")
    if data.get("avg_hr"):
        bits.append(f"平均心率 {data['avg_hr']}")
    if not bits:
        return None
    system_prompt = (
        _PERSONA + " 用户刚完成一次运动打卡。请用一句话（15 字左右）点评这次运动，"
        "语气轻松有梗、不说教，不要编造未给出的数据。"
    )
    user_text = f"{name} 本次运动：{'，'.join(bits)}。"
    return _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        max_tokens=120,
        timeout=5.0,
    )


def advise(name: str, period: str, s: dict) -> str | None:
    """根据周期汇总给训练建议；失败返回 None（由调用方降级）。"""
    system_prompt = (
        _PERSONA + f" 根据用户{period}运动数据给 2~3 句实用训练建议（强度/恢复/加量节奏），"
        "专业但不掉书袋，别编造数据。"
    )
    return _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _sport_facts(name, s)},
        ],
        max_tokens=400,
    )
