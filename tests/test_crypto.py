"""crypto 加解密：往返、明文兜底、损坏密文、密钥隔离。"""

import pytest

from app.services import crypto


@pytest.fixture()
def isolated_key(tmp_path, monkeypatch):
    """把密钥文件指到临时目录并重置模块级缓存，避免污染生产 data/secret.key。"""
    monkeypatch.setattr(crypto, "_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "_fernet", None)
    return tmp_path / "secret.key"


def test_roundtrip(isolated_key):
    data = {"email": "a@b.com", "password": "s3cr3t"}
    enc = crypto.encrypt_json(data)
    assert set(enc.keys()) == {"_enc"}
    assert crypto.decrypt_json(enc) == data


def test_plaintext_passthrough(isolated_key):
    # 旧明文数据原样返回（幂等迁移，保证老数据不中断）
    assert crypto.decrypt_json({"email": "a@b.com"}) == {"email": "a@b.com"}


def test_non_dict_passthrough(isolated_key):
    assert crypto.decrypt_json([1, 2, 3]) == [1, 2, 3]


def test_corrupted_ciphertext_returns_none(isolated_key):
    # 密文损坏 / 密钥不一致 → 解密失败返回 None（调用方判「未绑定/未授权」，绝不吐错误凭据）
    assert crypto.decrypt_json({"_enc": "not-a-valid-fernet-token"}) is None
