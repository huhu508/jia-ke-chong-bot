"""运维告警：把关键故障从「静默吞掉」升级为「可感知」。

provider 解析 / 定时任务里原本只 logger.warning 的关键失败（平台核心接口异常、
播报/同步失败等），除记日志外再调 ``alert.record()`` 登记一条；每日排行播报搭车
把这些告警私聊发给管理员（superuser），避免「数据悄悄缺失」却无人察觉。

内存态 + 去重限频：同一 key 一个窗口期内只记一次，避免同一故障反复刷屏；
重启即清空，不做持久化（告警是「提醒」性质，不是审计）。
"""

import threading
import time

# 同一告警 key 的去重窗口（秒）：同一故障 6 小时内只提醒一次
_DEDUP_SEC = 6 * 3600

_lock = threading.Lock()
# key -> (下次可再次记录的时间戳, 消息)
_pending: dict[str, tuple[float, str]] = {}


def record(key: str, message: str) -> None:
    """登记一条告警（同 key 去重限频）。同步函数、线程安全，可在 provider 解析路径调用。"""
    now = time.time()
    with _lock:
        until, _ = _pending.get(key, (0.0, ""))
        if now < until:
            return
        _pending[key] = (now + _DEDUP_SEC, message)


def drain() -> list[str]:
    """取走并清空所有待发告警，返回消息列表（可能为空）。"""
    with _lock:
        items = [msg for _, msg in _pending.values()]
        _pending.clear()
        return items
