import pytest

from warden.antispam import AntiSpam, normalize


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_normalize_ignores_numbers_and_ids():
    a = normalize("[2026.10.02-06.00.01:123][  5]LogNet: Error 42 at 0x7ffd1234abcd")
    b = normalize("[2026.10.02-06.05.09:999][ 17]LogNet: Error 43 at 0x7ffd9999ffff")
    assert a == b


def test_dedupe_within_window():
    clock = Clock()
    spam = AntiSpam(max_events=100, dedupe_seconds=300, clock=clock)
    assert spam.allow("Error 1")
    assert not spam.allow("Error 2")  # même ligne, chiffres près
    clock.t += 299
    assert not spam.allow("Error 3")
    clock.t += 2  # 301 s après la première
    assert spam.allow("Error 4")
    assert spam.pop_suppressed() == 2
    assert spam.pop_suppressed() == 0


def test_rate_limit_sliding_window():
    clock = Clock()
    spam = AntiSpam(max_events=3, window_seconds=60, dedupe_seconds=0, clock=clock)
    assert all(spam.allow(f"line {c}") for c in "abc")
    assert not spam.allow("line d")
    clock.t += 59
    assert not spam.allow("line e")
    clock.t += 1
    assert spam.allow("line f")
    assert spam.pop_suppressed() == 2


def test_custom_key():
    clock = Clock()
    spam = AntiSpam(max_events=10, dedupe_seconds=120, clock=clock)
    assert spam.allow("💥 Crash code 1", key="crash")
    assert not spam.allow("💥 Crash code 139", key="crash")
    assert spam.allow("🔁 Restart", key="restart")


def test_suppressed_lines_do_not_consume_quota():
    clock = Clock()
    spam = AntiSpam(max_events=2, window_seconds=60, dedupe_seconds=300, clock=clock)
    assert spam.allow("same")
    for _ in range(10):
        assert not spam.allow("same")
    assert spam.allow("other")


def test_invalid_config():
    with pytest.raises(ValueError):
        AntiSpam(max_events=0)
