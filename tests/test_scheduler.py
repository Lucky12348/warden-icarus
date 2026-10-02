from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from warden.scheduler import Action, decide, next_target, parse_hhmm

TZ = ZoneInfo("Europe/Paris")
SIX = time(6, 0)


def at(h, m, day=2):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def test_parse_hhmm():
    assert parse_hhmm("06:00") == time(6, 0)
    assert parse_hhmm(" 23:59 ") == time(23, 59)
    for bad in ("6h", "24:00", "12:60", "", "1:2:3"):
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_disabled_does_nothing():
    assert decide(at(6, 0), False, SIX, None, True, 0).action == Action.NONE


def test_full_day_without_players():
    d = decide(at(5, 50), True, SIX, None, True, 0)
    assert d.action == Action.NONE
    d = decide(at(5, 55), True, SIX, d.run, True, 0)
    assert d.action == Action.ANNOUNCE
    d = decide(at(5, 57), True, SIX, d.run, True, 0)
    assert d.action == Action.NONE  # une seule annonce
    d = decide(at(6, 0), True, SIX, d.run, True, 0)
    assert d.action == Action.RESTART
    d = decide(at(6, 1), True, SIX, d.run, True, 0)
    assert d.action == Action.NONE  # une seule fois par jour


def test_postpone_every_15_minutes_then_give_up():
    run = decide(at(5, 56), True, SIX, None, True, 2).run
    d = decide(at(6, 0), True, SIX, run, True, 2)
    assert d.action == Action.POSTPONE and d.run.next_attempt == at(6, 15)
    assert decide(at(6, 10), True, SIX, d.run, True, 2).action == Action.NONE
    attempts = 1
    now = at(6, 15)
    while True:
        d = decide(now, True, SIX, d.run, True, 1)
        attempts += 1
        if d.action != Action.POSTPONE:
            break
        now = d.run.next_attempt
    assert d.action == Action.GIVE_UP
    assert now == at(8, 0)
    assert attempts == 9  # 6:00, 6:15 … 8:00


def test_restart_when_players_leave():
    run = decide(at(6, 0), True, SIX, None, True, 1).run
    d = decide(at(6, 15), True, SIX, run, True, 0)
    assert d.action == Action.RESTART


def test_a2s_down_is_treated_as_busy_by_default():
    d = decide(at(6, 0), True, SIX, None, True, None)
    assert d.action == Action.POSTPONE
    d = decide(at(6, 0), True, SIX, None, True, None, restart_if_a2s_down=True)
    assert d.action == Action.RESTART


def test_stopped_server_is_skipped():
    d = decide(at(6, 0), True, SIX, None, False, None)
    assert d.action == Action.SKIP_STOPPED
    assert decide(at(6, 30), True, SIX, d.run, True, 0).action == Action.NONE


def test_late_bot_start_inside_window_still_restarts():
    d = decide(at(7, 10), True, SIX, None, True, 0)
    assert d.action == Action.RESTART


def test_after_window_targets_next_day():
    assert next_target(at(8, 1), SIX) == at(6, 0, day=3)
    d = decide(at(9, 0), True, SIX, None, True, 0)
    assert d.action == Action.NONE and d.run.target == at(6, 0, day=3)


def test_announce_before_midnight_target():
    d = decide(datetime(2026, 10, 2, 23, 57, tzinfo=TZ), True, time(0, 0), None, True, 0)
    assert d.action == Action.ANNOUNCE
    assert d.run.target == datetime(2026, 10, 3, 0, 0, tzinfo=TZ)


def test_time_change_creates_new_run():
    run = decide(at(6, 0), True, SIX, None, True, 0).run
    assert run.done
    d = decide(at(6, 55), True, time(7, 0), run, True, 0)
    assert d.action == Action.ANNOUNCE and d.run.target == at(7, 0)


def test_deadline_property():
    run = decide(at(6, 0), True, SIX, None, True, 3).run
    assert run.deadline - run.target == timedelta(hours=2)
