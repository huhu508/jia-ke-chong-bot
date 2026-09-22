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

    # 跑动类活动的 typeKey 白名单；游泳/骑行/力量等一律不统计（群里反馈游泳数据混入跑步）。
    # 跑步/越野跑/场地跑/跑步机/室内跑/虚拟跑/徒步；walking（步行/散步）刻意不计入，避免混入日常步数。
    _RUNNING_TYPE_KEYS = {
        "running", "run", "trail_running", "trail_run",
        "track_running", "track_run", "treadmill_running", "treadmill_run",
        "indoor_running", "indoor_run", "virtual_run",
        "hiking",
    }

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
        # 距离 / 时长 / 消耗 不取 get_stats 的「全天」口径——totalDistanceMeters 含日常步行、
        # activeSeconds / activeKilocalories 含日常活动，会高估运动量。这三项改由
        # _apply_activity_metrics 从当日活动列表聚合，只统计用户主动记录的运动。

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
        self._apply_activity_metrics(stats, acts)

        stats.raw = raw
        return stats

    @staticmethod
    def _is_running_activity(a) -> bool:
        """判断一条 Garmin 活动是否为跑动类（按 activityType.typeKey 白名单）。

        typeKey 缺失（罕见）时按「无法确认是跑动」跳过，宁可少算也不把游泳/骑行混入。
        """
        at = a.get("activityType") if isinstance(a, dict) else None
        if not isinstance(at, dict):
            return False
        return str(at.get("typeKey") or "").lower() in GarminProvider._RUNNING_TYPE_KEYS

    @staticmethod
    def _apply_activity_metrics(stats: DailyStats, acts) -> None:
        """从当日活动列表聚合运动指标（距离/时长/消耗/爬升/配速/心率/负荷）。

        与 get_stats 的「全天」口径区分：这里只统计用户主动记录的运动活动，
          - distance_km / active_minutes / calories：活动 distance/duration/calories 求和，
            而非 totalDistanceMeters（含日常步行）/ activeSeconds / activeKilocalories；
          - training_load：取活动 activityTrainingLoad（训练负荷）之和，
            而非 aerobic/anaerobicTrainingEffect（训练效果 TE，0~5 分，语义不同）。
        """
        total_dist_km = 0.0
        total_duration_s = 0.0
        total_calories = 0.0
        ascent = 0.0
        max_dist = 0.0
        load = 0.0
        hrs: list[int] = []
        best_pace = 0.0
        best_dist = 0.0

        if isinstance(acts, dict):
            acts = acts.get("activityList", []) or []

        running_count = 0

        for a in acts or []:
            if not isinstance(a, dict):
                continue
            if not GarminProvider._is_running_activity(a):
                continue  # 只统计跑动类，游泳/骑行/力量等一律跳过
            running_count += 1
            try:
                dist_m = float(a.get("distance") or 0)
                dist_km = dist_m / 1000.0
                total_dist_km += dist_km
                total_duration_s += float(a.get("duration") or 0)
                total_calories += float(a.get("calories") or 0)
                ascent += float(a.get("elevationGain") or 0)
                max_dist = max(max_dist, dist_km)

                hr = a.get("averageHR") or a.get("averageHeartRate") or 0
                if hr:
                    hrs.append(int(hr))

                # 训练负荷（activityTrainingLoad），不是训练效果（aerobic/anaerobicTrainingEffect）
                load += float(a.get("activityTrainingLoad") or 0)

                speed = float(a.get("averageSpeed") or 0)
                if dist_km > best_dist and speed > 0:
                    best_dist = dist_km
                    best_pace = 1000.0 / speed  # m/s -> 秒/公里
            except (TypeError, ValueError):
                continue

        # 当日运动次数只计跑动类（与下方各指标口径一致），供「周数据/月数据」的「运动次数」字段
        stats.activities_count = running_count
        stats.distance_km = round(total_dist_km, 2)
        stats.active_minutes = int(total_duration_s // 60)
        stats.calories = int(total_calories)
        stats.ascent_meters = round(ascent, 2)
        stats.max_activity_distance_km = round(max_dist, 2)
        stats.avg_hr = int(round(sum(hrs) / len(hrs))) if hrs else 0
        stats.avg_pace_sec_per_km = round(best_pace, 1) if best_pace else 0.0
        stats.training_load = round(load, 2)

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
