"""打卡体系测试：天数 / 里程碑 / 礼物先到先得 / 节日彩蛋 / 群抽奖的幂等与口径。"""

from datetime import date, timedelta

from app.services import checkin, timeutil


def _add_days(session, qq, n) -> int:
    """从计数起点起连续插入 n 个打卡日并提交，返回实际新增天数。"""
    start = checkin.counting_start()
    added = 0
    for i in range(n):
        if checkin.ensure_day(qq, start + timedelta(days=i), session):
            added += 1
    session.commit()
    return added


def test_ensure_day_idempotent(db_session):
    s = db_session
    d = checkin.counting_start()
    assert checkin.ensure_day("10001", d, s) is True
    s.commit()
    # 同一天再记一次应被唯一约束挡住（返回 False）
    assert checkin.ensure_day("10001", d, s) is False
    assert checkin.total_days("10001", s) == 1


def test_ensure_day_ignores_before_semester(db_session):
    s = db_session
    before = checkin.counting_start() - timedelta(days=1)
    assert checkin.ensure_day("10001", before, s) is False


def test_total_and_month_days(db_session):
    s = db_session
    start = checkin.counting_start()
    assert _add_days(s, "10001", 3) == 3
    assert checkin.total_days("10001", s) == 3
    assert checkin.month_days("10001", start, start + timedelta(days=2), s) == 2
    assert checkin.month_days("10001", start + timedelta(days=2), start + timedelta(days=3), s) == 1


def test_crossed_milestones_and_idempotent(db_session):
    s = db_session
    _add_days(s, "10001", 10)
    total = checkin.total_days("10001", s)
    assert total == 10
    hit = checkin.crossed_milestones("10001", total, s)
    s.commit()
    assert hit == [10]
    # 幂等：再次检查不重复触发
    assert checkin.crossed_milestones("10001", total, s) == []


def test_crossed_milestones_skip_multiple(db_session):
    s = db_session
    _add_days(s, "10001", 66)
    hit = checkin.crossed_milestones("10001", checkin.total_days("10001", s), s)
    s.commit()
    # 一次跨过 10 和 66 两个里程碑
    assert hit == [10, 66]


def test_auto_gift_first_come_first_served(db_session):
    s = db_session
    ranks = []
    for i in range(checkin.GIFT_QUOTA):
        r = checkin.auto_gift(f"qq{i}", checkin.GIFT_DAYS, s)
        s.flush()  # 让 gift_claims 的 count 查询看到刚写入的领取状态
        ranks.append(r)
    assert ranks == list(range(1, checkin.GIFT_QUOTA + 1))
    # 满额后不再发放
    assert checkin.auto_gift("qq_overflow", checkin.GIFT_DAYS, s) is None


def test_auto_gift_idempotent_per_member(db_session):
    s = db_session
    assert checkin.auto_gift("10001", checkin.GIFT_DAYS, s) == 1
    s.flush()
    # 同一成员不重复领取
    assert checkin.auto_gift("10001", checkin.GIFT_DAYS, s) is None


def test_match_festival_hits():
    f = checkin.match_festival(date(2026, 10, 1), 10.01)
    assert f is not None and f["name"] == "国庆"
    # 距离不符不触发
    assert checkin.match_festival(date(2026, 10, 1), 5.0) is None
    # 日期不符不触发
    assert checkin.match_festival(date(2026, 10, 2), 10.01) is None


def test_check_festival_idempotent(db_session):
    s = db_session
    d = date(2026, 10, 1)
    f = checkin.check_festival("10001", d, 10.01, s)
    assert f is not None and f["name"] == "国庆"
    s.commit()
    # 同成员同年同节日不重复触发
    assert checkin.check_festival("10001", d, 10.01, s) is None


def test_draw_lottery_picks_from_checkins(db_session):
    s = db_session
    _add_days(s, "qq1", 3)
    _add_days(s, "qq2", 1)
    winners = checkin.draw_lottery(s)
    assert len(winners) == checkin.LOTTERY_WINNERS
    assert winners[0] in ("qq1", "qq2")


def test_next_milestone():
    assert checkin.next_milestone(0) == 10
    assert checkin.next_milestone(10) == 66
    assert checkin.next_milestone(99) == 100
    assert checkin.next_milestone(365) is None


def test_global_total(db_session):
    s = db_session
    _add_days(s, "qq1", 2)
    _add_days(s, "qq2", 3)
    assert checkin.global_total(s) == 5


def test_counting_start_fallback_on_bad_config(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "semester_start", "not-a-date")
    assert checkin.counting_start() == timeutil.today()
