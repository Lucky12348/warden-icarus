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
