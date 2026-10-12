"""Backups of Icarus saves (prospects).

- Active saves: `Prospects/<name>.json`.
- Game backups: `<name>.json.backup_N` (created by Icarus).
- Restore safety copies: `<name>.json.pre_restore_<YYYYMMDD-HHMMSS>` (same format as the
  old bot, so existing ones are still recognised).
- Bot archives: `backups/icarus-<YYYYMMDD-HHMMSS>.tar.gz` containing all of `Saved/PlayerData`
  (without .backup/.pre_restore files), alongside a `.manifest.json` listing the saves.

This module depends on neither Discord nor Docker: restoring assumes the caller has already
stopped the container.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path, PurePosixPath

from . import safefs

ARCHIVE_PREFIX = "icarus-"
ARCHIVE_SUFFIX = ".tar.gz"
MANIFEST_SUFFIX = ".manifest.json"
_ARCHIVE_RE = re.compile(r"^icarus-(\d{8}-\d{6})(?:-(\d+))?\.tar\.gz$")
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
    source: str  # "game" (file next to the save) or "archive"
    ref: str  # name of the .backup/.pre_restore file, or of the archive
    kind: str  # "backup", "pre_restore" or "archive"
    modified: datetime
    size: int

    @property
    def token(self) -> str:
        """Short value for a Discord menu (100 characters max)."""
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
    """Reject anything that could escape the folder (value coming from a Discord interaction)."""
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
        if safefs.is_regular(path) and is_active_save(path.name):  # symlinks excluded
            st = path.stat()
            out.append(Prospect(path.name[: -len(".json")], path, datetime.fromtimestamp(st.st_mtime, tz), st.st_size))
    return sorted(out, key=lambda p: p.modified, reverse=True)


def _archive_order(path: Path) -> tuple[str, int]:
    """(timestamp, n): "...-120000-1" is newer than "...-120000" (same second)."""
    m = _ARCHIVE_RE.match(path.name)
    assert m is not None
    return m.group(1), int(m.group(2) or 0)


def list_archives(backup_dir: Path) -> list[Path]:
    """Bot archives, newest first (the name is timestamped)."""
    if not backup_dir.is_dir():
        return []
    archives = (p for p in backup_dir.iterdir() if p.is_file() and _ARCHIVE_RE.match(p.name))
    return sorted(archives, key=_archive_order, reverse=True)


def _manifest_path(archive: Path) -> Path:
    return archive.with_name(archive.name[: -len(ARCHIVE_SUFFIX)] + MANIFEST_SUFFIX)


def archive_prospects(archive: Path, prospects_rel: str) -> list[str]:
    """Saves contained in an archive (manifest if present, otherwise read the archive)."""
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
            if not safefs.is_regular(path):
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
            continue  # corrupt archive: skipped rather than blocking the list
    return sorted(points, key=lambda p: p.modified, reverse=True)


def _read_valid_json(
    path: Path, attempts: int = 3, delay: float = 1.0, root: Path | None = None
) -> tuple[bytes, bool]:
    """Read a JSON file; retry if the game is still writing it."""
    data = b""
    for i in range(attempts):
        data = safefs.read_bytes(path, safefs.SAVE_FILE, root)[0]
        try:
            json.loads(data)
            return data, True
        except ValueError:
            if i < attempts - 1:
                time.sleep(delay)
    return data, False


@dataclass(frozen=True)
class Export:
    filename: str  # e.g. icarus-Olympus-20261012-180000.zip
    data: bytes  # zip containing <prospect>.json
    json_size: int
    valid: bool  # False: the game was still writing the save (exported as is)


def _safe_filename(name: str) -> str:
    """File name usable on every OS (save names are chosen in game)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return cleaned[:60] or "map"


def export_prospect(
    prospects_dir: Path, prospect: str, now: datetime, root: Path | None = None, json_retry_delay: float = 1.0
) -> Export:
    """Zip of the current `<prospect>.json`, to download from Discord.

    Read-only: no symlink followed, real folder under `root` (data/) when given. The save is
    re-read until it is valid JSON, so it can be exported with the server running.
    """
    check_name(prospect)
    path = prospects_dir / f"{prospect}.json"
    try:
        if not safefs.lexists(path, root):
            raise BackupError(f"Partie introuvable : {prospect}")
        data, valid = _read_valid_json(path, delay=json_retry_delay, root=root)
    except safefs.UnsafePathError as e:
        raise BackupError(str(e)) from None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        info = zipfile.ZipInfo(f"{prospect}.json", date_time=now.timetuple()[:6])
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, data)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return Export(f"icarus-{_safe_filename(prospect)}-{stamp}.zip", buffer.getvalue(), len(data), valid)


def split_parts(data: bytes, part_size: int) -> list[bytes]:
    """Cut `data` into consecutive parts of at most `part_size` bytes (raw split: the parts
    concatenated give the original file back, as 7-Zip does with `.zip.001`, `.zip.002`...)."""
    if part_size < 1:
        raise ValueError("part_size must be >= 1")
    return [data[i:i + part_size] for i in range(0, len(data), part_size)] or [b""]


def _unique_archive_path(backup_dir: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"{ARCHIVE_PREFIX}{stamp}{ARCHIVE_SUFFIX}"
    n = 1
    while path.exists():
        path = backup_dir / f"{ARCHIVE_PREFIX}{stamp}-{n}{ARCHIVE_SUFFIX}"
        n += 1
    return path


def _walk_regular_files(root: Path) -> list[Path]:
    """Regular files under `root`, sorted, without following any symlink (file or folder)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
        out.extend(base / f for f in filenames if safefs.is_regular(base / f))
    return sorted(out)


def create_archive(
    saves_root: Path,
    backup_dir: Path,
    prospects_rel: str,
    now: datetime,
    json_retry_delay: float = 1.0,
    root: Path | None = None,
) -> ArchiveResult:
    """Archive `saves_root` (Saved/PlayerData) as a timestamped tar.gz, without the game backups.

    Works with the server running: each .json is re-read until it is valid JSON, so that a
    file being written is not archived. No symlink is followed, and the real folder must stay
    under `root` (data/) when given.
    """
    if not saves_root.is_dir():
        raise BackupError(f"Dossier des sauvegardes introuvable : {saves_root}")
    if root is not None:
        try:
            safefs.ensure_inside(saves_root / "_", root)
        except safefs.UnsafePathError as e:
            raise BackupError(str(e)) from None
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = _unique_archive_path(backup_dir, now)
    tmp = final.with_name(final.name + ".tmp")
    prefix = prospects_rel.rstrip("/") + "/"
    files, prospects, warnings = 0, [], []
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for path in _walk_regular_files(saves_root):
                if any(m in path.name for m in _EXCLUDED_MARKERS):
                    continue
                arcname = path.relative_to(saves_root).as_posix()
                if path.suffix == ".json":
                    data, valid = _read_valid_json(path, delay=json_retry_delay, root=root)
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
    """Delete archives beyond the `keep` newest. Return the deleted archives."""
    if keep < 1:
        raise ValueError("keep doit être >= 1")
    removed = []
    for archive in list_archives(backup_dir)[keep:]:
        archive.unlink(missing_ok=True)
        _manifest_path(archive).unlink(missing_ok=True)
        removed.append(archive)
    return removed


def _load_restore_data(
    prospects_dir: Path, backup_dir: Path, prospect: str, source: str, ref: str, prospects_rel: str,
    root: Path | None = None,
) -> bytes:
    check_name(ref)
    if source == "game":
        if not (ref.startswith(f"{prospect}.json.backup") or ref.startswith(f"{prospect}.json.pre_restore_")):
            raise BackupError(f"{ref} n'est pas une sauvegarde de {prospect}")
        path = prospects_dir / ref
        try:
            if not safefs.lexists(path, root):
                raise BackupError(f"Sauvegarde introuvable : {ref}")
            return safefs.read_bytes(path, safefs.SAVE_FILE, root)[0]  # never through a link (folder writable by the game)
        except safefs.UnsafePathError as e:
            raise BackupError(str(e)) from None
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
    root: Path | None = None,
) -> RestoreResult:
    """Replace `<prospect>.json` with the chosen backup.

    The current file is first **copied** to `.pre_restore_<date>`, and the source backup is
    kept (copy, not move). The caller must have stopped the container.
    The saves folder is writable by the game: no read or write goes through a symlink, and
    the real folder must stay under `root` (data/) when given.
    """
    check_name(prospect)
    data = _load_restore_data(prospects_dir, backup_dir, prospect, source, ref, prospects_rel, root)
    try:
        json.loads(data)
    except ValueError:
        raise BackupError(f"{ref} n'est pas un JSON valide : restauration refusée") from None

    prospects_dir.mkdir(parents=True, exist_ok=True)
    target = prospects_dir / f"{prospect}.json"
    try:
        mode = 0o644
        pre_restore = None
        if safefs.lexists(target, root):
            current, mode = safefs.read_bytes(target, safefs.SAVE_FILE, root)
            pre_restore = target.with_name(f"{target.name}.pre_restore_{now.strftime('%Y%m%d-%H%M%S')}")
            safefs.atomic_write(pre_restore, current, mode, root)
        safefs.atomic_write(target, data, mode, root)
    except safefs.UnsafePathError as e:
        raise BackupError(f"{e} — restauration annulée, rien n'a été modifié") from None
    return RestoreResult(target, pre_restore)


def human_size(size: int) -> str:
    kb = size / 1024
    if kb < 1024:
        return f"{round(kb)} Ko"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} Mo"
    return f"{mb / 1024:.2f} Go"
