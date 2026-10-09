from ...config import settings
from .base import DailyStats, SportProvider
from .coros import CorosProvider
from .garmin import GarminProvider

__all__ = ["SportProvider", "DailyStats", "get_provider", "VALID_PLATFORMS"]

# 当前开放绑定的平台（每用户独立绑定）
VALID_PLATFORMS = ("garmin", "coros")

_PROVIDERS: dict[str, SportProvider] = {
    # Garmin 为每用户凭据（成员私聊发邮箱+密码），构造只传 is_cn
    "garmin": GarminProvider(settings.garmin_is_cn),
    "coros": CorosProvider(settings.coros_region),
}


def get_provider(name: str) -> SportProvider:
    name = name.lower()
    if name not in _PROVIDERS:
        raise KeyError(f"未知平台: {name}，可选: {', '.join(VALID_PLATFORMS)}")
    provider = _PROVIDERS[name]
    # 当前 Garmin/COROS 均为「每用户凭据」，configured 恒为 True；此分支是为将来可能出现
    # 「需 .env 预配凭据」的平台预留的兜底，并非死代码。
    if not provider.configured:
        raise RuntimeError(f"平台 {name} 尚未配置凭据，请先在 .env 中填写")
    return provider
