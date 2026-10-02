import os

import pytest

from warden import settings_store as ss

COMPOSE = """
services:
  icarus:
    environment:
      - SERVERNAME=${SERVERNAME}
      - JOIN_PASSWORD=${JOIN_PASSWORD}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD}
      - MAX_PLAYERS=${MAX_PLAYERS:-8}
      - SHUTDOWN_EMPTY_FOR=300
      - STEAM_USERID=1000
"""


def test_parse_env_formats():
    text = (
        "# commentaire\n"
        "\n"
        "SERVERNAME=ZEHEFLAND\n"
        "export MAX_PLAYERS=8\n"
        "JOIN_PASSWORD='p@ss word#1'\n"
        'ADMIN_PASSWORD="a\\"b"\n'
        "GAMESAVEFREQUENCY=10 # minutes\n"
        "EMPTY=\n"
    )
    env = ss.parse_env(text)
    assert env == {
        "SERVERNAME": "ZEHEFLAND",
        "MAX_PLAYERS": "8",
        "JOIN_PASSWORD": "p@ss word#1",
        "ADMIN_PASSWORD": 'a"b',
        "GAMESAVEFREQUENCY": "10",
        "EMPTY": "",
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ZEHEFLAND", "ZEHEFLAND"),
        ("", ""),
        ("Mon serveur", "'Mon serveur'"),
        ("pa$$word", "'pa$$word'"),  # quotes simples : pas d'interpolation par compose
        ("l'île", '"l\'île"'),
    ],
)
def test_format_value(value, expected):
    assert ss.format_value(value) == expected


@pytest.mark.parametrize("value", ["a\nb", "it's $HOME"])
def test_format_value_rejects(value):
    with pytest.raises(ss.SettingError):
        ss.format_value(value)


@pytest.mark.parametrize("value", ["ZEHEFLAND", "Mon serveur", "pa$$ w'rd".replace("$", ""), "x#y z", "a=b"])
def test_format_then_parse_roundtrip(value):
    assert ss.parse_env(f"K={ss.format_value(value)}\n")["K"] == value


def test_update_env_preserves_comments_order_and_unknown_keys():
    text = "# Icarus\nJOIN_PASSWORD=old\n# fin\nOTHER=1\nSERVERNAME=A\nSERVERNAME=duplicate\n"
    out = ss.update_env_text(text, {"SERVERNAME": "Nouveau nom", "MAX_PLAYERS": "6"})
    assert out == "# Icarus\nJOIN_PASSWORD=old\n# fin\nOTHER=1\nSERVERNAME='Nouveau nom'\nMAX_PLAYERS=6\n"


def test_write_env_creates_backup_and_keeps_mode(tmp_path):
    path = tmp_path / ".env"
    path.write_text("JOIN_PASSWORD=secret\nADMIN_PASSWORD=admin\n", encoding="utf-8")
    os.chmod(path, 0o600)
    backup = ss.write_env(path, {"SERVERNAME": "ZEHEFLAND"})
    assert backup is not None and backup.read_text() == "JOIN_PASSWORD=secret\nADMIN_PASSWORD=admin\n"
    assert ss.read_env(path) == {"JOIN_PASSWORD": "secret", "ADMIN_PASSWORD": "admin", "SERVERNAME": "ZEHEFLAND"}
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


def test_restore_env_backup(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    ss.write_env(path, {"A": "2"})
    assert ss.restore_env_backup(path)
    assert ss.read_env(path) == {"A": "1"}


def test_validate_bool_and_int():
    bool_def = ss.SETTINGS_BY_KEY["RESUME_PROSPECT"]
    assert ss.validate(bool_def, "oui") == "True"
    assert ss.validate(bool_def, "FALSE") == "False"
    with pytest.raises(ss.SettingError):
        ss.validate(bool_def, "peut-être")

    players = ss.SETTINGS_BY_KEY["MAX_PLAYERS"]
    assert ss.validate(players, " 6 ") == "6"
    for bad in ("0", "9", "huit"):
        with pytest.raises(ss.SettingError):
            ss.validate(players, bad)

    shutdown = ss.SETTINGS_BY_KEY["SHUTDOWN_EMPTY_FOR"]
    assert ss.validate(shutdown, "-1") == "-1"


def test_validate_strings_and_secrets():
    name = ss.SETTINGS_BY_KEY["SERVERNAME"]
    assert ss.validate(name, "  ZEHEFLAND ") == "ZEHEFLAND"
    with pytest.raises(ss.SettingError):
        ss.validate(name, "   ")
    with pytest.raises(ss.SettingError):
        ss.validate(name, "x" * 65)

    secret = ss.SETTINGS_BY_KEY["JOIN_PASSWORD"]
    assert ss.validate(secret, " espace ") == " espace "  # un mot de passe n'est pas « nettoyé »
    assert ss.validate(secret, "") == ""


def test_secrets_never_displayed():
    secret = ss.SETTINGS_BY_KEY["ADMIN_PASSWORD"]
    shown = ss.display_value(secret, "SuperSecret42")
    assert "SuperSecret42" not in shown and ss.SECRET_MASK in shown
    assert ss.display_value(secret, "") == "(aucun)"
    assert ss.display_value(ss.SETTINGS_BY_KEY["SAVEGAMEONEXIT"], "True") == "✅ Oui"


def test_editable_keys_follow_compose_references():
    keys = ss.editable_keys(COMPOSE)
    assert {"SERVERNAME", "JOIN_PASSWORD", "ADMIN_PASSWORD", "MAX_PLAYERS"} <= keys
    assert "SHUTDOWN_EMPTY_FOR" not in keys  # en dur dans le compose


def test_pending_keys_compare_with_container_env():
    env_values = {"SERVERNAME": "Nouveau", "MAX_PLAYERS": "8", "JOIN_PASSWORD": "x"}
    running = ss.container_env(["SERVERNAME=Ancien", "MAX_PLAYERS=8", "JOIN_PASSWORD=x", "PATH=/usr/bin"])
    assert ss.pending_keys(env_values, running, ["SERVERNAME", "MAX_PLAYERS", "JOIN_PASSWORD"]) == {"SERVERNAME"}
