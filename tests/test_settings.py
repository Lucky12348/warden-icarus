import os

import pytest

from warden import settings_store as ss

COMPOSE = """
services:
  icarus:
    image: mornedhels/icarus-server:latest
    environment:
      - SERVER_NAME=${SERVERNAME}
      - SERVER_PASSWORD=${JOIN_PASSWORD}
      - SERVER_ADMIN_PASSWORD=${ADMIN_PASSWORD}
      - SERVER_MAX_PLAYERS=${MAX_PLAYERS:-8}
      - SERVER_SHUTDOWN_IF_EMPTY=300
      - PUID=1000
"""

INI = (
    "[/Script/Icarus.DedicatedServerSettings]\n"
    "SessionName=ZEHEFLAND\n"
    "JoinPassword=secret\n"
    "SaveGameOnExit=True\n"
    "GameSaveFrequency=10\n"
    "LastProspectName=Olympus\n"
    "\n"
    "[/Script/Autre.Section]\n"
    "SaveGameOnExit=ne pas toucher\n"
)


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
        ("pa$$word", "'pa$$word'"),  # single quotes: no interpolation by compose
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
    assert ss.validate(secret, " espace ") == " espace "  # a password is not "cleaned"
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
    assert "SHUTDOWN_EMPTY_FOR" not in keys  # hard-coded in the compose file


def test_ini_settings_are_always_editable():
    keys = ss.editable_keys(COMPOSE)
    assert {"SAVEGAMEONEXIT", "GAMESAVEFREQUENCY", "FIBERFOLIAGERESPAWN", "LARGESTONERESPAWN"} <= keys
    assert all(ss.SETTINGS_BY_KEY[k].storage == "ini" for k in ("SAVEGAMEONEXIT", "LARGESTONERESPAWN"))
    assert ss.SETTINGS_BY_KEY["LARGESTONERESPAWN"].ini_key == "LargeStonesRespawn"


def test_pending_keys_compare_with_translated_container_env():
    env_values = {"SERVERNAME": "Nouveau", "MAX_PLAYERS": "8", "JOIN_PASSWORD": "x", "SAVEGAMEONEXIT": "True"}
    running = ss.container_env(["SERVER_NAME=Ancien", "SERVER_MAX_PLAYERS=8", "SERVER_PASSWORD=x", "PATH=/usr/bin"])
    keys = ["SERVERNAME", "MAX_PLAYERS", "JOIN_PASSWORD", "SAVEGAMEONEXIT"]
    # SAVEGAMEONEXIT (left over from an old .env) is no longer a variable: never "pending".
    assert ss.pending_keys(env_values, running, keys) == {"SERVERNAME"}


def test_parse_ini_section_ignores_other_sections():
    values = ss.parse_ini_section(INI)
    assert values["SaveGameOnExit"] == "True" and values["GameSaveFrequency"] == "10"
    assert ss.ini_lookup(values, "savegameonexit") == "True"
    assert ss.ini_lookup(values, "FiberFoliageRespawn") is None


def test_update_ini_replaces_in_place_and_appends_missing_keys():
    out = ss.update_ini_text(INI, {"SaveGameOnExit": "False", "LargeStonesRespawn": "True"})
    assert out == (
        "[/Script/Icarus.DedicatedServerSettings]\n"
        "SessionName=ZEHEFLAND\n"
        "JoinPassword=secret\n"
        "SaveGameOnExit=False\n"
        "GameSaveFrequency=10\n"
        "LastProspectName=Olympus\n"
        "LargeStonesRespawn=True\n"
        "\n"
        "[/Script/Autre.Section]\n"
        "SaveGameOnExit=ne pas toucher\n"
    )


def test_update_ini_is_case_insensitive_and_keeps_crlf():
    text = "[/Script/Icarus.DedicatedServerSettings]\r\nsavegameonexit=True\r\n"
    out = ss.update_ini_text(text, {"SaveGameOnExit": "False"})
    assert out == "[/Script/Icarus.DedicatedServerSettings]\r\nsavegameonexit=False\r\n"


def test_update_ini_creates_missing_section():
    out = ss.update_ini_text("[Autre]\nA=1\n", {"GameSaveFrequency": "5"})
    assert out == "[Autre]\nA=1\n\n[/Script/Icarus.DedicatedServerSettings]\nGameSaveFrequency=5\n"
    with pytest.raises(ss.SettingError):
        ss.update_ini_text(INI, {"SessionName": "a\nb"})


def test_write_ini_creates_backup(tmp_path):
    path = tmp_path / "ServerSettings.ini"
    path.write_bytes(INI.encode("utf-8"))
    backup = ss.write_ini(path, {"GameSaveFrequency": "15"})
    assert backup.name == "ServerSettings.ini.bak" and backup.read_bytes() == INI.encode("utf-8")
    values = ss.read_ini(path)
    assert values["GameSaveFrequency"] == "15" and values["LastProspectName"] == "Olympus"
    assert b"\r" not in path.read_bytes()  # original line endings kept
    assert not list(tmp_path.glob(".*.tmp"))


def test_write_ini_keeps_utf16_encoding(tmp_path):
    path = tmp_path / "ServerSettings.ini"
    path.write_bytes(INI.replace("\n", "\r\n").encode("utf-16"))
    ss.write_ini(path, {"FiberFoliageRespawn": "False"})
    raw = path.read_bytes()
    assert raw.startswith(b"\xff\xfe")
    text = raw.decode("utf-16")
    assert "FiberFoliageRespawn=False\r\n" in text and "\r\r" not in text


def test_write_ini_requires_existing_file(tmp_path):
    with pytest.raises(ss.SettingError, match="introuvable"):
        ss.write_ini(tmp_path / "ServerSettings.ini", {"SaveGameOnExit": "True"})


def test_current_values_read_each_storage():
    env = {"SERVERNAME": "ZEHEFLAND", "SAVEGAMEONEXIT": "False"}  # old variable ignored
    values = ss.current_values(env, ss.parse_ini_section(INI))
    assert values["SERVERNAME"] == "ZEHEFLAND"
    assert values["SAVEGAMEONEXIT"] == "True"  # read from the .ini
    assert "FIBERFOLIAGERESPAWN" not in values


def test_update_ini_replaces_every_duplicate_key():
    text = "[/Script/Icarus.DedicatedServerSettings]\nSaveGameOnExit=True\nSaveGameOnExit=True\n"
    out = ss.update_ini_text(text, {"SaveGameOnExit": "False"})
    assert out == "[/Script/Icarus.DedicatedServerSettings]\nSaveGameOnExit=False\nSaveGameOnExit=False\n"
