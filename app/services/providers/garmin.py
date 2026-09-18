from datetime import date

from garminconnect import Garmin

from .. import credentials
from .base import DailyStats, SportProvider


class GarminProvider(SportProvider):
    """Garmin 佳明（每用户邮箱+密码登录）。

    garminconnect 库无第三方 OAuth，只能邮箱密码登录。因此成员在**私聊**中把
    邮箱密码发给机器人，按 QQ 号分别存储（``data/accounts/garmin_<qq>.json``），
    查询时用该成员自己的凭据登录拉数，不在群里暴露密码。
    """

    name = "garmin"

    def __init__(self, is_cn: bool = True):
        self._is_cn = is_cn

    @property
    def configured(self) -> bool:
        # 每用户凭据，无需 .env 预配账号密码，始终可发起绑定
        return True

    def bind(self, qq: str, email: str, password: str) -> None:
        """校验登录并保存该 QQ 的佳明凭据（登录失败抛 RuntimeError）。"""
        email = (email or "").strip()
        password = (password or "").strip()
        if not email or not password:
            raise RuntimeError("邮箱或密码为空")
        try:
            Garmin(email, password, is_cn=self._is_cn).login()
        except Exception as e:
            raise RuntimeError(f"佳明登录失败，请检查邮箱密码：{e}")
        credentials.save(qq, "garmin", {"email": email, "password": password, "is_cn": self._is_cn})

    def fetch_daily(self, qq: str, d: date) -> DailyStats:
        cred = credentials.load(qq, "garmin")
        if not cred:
            raise RuntimeError("尚未绑定佳明，请私聊机器人发送「garmin绑定 邮箱 密码」")
        client = Garmin(cred["email"], cred["password"], is_cn=cred.get("is_cn", True))
        client.login()

        iso = d.isoformat()
        stats = DailyStats(date=d)

        # 步数 / 距离 / 活动时长 / 消耗
        raw: dict = {}
        try:
            raw = client.get_stats(iso) or {}
        except Exception:
            raw = {}

        stats.steps = int(raw.get("totalSteps") or 0)
        stats.distance_km = round(float(raw.get("totalDistanceMeters") or 0) / 1000, 2)
        # 新版返回 activeSeconds（总活动秒数）与 *IntensityMinutes（分），此处用总活动秒数换算分钟
        stats.active_minutes = int(raw.get("activeSeconds") or 0) // 60
        stats.calories = int(raw.get("activeKilocalories") or 0)

        # 静息心率
        try:
            hr = client.get_heart_rates(iso)
            if isinstance(hr, dict):
                stats.resting_hr = int(hr.get("restingHeartRate") or 0)
        except Exception:
            pass

        # 睡眠
        try:
            sleep = client.get_sleep_data(iso)
            stats.sleep_hours = self._extract_sleep_hours(sleep)
        except Exception:
            pass

        # 活动列表：爬升 / 单次最长 / 配速 / 心率 / 负荷（字段名随版本漂移，全部容错）
        try:
            acts = client.get_activities_by_date(iso, iso) or []
        except Exception:
            acts = []
        self._apply_activity_metrics(stats, acts, raw)

        stats.raw = raw
        return stats

    @staticmethod
    def _apply_activity_metrics(stats: DailyStats, acts, raw: dict) -> None:
        """从当日活动列表聚合爬升/单次最长/配速/心率/负荷。"""
        ascent = 0.0
        max_dist = 0.0
        load = 0.0
        hrs: list[int] = []
        best_pace = 0.0
        best_dist = 0.0

        if isinstance(acts, dict):
            acts = acts.get("activityList", []) or []

        # 当日运动次数（活动列表条目数），供「周数据/月数据」的「运动次数」字段
        stats.activities_count = len(acts)

        for a in acts or []:
            if not isinstance(a, dict):
                continue
            try:
                dist_m = float(a.get("distance") or 0)
                dist_km = dist_m / 1000.0
                ascent += float(a.get("elevationGain") or 0)
                max_dist = max(max_dist, dist_km)

                hr = a.get("averageHR") or a.get("averageHeartRate") or 0
                if hr:
                    hrs.append(int(hr))

                load += float(a.get("aerobicTrainingEffect") or 0)
                load += float(a.get("anaerobicTrainingEffect") or 0)

                speed = float(a.get("averageSpeed") or 0)
                if dist_km > best_dist and speed > 0:
                    best_dist = dist_km
                    best_pace = 1000.0 / speed  # m/s -> 秒/公里
            except (TypeError, ValueError):
                continue

        stats.ascent_meters = round(ascent, 2)
        stats.max_activity_distance_km = round(max_dist, 2)
        stats.avg_hr = int(round(sum(hrs) / len(hrs))) if hrs else 0
        stats.avg_pace_sec_per_km = round(best_pace, 1) if best_pace else 0.0
        # 负荷：优先活动训练效果之和，否则用强度分钟数近似
        stats.training_load = round(load, 2)
        if not stats.training_load:
            stats.training_load = float(
                raw.get("intensityMinutes")
                or raw.get("moderateIntensityMinutes")
                or raw.get("vigorousIntensityMinutes")
                or 0
            )

    @staticmethod
    def _extract_sleep_hours(sleep) -> float:
        """garminconnect 不同版本返回结构差异较大，这里做保守解析。"""
        if not isinstance(sleep, dict):
            return 0.0
        try:
            total = sleep.get("totalSleep") or sleep.get("sleepTimeSeconds") or 0
            if isinstance(total, (int, float)) and total > 0:
                return round(total / 3600, 2)
        except Exception:
            pass
        try:
            daily = sleep.get("dailySleepDTO") or {}
            duration = daily.get("sleepTimeSeconds")
            if duration:
                return round(duration / 3600, 2)
        except Exception:
            pass
        return 0.0
