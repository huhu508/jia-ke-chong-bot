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
    return read_file(_path(qq, platform))


def save(qq: str, platform: str, data: dict) -> None:
    write_file(_path(qq, platform), data)


def delete(qq: str, platform: str) -> None:
    _path(qq, platform).unlink(missing_ok=True)


def read_file(path: Path) -> Optional[dict]:
    """读取一个加密 JSON 文件并解密；不存在/损坏/非对象返回 None。"""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    result = crypto.decrypt_json(raw)
    # 文件内容可能被写成非对象（数组/字符串等），调用方假设拿到 dict 会 .get() 崩溃，
    # 这里统一收敛为「无凭据」。
    return result if isinstance(result, dict) else None


def write_file(path: Path, data: dict) -> None:
    """把 dict 加密后写盘（自动建目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(crypto.encrypt_json(data), ensure_ascii=False, indent=2), encoding="utf-8"
    )
