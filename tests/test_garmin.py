"""Garmin 活动指标聚合口径测试：距离/负荷取「活动」，且只统计跑步类活动。"""

from datetime import date

from app.services.providers.base import DailyStats
from app.services.providers.garmin import GarminProvider


def _stats() -> DailyStats:
    return DailyStats(date=date(2026, 9, 20))


def _activity(type_key="running", **kw):
    a = dict(kw)
    a["activityType"] = {"typeKey": type_key}
    return a


def test_apply_activity_metrics_aggregates_activities():
    stats = _stats()
    acts = [
        _activity(
            "running",
            distance=5000,  # 米
            duration=1800,  # 秒
            calories=400,
            elevationGain=50,
            averageHR=150,
            activityTrainingLoad=80,
            aerobicTrainingEffect=5.0,  # 应被忽略（TE 不是负荷）
            anaerobicTrainingEffect=4.0,
            averageSpeed=2.78,
        ),
        _activity(
            "trail_running",
            distance=10000,
            duration=3600,
            calories=800,
            elevationGain=100,
            averageHR=160,
            activityTrainingLoad=120,
            averageSpeed=2.78,
        ),
    ]
    GarminProvider._apply_activity_metrics(stats, acts)

    assert stats.distance_km == 15.0  # (5000 + 10000) / 1000
    assert stats.active_minutes == 90  # (1800 + 3600) // 60
    assert stats.calories == 1200
    assert stats.ascent_meters == 150.0
    assert stats.max_activity_distance_km == 10.0
    assert stats.activities_count == 2
    # 训练负荷只取 activityTrainingLoad 之和，忽略 aerobic/anaerobicTrainingEffect
    assert stats.training_load == 200.0
    assert stats.avg_hr == 155  # (150 + 160) / 2


def test_apply_activity_metrics_dict_wrapper():
    # garminconnect 某些版本返回 {"activityList": [...]} 包裹结构
    stats = _stats()
    acts = {
        "activityList": [_activity("running", distance=3000, duration=900, calories=200)]
    }
    GarminProvider._apply_activity_metrics(stats, acts)
    assert stats.distance_km == 3.0
    assert stats.active_minutes == 15
    assert stats.calories == 200


def test_apply_activity_metrics_filters_non_running():
    # 只统计跑步；游泳、骑行、力量等一律跳过；typeKey 缺失也无法确认是跑步 → 跳过
    stats = _stats()
    acts = [
        _activity("running", distance=5000, duration=1800, calories=400),
        _activity("swimming", distance=3000, duration=1800, calories=300),
        _activity("road_biking", distance=20000, duration=3600, calories=600),
        {"distance": 1000, "duration": 600, "calories": 50},  # 无 activityType
    ]
    GarminProvider._apply_activity_metrics(stats, acts)

    assert stats.distance_km == 5.0  # 只算跑步 5km
    assert stats.active_minutes == 30
    assert stats.calories == 400
    assert stats.activities_count == 1
