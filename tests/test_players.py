from warden.players import build_snapshot, diff, players_line


def snap(count, names=None, max_players=8):
    return build_snapshot("ZEHEFLAND", count, max_players, names)


def test_names_unavailable_when_empty_or_inconsistent():
    assert snap(2, ["", ""]).names is None
    assert snap(2, ["Alice"]).names is None
    assert snap(2, None).names is None
    assert snap(2, ["bob", "Alice"]).names == ("Alice", "bob")


def test_diff_by_count_without_names():
    assert diff(snap(0, []), snap(1, [""])) == [("join", "👋 Un joueur a rejoint la partie (1/8 joueurs)")]
    assert diff(snap(1), snap(3)) == [("join", "👋 2 joueurs ont rejoint la partie (3/8 joueurs)")]
    assert diff(snap(3), snap(2)) == [("leave", "🚪 Un joueur a quitté la partie (2/8 joueurs)")]
    assert diff(snap(2), snap(2)) == []


def test_diff_by_names():
    events = diff(snap(2, ["Alice", "Bob"]), snap(2, ["Alice", "Chloé"]))
    assert events == [
        ("join", "👋 **Chloé** a rejoint la partie (2/8 joueurs)"),
        ("leave", "🚪 **Bob** a quitté la partie (2/8 joueurs)"),
    ]


def test_diff_ignores_missing_snapshots():
    assert diff(None, snap(2)) == []
    assert diff(snap(2), None) == []


def test_players_line():
    assert players_line(snap(2)) == "👥 **2/8 joueurs**"
    assert players_line(snap(2, ["Alice", "Bob"])) == "👥 **2/8 joueurs** : Alice, Bob"
    assert "ne répond pas" in players_line(None)


def test_player_names_cannot_inject_markdown():
    from warden.players import Snapshot, diff, players_line

    evil = "[Clique](https://evil.example)"
    before = Snapshot("S", 0, 8, ())
    after = Snapshot("S", 1, 8, (evil,))
    (_, text), = diff(before, after)
    assert r"\[Clique]" in text
    assert r"\[Clique]" in players_line(after)


# --------------------------------------------------------------------------- PresenceTracker
def feed(tracker, *steps):
    """steps: (seconds, snapshot or None) -> published texts."""
    out = []
    for t, s in steps:
        out += [text for _kind, text in tracker.update(s, t)]
    return out


def test_connecting_reconnection_is_a_single_arrival():
    """Reported case: Connect -> lobby -> prospect load drops the player for a few seconds."""
    from warden.players import PresenceTracker

    tracker = PresenceTracker(leave_grace=120)
    texts = feed(tracker, (0, snap(0)), (20, snap(1)), (40, snap(0)), (60, snap(1)), (80, snap(1)), (300, snap(1)))
    assert texts == ["👋 Un joueur a rejoint la partie (1/8 joueurs)"]


def test_real_departure_is_announced_once_after_the_grace():
    from warden.players import PresenceTracker

    tracker = PresenceTracker(leave_grace=120)
    texts = feed(tracker, (0, snap(1)), (20, snap(0)), (100, snap(0)))
    assert texts == []  # still within the grace
    texts = feed(tracker, (140, snap(0)), (160, snap(0)), (400, snap(0)))
    assert texts == ["🚪 Un joueur a quitté la partie (0/8 joueurs)"]


def test_names_reconnection_and_departure():
    from warden.players import PresenceTracker

    tracker = PresenceTracker(leave_grace=120)
    texts = feed(
        tracker,
        (0, snap(1, ["Alice"])),
        (20, snap(2, ["Alice", "Bob"])),  # Bob arrives
        (40, snap(1, ["Alice"])),  # Bob reconnecting
        (60, snap(2, ["Alice", "Bob"])),  # back: nothing
        (80, snap(1, ["Bob"])),  # Alice leaves for real
        (210, snap(1, ["Bob"])),
    )
    assert texts == [
        "👋 **Bob** a rejoint la partie (2/8 joueurs)",
        "🚪 **Alice** a quitté la partie (1/8 joueurs)",
    ]


def test_a2s_silence_and_server_stop_publish_nothing():
    from warden.players import PresenceTracker

    tracker = PresenceTracker(leave_grace=120)
    assert feed(tracker, (0, snap(2)), (20, None), (40, None), (500, None)) == []
    tracker.reset()  # server stopped
    assert feed(tracker, (600, snap(0)), (620, snap(0))) == []  # new baseline, no "2 left"


def test_count_dip_then_more_players():
    from warden.players import PresenceTracker

    tracker = PresenceTracker(leave_grace=120)
    texts = feed(tracker, (0, snap(2)), (20, snap(1)), (40, snap(3)))
    assert texts == ["👋 Un joueur a rejoint la partie (3/8 joueurs)"]


def test_zero_grace_keeps_the_immediate_behaviour():
    from warden.players import PresenceTracker

    tracker = PresenceTracker(leave_grace=0)
    texts = feed(tracker, (0, snap(1)), (20, snap(0)))
    assert texts == ["🚪 Un joueur a quitté la partie (0/8 joueurs)"]
