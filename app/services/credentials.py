"""按 QQ 号存储各平台绑定凭据，一个成员一个文件。

Garmin 存邮箱+密码（用户私聊提供），COROS 存 OAuth token（私密链接授权）。
文件放 ``data/accounts/``，经 ``crypto`` 模块 Fernet 加密落盘，密钥在
``data/secret.key``（自动生成，不入库）。读取兼容旧明文（见 crypto.decrypt_json）。
解绑时调用 ``delete`` 清掉对应文件，保证「再次绑定需重新授权」。
"""

import json
from pathlib import Path
from typing import Optional

from . import crypto

_DIR = Path("data/accounts")


def _path(qq: str, platform: str) -> Path:
    return _DIR / f"{platform}_{qq}.json"


def load(qq: str, platform: str) -> Optional[dict]:
    p = _path(qq, platform)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    result = crypto.decrypt_json(raw)
    # 文件内容可能被写成非对象（数组/字符串等），调用方假设拿到 dict 会 .get() 崩溃，
    # 这里统一收敛为「无凭据」。
    return result if isinstance(result, dict) else None


def save(qq: str, platform: str, data: dict) -> None:
    p = _path(qq, platform)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(crypto.encrypt_json(data), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def delete(qq: str, platform: str) -> None:
    _path(qq, platform).unlink(missing_ok=True)
