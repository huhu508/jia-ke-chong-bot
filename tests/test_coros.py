"""COROS 运动记录解析测试：只统计跑步类（100/102/103），游泳/骑行等跳过。"""

from app.services.providers.coros import CorosProvider


def test_parse_sport_records_filters_running_only():
    text = (
        "(3 records)\n"
        "Duration: 40:00 | Distance: 8.00 km\n"
        "Average Pace: 5:00 /km | Avg HR: 160 bpm | Calories: 598 kcal\n"
        "LabelId: 4803 | SportType: 100\n"
        "\n"
        "Duration: 30:00 | Distance: 2.00 km\n"
        "Average Pace: 15:00 /km | Avg HR: 120 bpm | Calories: 200 kcal\n"
        "LabelId: 4804 | SportType: 302\n"  # 游泳（非跑步）
        "\n"
        "Duration: 1:00:00 | Distance: 25.00 km\n"
        "Average Pace: 2:24 /km | Avg HR: 140 bpm | Calories: 900 kcal\n"
        "LabelId: 4805 | SportType: 200\n"  # 骑行（非跑步）
    )
    r = CorosProvider._parse_sport_records(text)

    assert r["total_distance_km"] == 8.0  # 只算跑步 8km
    assert r["count"] == 1
    assert r["max_distance_km"] == 8.0
    assert r["avg_pace_sec_per_km"] == 300.0  # 40:00 / 8km
    assert r["avg_hr"] == 160
    assert r["activities"] == [("4803", 100)]


def test_parse_sport_records_trail_and_track_counted():
    # 越野跑 102 / 场地跑 103 也算跑步
    text = (
        "(2 records)\n"
        "Duration: 50:00 | Distance: 10.00 km\n"
        "Average Pace: 5:00 /km | Avg HR: 150 bpm\n"
        "LabelId: 1 | SportType: 102\n"
        "\n"
        "Duration: 20:00 | Distance: 4.00 km\n"
        "Average Pace: 5:00 /km | Avg HR: 145 bpm\n"
        "LabelId: 2 | SportType: 103\n"
    )
    r = CorosProvider._parse_sport_records(text)
    assert r["total_distance_km"] == 14.0
    assert r["count"] == 2
    assert r["activities"] == [("1", 102), ("2", 103)]


def test_parse_sport_records_empty():
    assert CorosProvider._parse_sport_records("") == {
        "total_distance_km": 0.0,
        "count": 0,
        "max_distance_km": 0.0,
        "avg_pace_sec_per_km": 0.0,
        "avg_hr": 0,
        "activities": [],
    }
