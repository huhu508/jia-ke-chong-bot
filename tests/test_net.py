"""services.net 网络辅助函数测试：TLS 校验按地址区分（内网降级、公网校验）。"""

import sys
from pathlib import Path

# 项目根加入 sys.path，保证 `import app.*` 在任意 cwd 下都能工作（与 conftest 一致）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.net import is_private_host, verify_tls  # noqa: E402


def test_private_and_loopback_hosts():
    for host in (
        "localhost",
        "127.0.0.1",
        "::1",
        "10.0.0.5",
        "192.168.1.5",
        "172.16.0.1",
        "169.254.1.1",
        "nas.local",
    ):
        assert is_private_host(host), host


def test_public_hosts():
    for host in ("example.com", "8.8.8.8", "1.1.1.1", "p.qpic.cn"):
        assert not is_private_host(host), host


def test_verify_tls():
    # 内网/本机地址降级不校验
    assert not verify_tls("https://192.168.1.5:8080/download/a.jpg")
    assert not verify_tls("http://localhost:8080/img")
    # 公网地址保持校验
    assert verify_tls("https://p.qpic.cn/abc.jpg")
    assert verify_tls("https://8.8.8.8/x.jpg")
