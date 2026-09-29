from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Optional

from nonebot.log import logger
from pydantic import BaseModel


class DailyStats(BaseModel):
    """各平台统一后的每日统计数据。

    除基础指标外，新增用户最关心的运动维度（参考主流运动 App）：
    爬升高度、运动负荷、平均配速、运动平均心率、单次最长距离。
    各平台能取到多少就填多少，取不到留默认 0（展示层只显示 >0 的字段）。
    """

    date: date
    steps: int = 0
    distance_km: float = 0.0
    active_minutes: int = 0
    calories: int = 0
    resting_hr: int = 0
    sleep_hours: float = 0.0
    activities_count: int = 0
    # 新增运动维度（默认 0 = 平台未提供）
    ascent_meters: float = 0.0  # 爬升高度 / 累计爬升（米）
    training_load: float = 0.0  # 运动负荷（无量纲分数，各平台口径不一）
    avg_pace_sec_per_km: float = 0.0  # 平均配速（秒/公里）
    avg_hr: int = 0  # 运动平均心率（bpm）
    max_activity_distance_km: float = 0.0  # 单次运动最长距离（服务排行）
    raw: Optional[dict] = None


class SportProvider(ABC):
    """运动平台适配层统一接口。每个平台各自实现。"""

    name: str = ""

    @abstractmethod
    def fetch_daily(self, account: str, d: date) -> DailyStats:
        """拉取某账号某天的统一统计数据。为阻塞调用，需在线程中执行。"""
        raise NotImplementedError

    def fetch_range(self, account: str, start: date, end: date) -> list[DailyStats]:
        """拉取 ``[start, end)`` 区间的每日统计，按日期升序返回。

        默认实现逐日调用 ``fetch_daily``；平台可覆盖为「登录一次复用会话」，
        避免逐日重新认证（如回填一个月历史时）。单日失败仅记日志并跳过该天，
        不中断整体；返回列表即「成功拉取的天」，调用方据此统计回填天数。
        """
        out: list[DailyStats] = []
        d = start
        while d < end:
            try:
                out.append(self.fetch_daily(account, d))
            except Exception as e:
                logger.warning(f"{self.name} 拉取 {account} {d} 失败: {e}")
            d += timedelta(days=1)
        return out
