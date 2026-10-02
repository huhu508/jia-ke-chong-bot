"""输入安全加固：清理提问文本 + 拦截提示词注入（纯函数，无 nonebot/config 依赖）。

@机器人 收到的群友原话与多轮上下文，都可能被用来污染/注入 LLM。这里在进入 LLM 前
加一道本地、确定性、可单测的防线：控制字符清洗、超长截断、指令覆盖类注入拦截。
"""

import re

# 零宽字符（零宽空格、方向控制符、BOM 等）：直接删除（不可见，删除后词自然相连）。
_ZERO_WIDTH_RE = re.compile(r"[​-‏‪-‮﻿]")
# 控制字符（含换行/制表/null 等）：替换为空格，避免把换行两侧的词粘在一起。
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# 指令覆盖/越权类注入模式（大小写不敏感）。目标是把机器人「带偏」成别的角色、
# 或套取系统提示词/设定。刻意不拦「你是谁 / 你是什么模型」——那走 _PERSONA +
# _guard_identity 正常应答。
_INJECTION_PATTERNS = (
    r"忽略(?:之前|以上|所有|一切|你的)?(?:的)?(?:指令|提示|设定|规则|要求|之前的话)",
    r"无视(?:之前|以上|所有|你的)?(?:的)?(?:指令|提示|设定|规则)",
    r"(?:忘掉|忘记|清除|清空|删除)(?:之前|你|你的)?(?:的)?(?:指令|提示|设定|规则|记忆|对话)",
    r"扮演|假装|伪装成|role\s*play|pretend|act\s+as|you\s+are\s+now|你就是",
    r"系统提示(?:词)?|system\s*prompt|你的提示词|你的设定|你的角色",
    r"开发者模式|developer\s*mode|jail\s*break|越狱",
    r"(?:泄露|泄漏|告诉我|说出|显示|打印|吐出)(?:你的|系统)?(?:提示词|设定|指令|prompt|秘密)",
    r"ignore\s+(?:all|previous|above|the|your)?\s*(?:previous|above|all|the|your)?\s*(?:instructions|prompt|directives|rules)",
    r"disregard|override\s+your",
    r"(?:现在|从今(?:天|以后)|从此)起你是|你不再是",
)


def clean_question(text: str, max_len: int = 400) -> str:
    """清洗群友提问：剔控制/零宽字符、折叠空白、超长截断。"""
    if not text:
        return ""
    # 零宽字符直接删；控制字符（含换行）→ 空格，随后折叠空白。
    t = _ZERO_WIDTH_RE.sub("", text)
    t = _CONTROL_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_len:
        t = t[:max_len].rstrip()
    return t


def looks_like_injection(text: str) -> bool:
    """本地判定提问是否是指令覆盖/越权类注入（命中任一模式返回 True）。"""
    t = (text or "").strip()
    if not t:
        return False
    return any(re.search(p, t, re.IGNORECASE) for p in _INJECTION_PATTERNS)
