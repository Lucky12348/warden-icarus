"""Bot configuration, read from environment variables (the bot's .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

# The game needs time to save on shutdown (SaveGameOnExit): never less than 60 s.
# 90 s by default, like supervisord's stopwaitsecs in the mornedhels image.
DEFAULT_STOP_TIMEOUT = 90
MIN_STOP_TIMEOUT = 60


class ConfigError(Exception):
    pass


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"Variable obligatoire manquante : {key}")
    return value


def _int(env: Mapping[str, str], key: str, default: int | None = None) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"Variable obligatoire manquante : {key}")
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} doit être un entier (reçu : {raw!r})") from e


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "oui", "on")


def _int_set(env: Mapping[str, str], key: str, default: str) -> frozenset[int]:
    raw = env.get(key, "").strip() or default
    try:
        return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)
    except ValueError as e:
        raise ConfigError(f"{key} doit être une liste d'entiers séparés par des virgules") from e


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int
    admin_role_id: int
    channel_panel_id: int
    channel_backups_id: int
    channel_settings_id: int
    channel_logs_id: int

    container_name: str
    icarus_dir: Path
    state_dir: Path
    backup_dir: Path
    backup_retention: int

    a2s_host: str
    a2s_port: int
    panel_refresh_seconds: int
    player_poll_seconds: int
    stop_timeout: int
    recreate_timeout: int
    timezone: ZoneInfo

    ntfy_url: str
    ntfy_token: str
    ntfy_topic: str

    log_patterns_file: Path | None
    log_dedupe_seconds: int
    log_max_per_minute: int
    error_logs_to_discord: bool  # also publish the "error" category (off by default)
    log_spam_summary: bool  # hourly summary of lines dropped by the anti-spam (off by default)
    player_detection: str  # "a2s", "logs" or "both"

    clean_exit_codes: frozenset[int]
    scheduled_restart_default_time: str
    scheduled_restart_if_a2s_down: bool

    # --- paths derived from ICARUS_DIR ---
    @property
    def icarus_env_file(self) -> Path:
        return self.icarus_dir / ".env"

    @property
    def icarus_compose_file(self) -> Path:
        return self.icarus_dir / "docker-compose.yml"

    @property
    def data_dir(self) -> Path:
        return self.icarus_dir / "data"

    @property
    def game_dir(self) -> Path:
        return self.icarus_dir / "game"

    @property
    def server_settings_file(self) -> Path:
        """Game settings the image does not expose as environment variables."""
        return self.data_dir / "Saved" / "Config" / "WindowsServer" / "ServerSettings.ini"

    @property
    def saves_root(self) -> Path:
        """Root archived by manual backups (saves + player data)."""
        return self.data_dir / "Saved" / "PlayerData"

    # Path of the saves relative to saves_root (also used inside archives).
    prospects_rel = "DedicatedServer/Prospects"

    @property
    def prospects_dir(self) -> Path:
        return self.saves_root / self.prospects_rel

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.ntfy_url and self.ntfy_topic)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        icarus_dir = Path(env.get("ICARUS_DIR", "/home/lucky/icarus").strip() or "/home/lucky/icarus")
        backup_dir_raw = env.get("BACKUP_DIR", "").strip()
        patterns_raw = env.get("LOG_PATTERNS_FILE", "").strip()
        detection = env.get("PLAYER_DETECTION", "a2s").strip().lower() or "a2s"
        if detection not in ("a2s", "logs", "both"):
            raise ConfigError("PLAYER_DETECTION doit valoir a2s, logs ou both")
        tz_name = env.get("TZ", "Europe/Paris").strip() or "Europe/Paris"
        try:
            tz = ZoneInfo(tz_name)
        except Exception as e:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError...
            raise ConfigError(f"Fuseau horaire inconnu : {tz_name}") from e

        restart_time = env.get("SCHEDULED_RESTART_TIME", "06:00").strip() or "06:00"
        from .scheduler import parse_hhmm  # local import: avoids a cycle at load time

        try:
            parse_hhmm(restart_time)
        except ValueError as e:
            raise ConfigError(f"SCHEDULED_RESTART_TIME invalide : {restart_time!r}") from e

        return cls(
            discord_token=_required(env, "DISCORD_TOKEN"),
            guild_id=_int(env, "GUILD_ID"),
            admin_role_id=_int(env, "ADMIN_ROLE_ID"),
            channel_panel_id=_int(env, "CHANNEL_PANEL_ID"),
            channel_backups_id=_int(env, "CHANNEL_BACKUPS_ID"),
            channel_settings_id=_int(env, "CHANNEL_SETTINGS_ID"),
            channel_logs_id=_int(env, "CHANNEL_LOGS_ID"),
            container_name=env.get("ICARUS_CONTAINER", "icarus").strip() or "icarus",
            icarus_dir=icarus_dir,
            state_dir=Path(env.get("STATE_DIR", "/app/state").strip() or "/app/state"),
            backup_dir=Path(backup_dir_raw) if backup_dir_raw else icarus_dir / "backups",
            backup_retention=max(1, _int(env, "BACKUP_RETENTION", 14)),
            a2s_host=env.get("A2S_HOST", "127.0.0.1").strip() or "127.0.0.1",
            a2s_port=_int(env, "A2S_PORT", 32784),
            panel_refresh_seconds=max(15, _int(env, "PANEL_REFRESH_SECONDS", 60)),
            player_poll_seconds=max(5, _int(env, "PLAYER_POLL_SECONDS", 20)),
            stop_timeout=max(MIN_STOP_TIMEOUT, _int(env, "STOP_TIMEOUT", DEFAULT_STOP_TIMEOUT)),
            recreate_timeout=max(60, _int(env, "RECREATE_TIMEOUT", 600)),
            timezone=tz,
            ntfy_url=env.get("NTFY_URL", "").strip().rstrip("/"),
            ntfy_token=env.get("NTFY_TOKEN", "").strip(),
            ntfy_topic=env.get("NTFY_TOPIC", "icarus").strip(),
            log_patterns_file=Path(patterns_raw) if patterns_raw else None,
            log_dedupe_seconds=max(0, _int(env, "LOG_DEDUPE_SECONDS", 300)),
            log_max_per_minute=max(1, _int(env, "LOG_MAX_PER_MINUTE", 10)),
            error_logs_to_discord=_bool(env, "ERROR_LOGS_TO_DISCORD", False),
            log_spam_summary=_bool(env, "LOG_SPAM_SUMMARY", False),
            player_detection=detection,
            clean_exit_codes=_int_set(env, "CLEAN_EXIT_CODES", "0"),
            scheduled_restart_default_time=restart_time,
            scheduled_restart_if_a2s_down=_bool(env, "SCHEDULED_RESTART_IF_A2S_DOWN", False),
        )
