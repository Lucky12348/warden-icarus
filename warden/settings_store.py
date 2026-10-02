"""Réglages du serveur Icarus stockés dans /home/lucky/icarus/.env.

Le docker-compose.yml d'Icarus référence chaque réglage par `${VAR}` : le bot ne réécrit donc
que le .env (fichier simple), en préservant commentaires, ordre et clés inconnues, après une
copie `.env.bak`. Un réglage écrit en dur dans le compose est affiché mais pas modifiable.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


class SettingError(Exception):
    pass


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    kind: str  # "str", "secret", "int" ou "bool"
    help: str
    min: int | None = None
    max: int | None = None
    max_length: int = 64


SETTINGS: tuple[SettingDef, ...] = (
    SettingDef("SERVERNAME", "Nom du serveur", "str", "Nom affiché dans le navigateur de serveurs."),
    SettingDef("JOIN_PASSWORD", "Mot de passe joueurs", "secret", "Mot de passe pour rejoindre (vide = public)."),
    SettingDef("ADMIN_PASSWORD", "Mot de passe admin", "secret", "Mot de passe admin en jeu."),
    SettingDef("MAX_PLAYERS", "Joueurs max", "int", "Entre 1 et 8.", 1, 8),
    SettingDef(
        "SHUTDOWN_NOT_JOINED_FOR", "Arrêt si personne n'a rejoint (s)", "int",
        "Secondes avant arrêt si personne ne rejoint après le démarrage (-1 = jamais).", -1, 86400,
    ),
    SettingDef(
        "SHUTDOWN_EMPTY_FOR", "Arrêt si serveur vide (s)", "int",
        "Secondes avant arrêt quand le serveur se vide (-1 = jamais).", -1, 86400,
    ),
    SettingDef("ALLOW_NON_ADMINS_LAUNCH", "Non-admins peuvent lancer", "bool", "Les non-admins peuvent lancer une partie."),
    SettingDef("ALLOW_NON_ADMINS_DELETE", "Non-admins peuvent supprimer", "bool", "Les non-admins peuvent supprimer une partie."),
    SettingDef("RESUME_PROSPECT", "Reprendre la dernière partie", "bool", "Relance la dernière partie au démarrage."),
    SettingDef("SAVEGAMEONEXIT", "Sauvegarde à l'arrêt", "bool", "Sauvegarde la partie quand le serveur s'arrête."),
    SettingDef("GAMESAVEFREQUENCY", "Fréquence de sauvegarde (min)", "int", "Minutes entre deux sauvegardes.", 1, 1440),
    SettingDef("FIBERFOLIAGERESPAWN", "Repousse des fibres", "bool", "Repousse de la végétation à fibres."),
    SettingDef("LARGESTONERESPAWN", "Repousse des gros rochers", "bool", "Repousse des gros rochers."),
)
SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}

_TRUE = {"true", "1", "oui", "yes", "on", "vrai"}
_FALSE = {"false", "0", "non", "no", "off", "faux"}
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_SAFE_UNQUOTED = re.compile(r"^[A-Za-z0-9_.,:@+/=-]*$")
SECRET_MASK = "••••••"


# --------------------------------------------------------------------------- .env : lecture
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
    # Non quoté : un commentaire en ligne commence par « # » précédé d'un espace.
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


# --------------------------------------------------------------------------- .env : écriture
def format_value(value: str) -> str:
    """Formate une valeur pour un .env lu par docker compose, sans interpolation parasite."""
    if any(c in value for c in "\n\r\x00"):
        raise SettingError("La valeur ne peut pas contenir de retour à la ligne.")
    if _SAFE_UNQUOTED.match(value):
        return value
    if "'" not in value:
        return f"'{value}'"  # quotes simples : valeur littérale, pas d'interpolation de $
    if not any(c in value for c in '"$\\`'):
        return f'"{value}"'
    raise SettingError("La valeur ne peut pas contenir à la fois une apostrophe et l'un des caractères \" $ \\ `.")


def update_env_text(text: str, changes: Mapping[str, str]) -> str:
    """Remplace les clés existantes sur place et ajoute les nouvelles à la fin."""
    written: set[str] = set()
    out = []
    for line in text.splitlines():
        m = _LINE_RE.match(line) if not line.lstrip().startswith("#") else None
        key = m.group(1) if m else None
        if key in changes:
            if key not in written:  # un éventuel doublon plus bas est supprimé
                out.append(f"{key}={format_value(changes[key])}")
                written.add(key)
        else:
            out.append(line)
    for key, value in changes.items():
        if key not in written:
            out.append(f"{key}={format_value(value)}")
    return "\n".join(out) + "\n"


def write_env(path: Path, changes: Mapping[str, str]) -> Path | None:
    """Applique `changes` au .env : copie `.env.bak`, puis écriture atomique. Retourne le .bak."""
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


# --------------------------------------------------------------------------- validation / affichage
def normalize_bool(raw: str) -> str:
    low = raw.strip().lower()
    if low in _TRUE:
        return "True"
    if low in _FALSE:
        return "False"
    raise SettingError("Valeur attendue : True ou False (oui/non acceptés).")


def validate(defn: SettingDef, raw: str) -> str:
    """Valide une saisie et retourne la valeur normalisée à écrire."""
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
    format_value(value)  # vérifie qu'on saura l'écrire dans le .env
    return value


def display_value(defn: SettingDef, value: str | None) -> str:
    """Valeur affichable sur Discord : les secrets ne sont jamais montrés."""
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


# --------------------------------------------------------------------------- compose / conteneur
def compose_references(compose_text: str) -> set[str]:
    """Variables interpolées dans le compose (`${VAR}`, `${VAR:-x}`, `$VAR`)."""
    refs = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", compose_text))
    refs |= set(re.findall(r"(?<!\$)\$([A-Za-z_][A-Za-z0-9_]*)", compose_text))
    return refs


def editable_keys(compose_text: str, keys: Iterable[str] | None = None) -> set[str]:
    refs = compose_references(compose_text)
    return {k for k in (keys or SETTINGS_BY_KEY) if k in refs}


def container_env(env_list: Iterable[str] | None) -> dict[str, str]:
    out = {}
    for item in env_list or []:
        key, sep, value = item.partition("=")
        if sep:
            out[key] = value
    return out


def pending_keys(env_values: Mapping[str, str], running_env: Mapping[str, str], keys: Iterable[str]) -> set[str]:
    """Réglages dont la valeur du .env diffère de celle du conteneur (recréation nécessaire)."""
    return {k for k in keys if k in env_values and env_values.get(k) != running_env.get(k)}
