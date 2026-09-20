"""统一凭据加密：Fernet 对称加密 + ``data/secret.key`` 自动生成。

所有敏感落盘数据（佳明邮箱+密码、COROS OAuth token）统一经此模块加密。
设计要点：
  - 密钥自动生成到 ``data/secret.key``（``data/`` 已在 .gitignore，不会提交）；
  - 明文兜底读取 + 一次性迁移，保证老数据不中断；
  - 解密失败（密钥被换 / 密文损坏）返回 None，宁可判「未绑定/未授权」也不吐错误凭据。
"""

import json
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_KEY_FILE = Path("data/secret.key")
_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """读取或自动生成密钥，返回 Fernet 实例（模块级缓存）。"""
    global _fernet
    if _fernet is None:
        if _KEY_FILE.exists():
            key = _KEY_FILE.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _KEY_FILE.write_bytes(key)
        _fernet = Fernet(key)
    return _fernet


def encrypt_json(data: dict) -> dict:
    """把 dict 加密成 ``{"_enc": "<token>"}`` 结构，可直接 json.dumps 落盘。"""
    token = _get_fernet().encrypt(json.dumps(data, ensure_ascii=False).encode())
    return {"_enc": token.decode()}


def decrypt_json(raw: dict) -> Optional[dict]:
    """解密封装结构；明文老数据原样返回（幂等迁移）。

    返回 None 表示无法读取（密钥被换 / 密文损坏），由调用方视为「未绑定/未授权」。
    """
    if not isinstance(raw, dict):
        return raw
    if "_enc" not in raw:
        return raw  # 旧明文，兼容返回
    try:
        return json.loads(_get_fernet().decrypt(raw["_enc"].encode()).decode("utf-8"))
    except (InvalidToken, ValueError):
        return None


def migrate_encrypt() -> int:
    """把 ``data/accounts/*.json`` 下旧明文凭据升级为加密格式，返回迁移文件数（幂等）。"""
    accounts_dir = Path("data/accounts")
    if not accounts_dir.exists():
        return 0
    n = 0
    for p in accounts_dir.glob("*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(raw, dict) and "_enc" not in raw:
            p.write_text(
                json.dumps(encrypt_json(raw), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            n += 1
    return n
