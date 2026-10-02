from datetime import date, timedelta

from garminconnect import Garmin
from nonebot.log import logger

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
        "running",
        "run",
        "trail_running",
        "trail_run",
        "track_running",
        "track_run",
        "treadmill_running",
        "treadmill_run",
        "indoor_running",
        "indoor_run",
        "virtual_run",
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
        cred = self._require_cred(qq)
        client = Garmin(cred["email"], cred["password"], is_cn=cred.get("is_cn", True))
        client.login()
        return self._fetch_day(client, d)

    def fetch_range(self, qq: str, start: date, end: date) -> list[DailyStats]:
        """一次登录复用会话，逐日拉取 ``[start, end)`` 的统计数据（回填历史）。

        覆盖默认的「逐日 fetch_daily（每天重新登录）」——回填一个月历史时，默认实现
        会登录 31 次；这里登录一次即可，显著降低耗时与账号被封风险。单日失败跳过并
        记日志，与默认实现语义一致（返回列表即成功拉取的天）。
        """
        cred = self._require_cred(qq)
        client = Garmin(cred["email"], cred["password"], is_cn=cred.get("is_cn", True))
        client.login()
        out: list[DailyStats] = []
        d = start
        while d < end:
            try:
                out.append(self._fetch_day(client, d))
            except Exception as e:
                logger.warning(f"Garmin 回填 {qq} {d} 失败: {e}")
            d += timedelta(days=1)
        return out

    @staticmethod
    def _require_cred(qq: str) -> dict:
        cred = credentials.load(qq, "garmin")
        if not cred:
            raise RuntimeError("尚未绑定佳明，请私聊机器人发送「garmin绑定 邮箱 密码」")
        return cred

    def _fetch_day(self, client, d: date) -> DailyStats:
        """用已登录的 client 拉取某天数据（fetch_daily / fetch_range 共用）。"""
        iso = d.isoformat()
        stats = DailyStats(date=d)

        # 步数 / 距离 / 活动时长 / 消耗
        raw: dict = {}
        try:
            raw = client.get_stats(iso) or {}
        except Exception as e:
            self._warn("stats", e, "拉取全天指标")
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
        except Exception as e:
            self._warn("activities", e, "拉取活动列表")
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
            而非 aerobic/anaerobicTrainingEffect（训练效果 TE，0~5 分，语义不同）；
          - avg_pace_sec_per_km：各活动配速按距离加权（不是「最长那次」的配速）；
          - avg_hr：各活动心率按时长加权（时长越长的运动占比越高）。
        """
        total_dist_km = 0.0
        total_duration_s = 0.0
        total_calories = 0.0
        ascent = 0.0
        max_dist = 0.0
        load = 0.0
        hr_wsum = 0.0  # 心率 × 时长（按时长加权）
        hr_w = 0.0
        pace_wsum = 0.0  # 配速 × 距离（按距离加权）
        pace_w = 0.0

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
                dur_s = float(a.get("duration") or 0)
                if hr and dur_s > 0:
                    hr_wsum += int(hr) * dur_s
                    hr_w += dur_s

                # 训练负荷（activityTrainingLoad），不是训练效果（aerobic/anaerobicTrainingEffect）
                load += float(a.get("activityTrainingLoad") or 0)

                speed = float(a.get("averageSpeed") or 0)
                if speed > 0 and dist_km > 0:
                    pace_wsum += (1000.0 / speed) * dist_km  # m/s -> 秒/公里，按距离加权
                    pace_w += dist_km
            except (TypeError, ValueError):
                continue

        # 当日运动次数只计跑动类（与下方各指标口径一致），供「周数据/月数据」的「运动次数」字段
        stats.activities_count = running_count
        stats.distance_km = round(total_dist_km, 2)
        stats.active_minutes = int(total_duration_s // 60)
        stats.calories = int(total_calories)
        stats.ascent_meters = round(ascent, 2)
        stats.max_activity_distance_km = round(max_dist, 2)
        stats.avg_hr = int(round(hr_wsum / hr_w)) if hr_w > 0 else 0
        stats.avg_pace_sec_per_km = round(pace_wsum / pace_w, 1) if pace_w > 0 else 0.0
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
