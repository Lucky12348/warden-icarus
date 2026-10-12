"""Safe file access in the folders writable by the game container.

`data/` (saves, ServerSettings.ini) is writable by the Icarus server, which is exposed to the
Internet. A compromised game could place symlinks there so that the bot, which runs with the
Docker socket, reads or overwrites another file (Icarus docker-compose.yml,
/proc/self/environ with DISCORD_TOKEN...). Rules applied here:

- read: never through a symlink (O_NOFOLLOW), and only a regular file;
- write: temporary file with an unpredictable name (exclusive creation), then `os.replace`,
  which replaces a planted link instead of writing through it;
- paths: given a `root` (data/), the folders are walked from it one level at a time through
  directory file descriptors, without following any link (Linux). A folder swapped for a link
  between a check and the write therefore cannot redirect the write (no time-of-check gap).
  Where `dir_fd` is not supported (Windows, development), the real folder is checked instead.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import tempfile
from pathlib import Path

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_BINARY = getattr(os, "O_BINARY", 0)
SMALL_FILE = 1024 * 1024  # configuration files / manifests
SAVE_FILE = 512 * 1024 * 1024  # Icarus saves (a few tens of MB at most in practice)

# openat()-style access (Linux): every folder below `root` is opened without following links.
# os.rename (not os.replace, which Python leaves out of supports_dir_fd although it accepts it):
# on POSIX, rename atomically overwrites the destination exactly like replace.
DIR_FD_SUPPORTED = bool(_NOFOLLOW and _DIRECTORY) and all(
    f in os.supports_dir_fd for f in (os.open, os.stat, os.unlink, os.rename)
)


class UnsafePathError(OSError):
    pass


def _refused(path: Path, why: str) -> UnsafePathError:
    return UnsafePathError(f"{path.name} {why} : refusé")


def ensure_inside(path: Path, root: Path) -> None:
    """Refuse a path whose real folder leaves `root` (parent folder replaced by a link)."""
    real_root = os.path.realpath(root)
    real_parent = os.path.realpath(path.parent)
    if real_parent != real_root and not real_parent.startswith(real_root + os.sep):
        raise UnsafePathError(f"{path.parent} sort de {root} : refusé")


def _open_dir(folder: Path, root: Path) -> int:
    """Descriptor of `folder`, opened from `root` one level at a time without following links."""
    rel = os.path.relpath(folder, root)
    parts = [] if rel == os.curdir else Path(rel).parts
    if any(p in (os.pardir, "") for p in parts):
        raise UnsafePathError(f"{folder} sort de {root} : refusé")
    fd = os.open(root, os.O_RDONLY | _DIRECTORY)
    try:
        for part in parts:
            try:
                child = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise UnsafePathError(f"{folder} : « {part} » est un lien symbolique : refusé") from e
                raise
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def lexists(path: Path, root: Path | None = None) -> bool:
    """True if `path` exists (a link counts as existing; it is never followed)."""
    if root is None or not DIR_FD_SUPPORTED:
        return os.path.lexists(path)
    try:
        dfd = _open_dir(path.parent, root)
    except FileNotFoundError:
        return False
    try:
        os.stat(path.name, dir_fd=dfd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(dfd)


def _read_fd(fd: int, path: Path, max_size: int | None) -> tuple[bytes, int]:
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise _refused(path, "n'est pas un fichier ordinaire")
        if max_size is None:
            return f.read(), stat.S_IMODE(st.st_mode)
        data = f.read(max_size + 1)  # the reported size (procfs...) is not reliable: bounded read
        if len(data) > max_size:
            raise UnsafePathError(f"{path.name} dépasse {max_size} octets : refusé")
        return data, stat.S_IMODE(st.st_mode)


def read_bytes(path: Path, max_size: int | None = None, root: Path | None = None) -> tuple[bytes, int]:
    """Content and mode of a regular file, without following a symlink.

    `max_size`: refuse a bigger file (memory protection against a trapped file).
    `root`: the folders between `root` and the file are not followed either.
    """
    if root is not None and DIR_FD_SUPPORTED:
        dfd = _open_dir(path.parent, root)
        try:
            fd = os.open(path.name, os.O_RDONLY | _NOFOLLOW, dir_fd=dfd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise _refused(path, "est un lien symbolique") from e
            raise
        finally:
            os.close(dfd)
        return _read_fd(fd, path, max_size)
    if root is not None:
        ensure_inside(path, root)
    if path.is_symlink():  # O_NOFOLLOW missing (Windows): check beforehand
        raise _refused(path, "est un lien symbolique")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
    except OSError as e:
        if path.is_symlink():
            raise _refused(path, "est un lien symbolique") from e
        raise
    return _read_fd(fd, path, max_size)


def is_regular(path: Path) -> bool:
    """Regular file, symlink excluded."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def atomic_write(path: Path, data: bytes, mode: int = 0o644, root: Path | None = None) -> None:
    """Write `path` through an unpredictable temporary file, then `os.replace` (never through a link).

    `root`: the folders between `root` and the file are opened without following links.
    """
    if root is not None and DIR_FD_SUPPORTED:
        dfd = _open_dir(path.parent, root)
        tmp = f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    os.fchmod(f.fileno(), mode)
                os.rename(tmp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except OSError:
                    pass
                raise
        finally:
            os.close(dfd)
        return
    if root is not None:
        ensure_inside(path, root)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
