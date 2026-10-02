from warden.events import (
    CRASH,
    EXPECTED,
    EXTERNAL_START,
    MANUAL_STOP,
    RESTART_AFTER_CRASH,
    UNEXPECTED_RESTART,
    EventClassifier,
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def ev(action, code=None):
    attrs = {"name": "icarus"}
    if code is not None:
        attrs["exitCode"] = str(code)
    return {"Type": "container", "Action": action, "Actor": {"Attributes": attrs}}


def run(classifier, clock, *events):
    out = []
    for e in events:
        clock.t += 1
        r = classifier.classify(e)
        out.append(r.kind if r else None)
    return out


def test_idle_shutdown_cycle_is_silent():
    clock = Clock()
    c = EventClassifier(clock=clock)
    assert run(c, clock, ev("die", 0), ev("start")) == [None, None]


def test_crash_then_docker_restart():
    clock = Clock()
    c = EventClassifier(clock=clock)
    assert run(c, clock, ev("die", 139), ev("start"), ev("restart")) == [CRASH, RESTART_AFTER_CRASH, None]


def test_oom_is_a_crash():
    clock = Clock()
    c = EventClassifier(clock=clock)
    clock.t += 1
    assert c.classify(ev("oom")) is None
    clock.t += 1
    result = c.classify(ev("die", 137))
    assert result.kind == CRASH and result.oom


def test_bot_operations_are_expected():
    clock = Clock()
    c = EventClassifier(clock=clock)
    c.begin_operation("Redémarrage")
    assert run(c, clock, ev("kill"), ev("die", 143), ev("stop"), ev("start"), ev("restart")) == [EXPECTED] * 5
    c.end_operation()
    clock.t += 10
    assert c.classify(ev("start")).kind == EXPECTED  # événement tardif
    clock.t += 60
    assert c.classify(ev("die", 1)).kind == CRASH


def test_manual_docker_stop_and_later_start():
    clock = Clock()
    c = EventClassifier(clock=clock)
    assert run(c, clock, ev("kill"), ev("die", 143), ev("stop")) == [None, MANUAL_STOP, None]
    clock.t += 600
    assert c.classify(ev("start")).kind == EXTERNAL_START


def test_manual_docker_restart():
    clock = Clock()
    c = EventClassifier(clock=clock)
    kinds = run(c, clock, ev("kill"), ev("die", 0), ev("stop"), ev("start"), ev("restart"))
    assert kinds == [None, MANUAL_STOP, None, None, UNEXPECTED_RESTART]


def test_custom_clean_exit_codes():
    clock = Clock()
    c = EventClassifier(clean_exit_codes=frozenset({0, 1}), clock=clock)
    assert run(c, clock, ev("die", 1), ev("start")) == [None, None]
    assert run(c, clock, ev("die", 2)) == [CRASH]


def test_ignores_exec_and_health_events():
    clock = Clock()
    c = EventClassifier(clock=clock)
    assert run(c, clock, ev("exec_start: bash"), ev("health_status: healthy")) == [None, None]


def test_start_without_history_is_external():
    clock = Clock()
    c = EventClassifier(clock=clock)
    assert run(c, clock, ev("start")) == [EXTERNAL_START]
