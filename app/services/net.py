"""网络辅助函数：按地址区分是否校验 TLS。

NapCat 图片 url 常指向内网/本机服务（自签证书或 HTTP），无有效公网证书；
公网 URL（QQ CDN 等）则应校验 TLS，避免中间人替换图片内容。
"""

import ipaddress
from urllib.parse import urlparse


def is_private_host(host: str) -> bool:
    """判断 host 是否为内网/回环/链路本地/保留地址（这些地址应关闭 TLS 校验）。

    返回 True 表示关闭 TLS 校验。
    """
    host = (host or "").strip().lower()
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 非 IP（公网域名），默认校验
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def verify_tls(url: str) -> bool:
    """按 URL 决定是否校验 TLS：内网/本机降级，公网保持校验。"""
    try:
        host = urlparse(url).hostname
    except Exception:
        return True
    return not (host and is_private_host(host))
