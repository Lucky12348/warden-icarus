"""Icarus server settings (mornedhels/icarus-server image).

Two locations:
- `/home/lucky/icarus/.env` ("env" storage): the Icarus docker-compose.yml references each
  setting as `${VAR}` and translates it to the image's variable (SERVERNAME -> SERVER_NAME...).
  The bot only rewrites the .env, keeping comments, order and unknown keys, after a
  `.env.bak` copy. A setting hard-coded in the compose file is shown but cannot be changed.
- `data/Saved/Config/WindowsServer/ServerSettings.ini` ("ini" storage): settings the image
  does not expose as environment variables. The bot changes the key in the
  [/Script/Icarus.DedicatedServerSettings] section after a `.bak` copy; the rest of the file
  is kept.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from . import safefs


class SettingError(Exception):
    pass


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    kind: str  # "str", "secret", "int" or "bool"
    help: str
    min: int | None = None
    max: int | None = None
    max_length: int = 64
    storage: str = "env"  # "env" (Icarus .env) or "ini" (ServerSettings.ini)
    # env storage: name of the variable inside the container (translated by the compose file).
    container_key: str | None = None
    # ServerSettings.ini key: the setting itself (ini), or the key the image copies it to (env).
    ini_key: str | None = None


SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        "SERVERNAME", "Nom du serveur", "str", "Nom affiché dans le navigateur de serveurs.",
        container_key="SERVER_NAME", ini_key="SessionName",
    ),
    SettingDef(
        "JOIN_PASSWORD", "Mot de passe joueurs", "secret", "Mot de passe pour rejoindre (vide = public).",
        container_key="SERVER_PASSWORD", ini_key="JoinPassword",
    ),
    SettingDef(
        "ADMIN_PASSWORD", "Mot de passe admin", "secret", "Mot de passe admin en jeu.",
        container_key="SERVER_ADMIN_PASSWORD", ini_key="AdminPassword",
    ),
    SettingDef(
        "MAX_PLAYERS", "Joueurs max", "int", "Entre 1 et 8.", 1, 8,
        container_key="SERVER_MAX_PLAYERS", ini_key="MaxPlayers",
    ),
    SettingDef(
        "SHUTDOWN_NOT_JOINED_FOR", "Arrêt si personne n'a rejoint (s)", "int",
        "Secondes avant arrêt si personne ne rejoint après le démarrage (-1 = jamais).", -1, 86400,
        container_key="SERVER_SHUTDOWN_IF_NOT_JOINED", ini_key="ShutdownIfNotJoinedFor",
    ),
    SettingDef(
        "SHUTDOWN_EMPTY_FOR", "Arrêt si serveur vide (s)", "int",
        "Secondes avant arrêt quand le serveur se vide (-1 = jamais).", -1, 86400,
        container_key="SERVER_SHUTDOWN_IF_EMPTY", ini_key="ShutdownIfEmptyFor",
    ),
    SettingDef(
        "ALLOW_NON_ADMINS_LAUNCH", "Non-admins peuvent lancer", "bool", "Les non-admins peuvent lancer une partie.",
        container_key="SERVER_ALLOW_NON_ADMINS_LAUNCH", ini_key="AllowNonAdminsToLaunchProspects",
    ),
    SettingDef(
        "ALLOW_NON_ADMINS_DELETE", "Non-admins peuvent supprimer", "bool", "Les non-admins peuvent supprimer une partie.",
        container_key="SERVER_ALLOW_NON_ADMINS_DELETE", ini_key="AllowNonAdminsToDeleteProspects",
    ),
    SettingDef(
        "RESUME_PROSPECT", "Reprendre la dernière partie", "bool", "Relance la dernière partie au démarrage.",
        container_key="SERVER_RESUME_PROSPECT", ini_key="ResumeProspect",
    ),
    SettingDef(
        "SAVEGAMEONEXIT", "Sauvegarde à l'arrêt", "bool", "Sauvegarde la partie quand le serveur s'arrête.",
        storage="ini", ini_key="SaveGameOnExit",
    ),
    SettingDef(
        "GAMESAVEFREQUENCY", "Fréquence de sauvegarde (min)", "int", "Minutes entre deux sauvegardes.", 1, 1440,
        storage="ini", ini_key="GameSaveFrequency",
    ),
    SettingDef(
        "FIBERFOLIAGERESPAWN", "Repousse des fibres", "bool", "Repousse de la végétation à fibres.",
        storage="ini", ini_key="FiberFoliageRespawn",
    ),
    SettingDef(
        "LARGESTONERESPAWN", "Repousse des gros rochers", "bool", "Repousse des gros rochers.",
        storage="ini", ini_key="LargeStonesRespawn",
    ),
)
SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}

_TRUE = {"true", "1", "oui", "yes", "on", "vrai"}
_FALSE = {"false", "0", "non", "no", "off", "faux"}
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_SAFE_UNQUOTED = re.compile(r"^[A-Za-z0-9_.,:@+/=-]*$")
SECRET_MASK = "••••••"


# --------------------------------------------------------------------------- .env: reading
def _parse_value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("'"):
        end = raw.find("'", 1)
        return raw[1:end] if end != -1 else raw[1:]
    if raw.startswith('"'):
        out, i = [], 1
        while i < len(raw):
            c = raw[i]
            if c == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                i += 2
                continue
            if c == '"':
                break
            out.append(c)
            i += 1
        return "".join(out)
    # Unquoted: an inline comment starts with "#" preceded by a space.
    m = re.search(r"\s#", raw)
    return (raw[: m.start()] if m else raw).strip()


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if m:
            values[m.group(1)] = _parse_value(m.group(2))
    return values


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_env(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- .env: writing
def format_value(value: str) -> str:
    """Format a value for a .env read by docker compose, without unwanted interpolation."""
    if any(c in value for c in "\n\r\x00"):
        raise SettingError("La valeur ne peut pas contenir de retour à la ligne.")
    if _SAFE_UNQUOTED.match(value):
        return value
    if "'" not in value:
        return f"'{value}'"  # single quotes: literal value, no $ interpolation
    if not any(c in value for c in '"$\\`'):
        return f'"{value}"'
    raise SettingError("La valeur ne peut pas contenir à la fois une apostrophe et l'un des caractères \" $ \\ `.")


def update_env_text(text: str, changes: Mapping[str, str]) -> str:
    """Replace existing keys in place and append new ones at the end."""
    written: set[str] = set()
    out = []
    for line in text.splitlines():
        m = _LINE_RE.match(line) if not line.lstrip().startswith("#") else None
        key = m.group(1) if m else None
        if key in changes:
            if key not in written:  # a possible duplicate further down is removed
                out.append(f"{key}={format_value(changes[key])}")
                written.add(key)
        else:
            out.append(line)
    for key, value in changes.items():
        if key not in written:
            out.append(f"{key}={format_value(value)}")
    return "\n".join(out) + "\n"


def write_env(path: Path, changes: Mapping[str, str]) -> Path | None:
    """Apply `changes` to the .env: `.env.bak` copy, then atomic write. Return the .bak."""
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text = update_env_text(old_text, changes)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        if path.exists():
            shutil.copymode(path, tmp)
        else:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return backup


def restore_env_backup(path: Path) -> bool:
    backup = path.with_name(path.name + ".bak")
    if not backup.exists():
        return False
    shutil.copy2(backup, path)
    return True


# --------------------------------------------------------------------------- ServerSettings.ini
INI_SECTION = "/Script/Icarus.DedicatedServerSettings"
_SECTION_RE = re.compile(r"^\s*\[(.*)\]\s*$")
_INI_KEY_RE = re.compile(r"^\s*([^=;#\s][^=]*?)\s*=(.*)$")


def _decode_ini(raw: bytes) -> tuple[str, str]:
    """Text and encoding of an .ini (Unreal writes UTF-16 with a BOM if needed, otherwise ANSI/UTF-8)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def parse_ini_section(text: str, section: str = INI_SECTION) -> dict[str, str]:
    """Keys of a section, with their name as written in the file."""
    values: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        sec = _SECTION_RE.match(line)
        if sec:
            current = sec.group(1).strip()
            continue
        m = _INI_KEY_RE.match(line) if current == section else None
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values


def read_ini(path: Path, section: str = INI_SECTION) -> dict[str, str]:
    """Read the section; the file is in data/ (writable by the game): no link followed."""
    if not os.path.lexists(path):
        return {}
    return parse_ini_section(_decode_ini(safefs.read_bytes(path, safefs.SMALL_FILE)[0])[0], section)


def ini_lookup(values: Mapping[str, str], key: str) -> str | None:
    """Value of an .ini key, case-insensitive (like Unreal)."""
    low = key.lower()
    return next((v for k, v in values.items() if k.lower() == low), None)


def update_ini_text(text: str, changes: Mapping[str, str], section: str = INI_SECTION) -> str:
    """Replace the section's keys in place and append missing ones at the end of the section (or
    create the section). The rest of the file (other sections, comments) is kept as is."""
    for key, value in changes.items():
        if any(c in value for c in "\n\r\x00"):
            raise SettingError(f"{key} : retour à la ligne interdit.")
    newline = "\r\n" if "\r\n" in text else "\n"
    wanted = {k.lower(): (k, v) for k, v in changes.items()}
    done: set[str] = set()
    out: list[str] = []
    in_target = False
    insert_at: int | None = None  # right after the last non-empty line of the section
    for line in text.splitlines():
        sec = _SECTION_RE.match(line)
        if sec:
            in_target = sec.group(1).strip() == section
            out.append(line)
            if in_target:
                insert_at = len(out)
            continue
        m = _INI_KEY_RE.match(line) if in_target else None
        if m and m.group(1).lower() in wanted:
            # Every occurrence: Unreal keeps the last one of a duplicated key.
            _, value = wanted[m.group(1).lower()]
            line = f"{m.group(1)}={value}"
            done.add(m.group(1).lower())
        out.append(line)
        if in_target and line.strip():
            insert_at = len(out)
    added = [f"{k}={v}" for low, (k, v) in wanted.items() if low not in done]
    if added and insert_at is None:
        if out and out[-1].strip():
            out.append("")
        out += [f"[{section}]", *added]
    elif added:
        out[insert_at:insert_at] = added
    return newline.join(out) + newline


def write_ini(path: Path, changes: Mapping[str, str], section: str = INI_SECTION, root: Path | None = None) -> Path:
    """Apply `changes` to ServerSettings.ini: `.bak` copy, then atomic write with the same
    encoding and line endings as the original. Return the path of the .bak.

    The folder is writable by the game: neither the original, nor the .bak, nor the temporary
    file is handled through a symlink (see safefs). `root`: root the real folder must not
    leave.
    """
    if root is not None:
        safefs.ensure_inside(path, root)
    if not os.path.lexists(path):
        raise SettingError(f"{path.name} introuvable : démarre le serveur une fois pour que l'image le crée.")
    raw, mode = safefs.read_bytes(path, safefs.SMALL_FILE)
    text, encoding = _decode_ini(raw)
    new_text = update_ini_text(text, changes, section)
    backup = path.with_name(path.name + ".bak")
    safefs.atomic_write(backup, raw, mode)
    safefs.atomic_write(path, new_text.encode(encoding), mode)
    return backup


# --------------------------------------------------------------------------- validation / display
def normalize_bool(raw: str) -> str:
    low = raw.strip().lower()
    if low in _TRUE:
        return "True"
    if low in _FALSE:
        return "False"
    raise SettingError("Valeur attendue : True ou False (oui/non acceptés).")


def validate(defn: SettingDef, raw: str) -> str:
    """Validate an input and return the normalised value to write."""
    value = raw.strip() if defn.kind != "secret" else raw
    if defn.kind == "bool":
        return normalize_bool(value)
    if defn.kind == "int":
        try:
            n = int(value)
        except ValueError:
            raise SettingError(f"{defn.label} : nombre entier attendu.") from None
        if (defn.min is not None and n < defn.min) or (defn.max is not None and n > defn.max):
            raise SettingError(f"{defn.label} : doit être entre {defn.min} et {defn.max}.")
        return str(n)
    if any(c in value for c in "\n\r\x00"):
        raise SettingError("Retour à la ligne interdit.")
    if len(value) > defn.max_length:
        raise SettingError(f"{defn.label} : {defn.max_length} caractères maximum.")
    if defn.kind == "str" and not value:
        raise SettingError(f"{defn.label} ne peut pas être vide.")
    if defn.storage == "env":
        format_value(value)  # check that it can be written to the .env
    return value


def display_value(defn: SettingDef, value: str | None) -> str:
    """Value that can be shown on Discord: secrets are never shown."""
    if defn.kind == "secret":
        return f"{SECRET_MASK} (défini)" if value else "(aucun)"
    if value is None or value == "":
        return "(non défini)"
    if defn.kind == "bool":
        try:
            return "✅ Oui" if normalize_bool(value) == "True" else "❌ Non"
        except SettingError:
            return value
    return value


# --------------------------------------------------------------------------- compose / container
def compose_references(compose_text: str) -> set[str]:
    """Variables interpolated in the compose file (`${VAR}`, `${VAR:-x}`, `$VAR`)."""
    refs = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", compose_text))
    refs |= set(re.findall(r"(?<!\$)\$([A-Za-z_][A-Za-z0-9_]*)", compose_text))
    return refs


def editable_keys(compose_text: str, keys: Iterable[str] | None = None) -> set[str]:
    """Editable settings: the .env ones referenced by the compose file, and all .ini ones."""
    refs = compose_references(compose_text)
    out = set()
    for k in keys or SETTINGS_BY_KEY:
        defn = SETTINGS_BY_KEY.get(k)
        if k in refs or (defn is not None and defn.storage == "ini"):
            out.add(k)
    return out


def current_values(env_values: Mapping[str, str], ini_values: Mapping[str, str]) -> dict[str, str]:
    """Current value of each setting, read from its storage (.env or ServerSettings.ini)."""
    out = {}
    for defn in SETTINGS:
        if defn.storage == "ini":
            value = ini_lookup(ini_values, defn.ini_key or defn.key)
        else:
            value = env_values.get(defn.key)
        if value is not None:
            out[defn.key] = value
    return out


def container_env(env_list: Iterable[str] | None) -> dict[str, str]:
    out = {}
    for item in env_list or []:
        key, sep, value = item.partition("=")
        if sep:
            out[key] = value
    return out


def pending_keys(env_values: Mapping[str, str], running_env: Mapping[str, str], keys: Iterable[str]) -> set[str]:
    """.env settings whose value differs from the container's (recreation needed).

    The compose file translates the names (SERVERNAME -> SERVER_NAME): the comparison uses
    `container_key`. An .ini setting is never "pending": the bot writes it with the server stopped.
    """
    out = set()
    for k in keys:
        defn = SETTINGS_BY_KEY.get(k)
        if k not in env_values or (defn is not None and defn.storage != "env"):
            continue
        running_key = (defn.container_key if defn else None) or k
        if env_values.get(k) != running_env.get(running_key):
            out.add(k)
    return out
