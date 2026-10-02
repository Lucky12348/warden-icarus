"""Sauvegardes des parties Icarus (prospects).

- Parties actives : `Prospects/<nom>.json`.
- Sauvegardes du jeu : `<nom>.json.backup_N` (créées par Icarus).
- Copies de sécurité de restauration : `<nom>.json.pre_restore_<AAAAMMJJ-HHMMSS>` (même format
  que l'ancien bot, pour que celles déjà présentes restent reconnues).
- Archives du bot : `backups/icarus-<AAAAMMJJ-HHMMSS>.tar.gz` contenant tout `Saved/PlayerData`
  (sans les .backup/.pre_restore), accompagnées d'un `.manifest.json` listant les parties.

Ce module ne dépend ni de Discord ni de Docker : la restauration suppose que l'appelant a
déjà arrêté le conteneur.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path, PurePosixPath

ARCHIVE_PREFIX = "icarus-"
ARCHIVE_SUFFIX = ".tar.gz"
MANIFEST_SUFFIX = ".manifest.json"
_ARCHIVE_RE = re.compile(r"^icarus-\d{8}-\d{6}(?:-\d+)?\.tar\.gz$")
_EXCLUDED_MARKERS = (".backup", ".pre_restore_", ".tmp")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class Prospect:
    name: str
    path: Path
    modified: datetime
    size: int


@dataclass(frozen=True)
class RestorePoint:
    source: str  # "game" (fichier à côté de la partie) ou "archive"
    ref: str  # nom du fichier .backup/.pre_restore, ou nom de l'archive
    kind: str  # "backup", "pre_restore" ou "archive"
    modified: datetime
    size: int

    @property
    def token(self) -> str:
        """Valeur courte pour un menu Discord (100 caractères max)."""
        return f"{'g' if self.source == 'game' else 'a'}:{self.ref}"


@dataclass(frozen=True)
class ArchiveResult:
    path: Path
    files: int
    size: int
    prospects: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class RestoreResult:
    target: Path
    pre_restore: Path | None


def _mtime(path: Path, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz)


def check_name(name: str) -> str:
    """Refuse tout ce qui pourrait sortir du dossier (valeur venant d'une interaction Discord)."""
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or name.startswith(".")
    ):
        raise BackupError(f"Nom invalide : {name!r}")
    return name


def is_active_save(filename: str) -> bool:
    return filename.endswith(".json") and ".json." not in filename and not filename.startswith(".")


def list_prospects(prospects_dir: Path, tz: tzinfo | None = None) -> list[Prospect]:
    if not prospects_dir.is_dir():
        return []
    out = []
    for path in prospects_dir.iterdir():
        if path.is_file() and is_active_save(path.name):
            st = path.stat()
            out.append(Prospect(path.name[: -len(".json")], path, datetime.fromtimestamp(st.st_mtime, tz), st.st_size))
    return sorted(out, key=lambda p: p.modified, reverse=True)


def list_archives(backup_dir: Path) -> list[Path]:
    """Archives du bot, de la plus récente à la plus ancienne (le nom est horodaté)."""
    if not backup_dir.is_dir():
        return []
    return sorted((p for p in backup_dir.iterdir() if p.is_file() and _ARCHIVE_RE.match(p.name)), reverse=True)


def _manifest_path(archive: Path) -> Path:
    return archive.with_name(archive.name[: -len(ARCHIVE_SUFFIX)] + MANIFEST_SUFFIX)


def archive_prospects(archive: Path, prospects_rel: str) -> list[str]:
    """Parties contenues dans une archive (manifeste si présent, sinon lecture de l'archive)."""
    manifest = _manifest_path(archive)
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return list(data.get("prospects", []))
        except (OSError, ValueError):
            pass
    prefix = prospects_rel.rstrip("/") + "/"
    names = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.startswith(prefix):
                rest = member.name[len(prefix):]
                if "/" not in rest and is_active_save(rest):
                    names.append(rest[: -len(".json")])
    return names


def list_restore_points(
    prospects_dir: Path,
    backup_dir: Path,
    prospect: str,
    prospects_rel: str,
    tz: tzinfo | None = None,
) -> list[RestorePoint]:
    check_name(prospect)
    points: list[RestorePoint] = []
    if prospects_dir.is_dir():
        for path in prospects_dir.iterdir():
            if not path.is_file():
                continue
            if path.name.startswith(f"{prospect}.json.backup"):
                kind = "backup"
            elif path.name.startswith(f"{prospect}.json.pre_restore_"):
                kind = "pre_restore"
            else:
                continue
            points.append(RestorePoint("game", path.name, kind, _mtime(path, tz), path.stat().st_size))
    for archive in list_archives(backup_dir):
        try:
            if prospect in archive_prospects(archive, prospects_rel):
                points.append(RestorePoint("archive", archive.name, "archive", _mtime(archive, tz), archive.stat().st_size))
        except (OSError, tarfile.TarError):
            continue  # archive corrompue : ignorée plutôt que de bloquer la liste
    return sorted(points, key=lambda p: p.modified, reverse=True)


def _read_valid_json(path: Path, attempts: int = 3, delay: float = 1.0) -> tuple[bytes, bool]:
    """Lit un fichier JSON ; réessaie s'il est en cours d'écriture par le jeu."""
    data = b""
    for i in range(attempts):
        data = path.read_bytes()
        try:
            json.loads(data)
            return data, True
        except ValueError:
            if i < attempts - 1:
                time.sleep(delay)
    return data, False


def _unique_archive_path(backup_dir: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"{ARCHIVE_PREFIX}{stamp}{ARCHIVE_SUFFIX}"
    n = 1
    while path.exists():
        path = backup_dir / f"{ARCHIVE_PREFIX}{stamp}-{n}{ARCHIVE_SUFFIX}"
        n += 1
    return path


def create_archive(
    saves_root: Path,
    backup_dir: Path,
    prospects_rel: str,
    now: datetime,
    json_retry_delay: float = 1.0,
) -> ArchiveResult:
    """Archive `saves_root` (Saved/PlayerData) en tar.gz horodaté, sans les sauvegardes du jeu.

    Possible serveur allumé : chaque .json est relu jusqu'à être un JSON valide, pour ne pas
    archiver un fichier en cours d'écriture.
    """
    if not saves_root.is_dir():
        raise BackupError(f"Dossier des sauvegardes introuvable : {saves_root}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = _unique_archive_path(backup_dir, now)
    tmp = final.with_name(final.name + ".tmp")
    prefix = prospects_rel.rstrip("/") + "/"
    files, prospects, warnings = 0, [], []
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for path in sorted(saves_root.rglob("*")):
                if not path.is_file() or any(m in path.name for m in _EXCLUDED_MARKERS):
                    continue
                arcname = path.relative_to(saves_root).as_posix()
                if path.suffix == ".json":
                    data, valid = _read_valid_json(path, delay=json_retry_delay)
                    if not valid:
                        warnings.append(f"{arcname} n'est pas un JSON valide (archivé tel quel)")
                    info = tarfile.TarInfo(arcname)
                    info.size = len(data)
                    info.mtime = int(path.stat().st_mtime)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
                else:
                    tar.add(path, arcname=arcname, recursive=False)
                files += 1
                if arcname.startswith(prefix) and "/" not in arcname[len(prefix):] and is_active_save(path.name):
                    prospects.append(path.name[: -len(".json")])
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    manifest = {"created": now.isoformat(), "files": files, "prospects": sorted(prospects)}
    _manifest_path(final).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return ArchiveResult(final, files, final.stat().st_size, sorted(prospects), warnings)


def apply_retention(backup_dir: Path, keep: int) -> list[Path]:
    """Supprime les archives au-delà des `keep` plus récentes. Retourne les archives supprimées."""
    if keep < 1:
        raise ValueError("keep doit être >= 1")
    removed = []
    for archive in list_archives(backup_dir)[keep:]:
        archive.unlink(missing_ok=True)
        _manifest_path(archive).unlink(missing_ok=True)
        removed.append(archive)
    return removed


def _load_restore_data(
    prospects_dir: Path, backup_dir: Path, prospect: str, source: str, ref: str, prospects_rel: str
) -> bytes:
    check_name(ref)
    if source == "game":
        if not (ref.startswith(f"{prospect}.json.backup") or ref.startswith(f"{prospect}.json.pre_restore_")):
            raise BackupError(f"{ref} n'est pas une sauvegarde de {prospect}")
        path = prospects_dir / ref
        if not path.is_file():
            raise BackupError(f"Sauvegarde introuvable : {ref}")
        return path.read_bytes()
    if source == "archive":
        if not _ARCHIVE_RE.match(ref):
            raise BackupError(f"Archive invalide : {ref}")
        archive = backup_dir / ref
        if not archive.is_file():
            raise BackupError(f"Archive introuvable : {ref}")
        member_name = str(PurePosixPath(prospects_rel) / f"{prospect}.json")
        with tarfile.open(archive, "r:gz") as tar:
            try:
                member = tar.getmember(member_name)
            except KeyError:
                raise BackupError(f"{prospect} absent de l'archive {ref}") from None
            if not member.isfile():
                raise BackupError(f"Entrée d'archive invalide : {member_name}")
            fileobj = tar.extractfile(member)
            if fileobj is None:
                raise BackupError(f"Lecture impossible : {member_name}")
            return fileobj.read()
    raise BackupError(f"Source inconnue : {source}")


def restore_prospect(
    prospects_dir: Path,
    backup_dir: Path,
    prospect: str,
    source: str,
    ref: str,
    prospects_rel: str,
    now: datetime,
) -> RestoreResult:
    """Remplace `<prospect>.json` par la sauvegarde choisie.

    L'actuelle est d'abord **copiée** en `.pre_restore_<date>`, et la sauvegarde source est
    conservée (copie, pas déplacement). Le conteneur doit être arrêté par l'appelant.
    """
    check_name(prospect)
    data = _load_restore_data(prospects_dir, backup_dir, prospect, source, ref, prospects_rel)
    try:
        json.loads(data)
    except ValueError:
        raise BackupError(f"{ref} n'est pas un JSON valide : restauration refusée") from None

    prospects_dir.mkdir(parents=True, exist_ok=True)
    target = prospects_dir / f"{prospect}.json"
    pre_restore = None
    if target.exists():
        pre_restore = target.with_name(f"{target.name}.pre_restore_{now.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, pre_restore)

    tmp = target.with_name(f".{target.name}.restore.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return RestoreResult(target, pre_restore)


def human_size(size: int) -> str:
    kb = size / 1024
    if kb < 1024:
        return f"{round(kb)} Ko"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} Mo"
    return f"{mb / 1024:.2f} Go"
