"""Safe file access in the folders writable by the game container.

`data/` (saves, ServerSettings.ini) is writable by the Icarus server, which is exposed to the
Internet. A compromised game could place symlinks there so that the bot, which runs with the
Docker socket, reads or overwrites another file (Icarus docker-compose.yml,
/proc/self/environ with DISCORD_TOKEN...). Rules applied here:

- read: never through a symlink (O_NOFOLLOW), and only a regular file;
- write: temporary file with an unpredictable name (mkstemp, exclusive creation), then
  `os.replace`, which replaces a planted link instead of writing through it;
- paths: the real folder must stay under the expected root (parent folders as links).
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
SMALL_FILE = 1024 * 1024  # configuration files / manifests
SAVE_FILE = 512 * 1024 * 1024  # Icarus saves (a few tens of MB at most in practice)
_BINARY = getattr(os, "O_BINARY", 0)


class UnsafePathError(OSError):
    pass


def read_bytes(path: Path, max_size: int | None = None) -> tuple[bytes, int]:
    """Content and mode of a regular file, without following a symlink.

    `max_size`: refuse a bigger file (memory protection against a trapped file).
    """
    if path.is_symlink():  # O_NOFOLLOW missing (Windows): check beforehand
        raise UnsafePathError(f"{path.name} est un lien symbolique : refusé")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
    except OSError as e:
        if path.is_symlink():
            raise UnsafePathError(f"{path.name} est un lien symbolique : refusé") from e
        raise
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePathError(f"{path.name} n'est pas un fichier ordinaire : refusé")
        if max_size is None:
            return f.read(), stat.S_IMODE(st.st_mode)
        data = f.read(max_size + 1)  # the reported size (procfs...) is not reliable: bounded read
        if len(data) > max_size:
            raise UnsafePathError(f"{path.name} dépasse {max_size} octets : refusé")
        return data, stat.S_IMODE(st.st_mode)


def is_regular(path: Path) -> bool:
    """Regular file, symlink excluded."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write `path` through an unpredictable temporary file, then `os.replace` (never through a link)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_inside(path: Path, root: Path) -> None:
    """Refuse a path whose real folder leaves `root` (parent folder replaced by a link)."""
    real_root = os.path.realpath(root)
    real_parent = os.path.realpath(path.parent)
    if real_parent != real_root and not real_parent.startswith(real_root + os.sep):
        raise UnsafePathError(f"{path.parent} sort de {root} : refusé")
