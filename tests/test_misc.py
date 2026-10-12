"""Pure helper functions: logs, Docker (computations), configuration, state, tokens."""

import json
from datetime import timezone

import pytest

from warden.config import MIN_STOP_TIMEOUT, Config, ConfigError
from warden.docker_ctl import compute_cpu, compute_memory, parse_docker_time, read_buildid
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


def cpu_stats(container_delta, system_delta, online_cpus=None, percpu=None):
    usage = {"total_usage": 1_000 + container_delta}
    if percpu is not None:
        usage["percpu_usage"] = percpu
    cpu = {"cpu_usage": usage, "system_cpu_usage": 10_000 + system_delta}
    if online_cpus is not None:
        cpu["online_cpus"] = online_cpus
    return {"cpu_stats": cpu, "precpu_stats": {"cpu_usage": {"total_usage": 1_000}, "system_cpu_usage": 10_000}}


def test_cpu_is_a_percentage_of_the_whole_vm():
    # docker stats would show 15 % (of one core) on 4 cores: Proxmox shows 3.75 % of the VM.
    percent, cores = compute_cpu(cpu_stats(375, 10_000, online_cpus=4))
    assert percent == pytest.approx(3.75) and cores == 4
    # Without online_cpus: the number of cores comes from percpu_usage.
    assert compute_cpu(cpu_stats(2_000, 10_000, percpu=[1, 2])) == (pytest.approx(20.0), 2)
    # Never above 100 % of the VM, idle or invalid counters give 0 %, missing data gives None.
    assert compute_cpu(cpu_stats(20_000, 10_000, online_cpus=4))[0] == 100.0
    assert compute_cpu(cpu_stats(0, 0, online_cpus=4)) == (0.0, 4)
    assert compute_cpu({}) == (None, None)


def test_memory_excludes_cache():
    stats = {"memory_stats": {"usage": 1_000, "limit": 4_000, "stats": {"inactive_file": 200}}}
    assert compute_memory(stats) == (800, 4_000)
    cgroup_v1 = {"memory_stats": {"usage": 1_000, "limit": 4_000, "stats": {"total_inactive_file": 300}}}
    assert compute_memory(cgroup_v1) == (700, 4_000)
    assert compute_memory({}) == (None, None)


def test_resources_line_format():
    from warden.docker_ctl import Stats
    from warden.panels import resources_line

    gib = 1024**3
    line = resources_line(Stats(3.75, int(1.2 * gib), int(7.7 * gib), 4))
    assert line == "🖥️ CPU **4 %** (4 cœurs) · RAM Icarus **1,2 Go / 7,7 Go**"
    assert resources_line(Stats(50.0, None, None, 1)) == "🖥️ CPU **50 %** (1 cœur) · RAM Icarus **?**"
    assert resources_line(Stats(None, None, None)) == "🖥️ CPU **—** · RAM Icarus **?**"


def test_read_buildid(tmp_path):
    # mornedhels image: game in /opt/icarus/server -> game/server on the host.
    steamapps = tmp_path / "server" / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "appmanifest_2089300.acf").write_text('"AppState"\n{\n\t"buildid"\t\t"16843211"\n}\n')
    (tmp_path / "current_version").write_text("16000000\n")
    assert read_buildid(tmp_path) == "16843211"
    assert read_buildid(tmp_path / "missing") is None


def test_read_buildid_falls_back_to_current_version(tmp_path):
    (tmp_path / "current_version").write_text("16843211\n")
    assert read_buildid(tmp_path) == "16843211"
    (tmp_path / "current_version").write_text("null\n")
    assert read_buildid(tmp_path) is None


def test_config_defaults_and_minimum_stop_timeout():
    assert Config.from_env(BASE_ENV).stop_timeout == 90  # supervisord's stopwaitsecs
    cfg = Config.from_env({**BASE_ENV, "STOP_TIMEOUT": "10"})
    assert cfg.stop_timeout == MIN_STOP_TIMEOUT
    assert str(cfg.server_settings_file).replace("\\", "/") == (
        "/home/lucky/icarus/data/Saved/Config/WindowsServer/ServerSettings.ini"
    )
    assert str(cfg.prospects_dir).replace("\\", "/").endswith(
        "icarus/data/Saved/PlayerData/DedicatedServer/Prospects"
    )
    assert str(cfg.backup_dir).replace("\\", "/") == "/home/lucky/icarus/backups"
    assert cfg.ntfy_topic == "icarus" and not cfg.ntfy_enabled
    assert cfg.clean_exit_codes == frozenset({0})
    assert not cfg.error_logs_to_discord and not cfg.log_spam_summary
    cfg = Config.from_env({**BASE_ENV, "ERROR_LOGS_TO_DISCORD": "true", "LOG_SPAM_SUMMARY": "true"})
    assert cfg.error_logs_to_discord and cfg.log_spam_summary


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
    # Off by default: the image already updates the game itself.
    assert not state.scheduled_restart_enabled and state.scheduled_restart_time == "06:00"
    assert state.toggle_alert("crash") is False
    state.set_panel_message("panel", 10, 20)
    reloaded = State(path)
    assert reloaded.alert_enabled("crash") is False
    assert reloaded.panel_message("panel") == (10, 20)
    assert reloaded.alert_enabled("ntfy") is True


def test_state_migration_disables_scheduled_restart_once(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"scheduled_restart": {"enabled": True, "time": "05:30"}, "last_buildid": "1"}))
    state = State(path)
    assert not state.scheduled_restart_enabled and state.scheduled_restart_time == "05:30"
    assert json.loads(path.read_text())["schema"] == 2  # migration saved
    assert state.toggle_scheduled_restart() is True
    assert State(path).scheduled_restart_enabled  # a choice made after the migration is kept


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


def test_settings_panel_warns_about_scheduled_restart_and_marks_ini_settings():
    from warden.panels import SCHEDULED_RESTART_WARNING, SettingsInfo, settings_panel

    info = SettingsInfo(
        alerts={}, ntfy_configured=False, scheduled_enabled=False, scheduled_time="06:00", tz_name="Europe/Paris",
        env_values={"SERVERNAME": "ZEHEFLAND", "SAVEGAMEONEXIT": "True"},
        editable={"SERVERNAME", "SAVEGAMEONEXIT"}, pending=set(), env_readable=True, ini_readable=False,
    )
    text = "\n".join(b for b in settings_panel(info).blocks if isinstance(b, str))
    assert SCHEDULED_RESTART_WARNING in text and "UPDATE_CRON" in text
    assert "**Sauvegarde à l'arrêt** : ✅ Oui 📄" in text
    assert "ServerSettings.ini` introuvable" in text


def test_state_with_unreadable_schema_is_kept(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema": "x", "panel_messages": {"panel": [1, 2]}}))
    state = State(path)
    assert state.panel_message("panel") == (1, 2)  # the whole state is not reset
