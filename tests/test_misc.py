"""Fonctions pures annexes : logs, Docker (calculs), configuration, état, jetons."""

import json
from datetime import timezone

import pytest

from warden.config import MIN_STOP_TIMEOUT, Config, ConfigError
from warden.docker_ctl import compute_cpu_percent, compute_memory, parse_docker_time, read_buildid
from warden.logpatterns import LogPatterns
from warden.pending import Pending
from warden.state import State

BASE_ENV = {
    "DISCORD_TOKEN": "x",
    "GUILD_ID": "1",
    "ADMIN_ROLE_ID": "2",
    "CHANNEL_PANEL_ID": "3",
    "CHANNEL_BACKUPS_ID": "4",
    "CHANNEL_SETTINGS_ID": "5",
    "CHANNEL_LOGS_ID": "6",
}


def test_log_patterns_defaults():
    p = LogPatterns.load(None)
    assert p.classify("0024:fixme:ntdll:NtQuerySystemInformation info") is None
    assert p.classify("Success! App '2089300' fully installed.").category == "update"
    assert p.classify("\x1b[31mLogWindows: Error: something bad\x1b[0m").category == "error"
    assert p.classify("=== Critical error: ===").category == "crash"
    assert p.classify("LogInit: Display: Engine is initialized.").category == "startup"
    assert p.classify("LogTemp: banal message") is None


def test_log_patterns_file_with_player_names(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"join": [r"Join succeeded: (?P<name>.+)"]}), encoding="utf-8")
    match = LogPatterns.load(path).classify("LogNet: Join succeeded: Alice")
    assert match.category == "join" and match.name == "Alice"


def test_log_patterns_file_rejects_unknown_category(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"oops": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        LogPatterns.load(path)


def test_parse_docker_time():
    dt = parse_docker_time("2026-10-02T06:00:03.123456789Z")
    assert dt.tzinfo == timezone.utc and dt.microsecond == 123456
    assert parse_docker_time("0001-01-01T00:00:00Z") is None
    assert parse_docker_time("2026-10-02T06:00:03+02:00").utcoffset().total_seconds() == 7200


def test_cpu_and_memory():
    stats = {
        "cpu_stats": {"cpu_usage": {"total_usage": 3_000}, "system_cpu_usage": 20_000, "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 1_000}, "system_cpu_usage": 10_000},
        "memory_stats": {"usage": 1_000, "limit": 4_000, "stats": {"inactive_file": 200}},
    }
    assert compute_cpu_percent(stats) == pytest.approx(80.0)
    assert compute_memory(stats) == (800, 4_000)
    assert compute_cpu_percent({}) is None


def test_read_buildid(tmp_path):
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    (steamapps / "appmanifest_2089300.acf").write_text('"AppState"\n{\n\t"buildid"\t\t"16843211"\n}\n')
    assert read_buildid(tmp_path) == "16843211"
    assert read_buildid(tmp_path / "missing") is None


def test_config_defaults_and_minimum_stop_timeout():
    cfg = Config.from_env({**BASE_ENV, "STOP_TIMEOUT": "10"})
    assert cfg.stop_timeout == MIN_STOP_TIMEOUT
    assert str(cfg.prospects_dir).replace("\\", "/").endswith(
        "icarus/data/Saved/PlayerData/DedicatedServer/Prospects"
    )
    assert str(cfg.backup_dir).replace("\\", "/") == "/home/lucky/icarus/backups"
    assert cfg.ntfy_topic == "icarus" and not cfg.ntfy_enabled
    assert cfg.clean_exit_codes == frozenset({0})


def test_config_errors():
    with pytest.raises(ConfigError):
        Config.from_env({k: v for k, v in BASE_ENV.items() if k != "DISCORD_TOKEN"})
    with pytest.raises(ConfigError):
        Config.from_env({**BASE_ENV, "GUILD_ID": "abc"})
    with pytest.raises(ConfigError):
        Config.from_env({**BASE_ENV, "SCHEDULED_RESTART_TIME": "25:00"})


def test_state_persistence(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    assert state.scheduled_restart_enabled and state.scheduled_restart_time == "06:00"
    assert state.toggle_alert("crash") is False
    state.set_panel_message("panel", 10, 20)
    reloaded = State(path)
    assert reloaded.alert_enabled("crash") is False
    assert reloaded.panel_message("panel") == (10, 20)
    assert reloaded.alert_enabled("ntfy") is True


def test_pending_tokens_are_per_user_and_expire():
    now = [0.0]
    pending = Pending(clock=lambda: now[0])
    token = pending.put(42, prospect="Olympus")
    assert len(token) <= 20
    assert pending.get(token, 7) is None
    assert pending.pop(token, 42) == {"prospect": "Olympus"}
    assert pending.pop(token, 42) is None
    token = pending.put(42, x=1)
    now[0] += 901
    assert pending.get(token, 42) is None
