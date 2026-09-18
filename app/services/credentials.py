"""按 QQ 号存储各平台绑定凭据，一个成员一个文件。

Garmin 存邮箱+密码（用户私聊提供），COROS 存 OAuth token（私密链接授权）。
文件放 ``data/accounts/``，与既有 token 文件同一风格，便于备份与清理。
解绑时调用 ``delete`` 清掉对应文件，保证「再次绑定需重新授权」。
"""

import json
from pathlib import Path
from typing import Optional

_DIR = Path("data/accounts")


def _path(qq: str, platform: str) -> Path:
    return _DIR / f"{platform}_{qq}.json"


def load(qq: str, platform: str) -> Optional[dict]:
    p = _path(qq, platform)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save(qq: str, platform: str, data: dict) -> None:
    p = _path(qq, platform)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def delete(qq: str, platform: str) -> None:
    _path(qq, platform).unlink(missing_ok=True)
