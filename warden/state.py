"""État persistant du bot (state/state.json) : interrupteurs, IDs des panneaux, planification."""

from __future__ import annotations

import json
import logging
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ALERT_KEYS = ("discord", "crash", "unexpected_restart", "update", "ntfy")
ALERT_LABELS = {
    "discord": "Alertes Discord",
    "crash": "Crash",
    "unexpected_restart": "Redémarrage inattendu",
    "update": "Mise à jour",
    "ntfy": "ntfy",
}


def default_state(scheduled_time: str = "06:00") -> dict[str, Any]:
    return {
        "alerts": {key: True for key in ALERT_KEYS},
        # Équivalent de restartcrontab=1 "0 6 * * *" de l'ancien bot.
        "scheduled_restart": {"enabled": True, "time": scheduled_time},
        "panel_messages": {},  # "panel" / "backups" / "settings" -> [channel_id, message_id]
        "last_buildid": None,
    }


def _merge(defaults: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(defaults)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class State:
    """Petit magasin JSON, écrit de façon atomique (fichier temporaire + rename)."""

    def __init__(self, path: Path, scheduled_time: str = "06:00") -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data = default_state(scheduled_time)
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = _merge(self._data, loaded)
            except (OSError, ValueError) as e:
                log.warning("state.json illisible (%s), valeurs par défaut utilisées", e)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    # --- alertes ---
    def alert_enabled(self, key: str) -> bool:
        return bool(self._data["alerts"].get(key, True))

    def toggle_alert(self, key: str) -> bool:
        if key not in ALERT_KEYS:
            raise KeyError(key)
        self._data["alerts"][key] = not self.alert_enabled(key)
        self.save()
        return self._data["alerts"][key]

    # --- redémarrage planifié ---
    @property
    def scheduled_restart_enabled(self) -> bool:
        return bool(self._data["scheduled_restart"]["enabled"])

    @property
    def scheduled_restart_time(self) -> str:
        return str(self._data["scheduled_restart"]["time"])

    def toggle_scheduled_restart(self) -> bool:
        self._data["scheduled_restart"]["enabled"] = not self.scheduled_restart_enabled
        self.save()
        return self.scheduled_restart_enabled

    def set_scheduled_restart_time(self, hhmm: str) -> None:
        self._data["scheduled_restart"]["time"] = hhmm
        self.save()

    # --- messages des panneaux ---
    def panel_message(self, name: str) -> tuple[int, int] | None:
        ref = self._data["panel_messages"].get(name)
        if not ref:
            return None
        return int(ref[0]), int(ref[1])

    def set_panel_message(self, name: str, channel_id: int, message_id: int) -> None:
        self._data["panel_messages"][name] = [channel_id, message_id]
        self.save()

    # --- build du jeu (détection de mise à jour) ---
    @property
    def last_buildid(self) -> str | None:
        return self._data.get("last_buildid")

    def set_last_buildid(self, buildid: str) -> None:
        self._data["last_buildid"] = buildid
        self.save()
