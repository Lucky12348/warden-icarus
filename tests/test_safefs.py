"""Symlinks placed in data/ by a compromised game: the bot must neither read nor write through them."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from warden import backups as bk
from warden import safefs
from warden import settings_store as ss

INI = "[/Script/Icarus.DedicatedServerSettings]\nSaveGameOnExit=True\n"
NOW = datetime(2026, 10, 12, 18, 0, 0)


def symlink(link: Path, target: Path, is_dir: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("création de liens symboliques impossible sur ce système")


@pytest.fixture
def victim(tmp_path):
    """Sensitive file outside data/ (Icarus docker-compose.yml, /proc/self/environ...)."""
    path = tmp_path / "docker-compose.yml"
    path.write_text("services: {}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- safefs
def test_read_bytes_refuses_symlink(tmp_path, victim):
    link = tmp_path / "lien.ini"
    symlink(link, victim)
    with pytest.raises(safefs.UnsafePathError):
        safefs.read_bytes(link)


def test_atomic_write_replaces_planted_symlink(tmp_path, victim):
    link = tmp_path / "ServerSettings.ini.bak"
    symlink(link, victim)
    safefs.atomic_write(link, b"contenu du jeu")
    assert victim.read_text(encoding="utf-8") == "services: {}\n"  # target untouched
    assert not link.is_symlink() and link.read_bytes() == b"contenu du jeu"


def test_ensure_inside(tmp_path):
    root = tmp_path / "data"
    (root / "Saved").mkdir(parents=True)
    safefs.ensure_inside(root / "Saved" / "x", root)
    with pytest.raises(safefs.UnsafePathError):
        safefs.ensure_inside(tmp_path / "ailleurs" / "x", root)


def test_ensure_inside_refuses_symlinked_parent(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    symlink(root / "Config", tmp_path, is_dir=True)
    with pytest.raises(safefs.UnsafePathError):
        safefs.ensure_inside(root / "Config" / "ServerSettings.ini", root)


# --------------------------------------------------------------------------- ServerSettings.ini
def test_write_ini_never_writes_through_planted_backup_link(tmp_path, victim):
    data = tmp_path / "data"
    data.mkdir()
    ini = data / "ServerSettings.ini"
    ini.write_text(INI, encoding="utf-8")
    symlink(data / "ServerSettings.ini.bak", victim)
    ss.write_ini(ini, {"SaveGameOnExit": "False"}, root=data)
    assert victim.read_text(encoding="utf-8") == "services: {}\n"
    assert (data / "ServerSettings.ini.bak").read_text(encoding="utf-8") == INI
    assert ss.read_ini(ini)["SaveGameOnExit"] == "False"


def test_ini_symlink_is_neither_read_nor_written(tmp_path, victim):
    data = tmp_path / "data"
    data.mkdir()
    ini = data / "ServerSettings.ini"
    symlink(ini, victim)
    with pytest.raises(safefs.UnsafePathError):
        ss.read_ini(ini)
    with pytest.raises(safefs.UnsafePathError):
        ss.write_ini(ini, {"SaveGameOnExit": "False"}, root=data)
    assert victim.read_text(encoding="utf-8") == "services: {}\n"


# --------------------------------------------------------------------------- backups
@pytest.fixture
def layout(tmp_path):
    data = tmp_path / "data"
    saves = data / "Saved" / "PlayerData"
    prospects = saves / "DedicatedServer" / "Prospects"
    prospects.mkdir(parents=True)
    backups = tmp_path / "backups"
    backups.mkdir()
    return data, saves, prospects, backups


def test_restore_does_not_write_through_planted_pre_restore_link(layout, victim):
    data, _, prospects, backups = layout
    (prospects / "Olympus.json").write_text('{"v": 1}', encoding="utf-8")
    (prospects / "Olympus.json.backup_1").write_text('{"v": 0}', encoding="utf-8")
    symlink(prospects / "Olympus.json.pre_restore_20261012-180000", victim)
    bk.restore_prospect(prospects, backups, "Olympus", "game", "Olympus.json.backup_1",
                        "DedicatedServer/Prospects", NOW, root=data)
    assert victim.read_text(encoding="utf-8") == "services: {}\n"
    assert json.loads((prospects / "Olympus.json").read_text()) == {"v": 0}
    assert json.loads((prospects / "Olympus.json.pre_restore_20261012-180000").read_text()) == {"v": 1}


def test_restore_refuses_symlinked_save_and_source(layout, victim):
    data, _, prospects, backups = layout
    (prospects / "Olympus.json.backup_1").write_text('{"v": 0}', encoding="utf-8")
    symlink(prospects / "Olympus.json", victim)
    with pytest.raises(bk.BackupError):
        bk.restore_prospect(prospects, backups, "Olympus", "game", "Olympus.json.backup_1",
                            "DedicatedServer/Prospects", NOW, root=data)
    secret = victim.parent / "environ.json"
    secret.write_text('{"DISCORD_TOKEN": "x"}', encoding="utf-8")
    symlink(prospects / "Evil.json.backup_1", secret)
    with pytest.raises(bk.BackupError):
        bk.restore_prospect(prospects, backups, "Evil", "game", "Evil.json.backup_1",
                            "DedicatedServer/Prospects", NOW, root=data)
    assert victim.read_text(encoding="utf-8") == "services: {}\n"
    assert not (prospects / "Evil.json").exists()


def test_symlinks_are_not_listed_nor_archived(layout, victim):
    data, saves, prospects, backups = layout
    (prospects / "Olympus.json").write_text('{"v": 1}', encoding="utf-8")
    symlink(prospects / "Evil.json", victim)
    symlink(saves / "lien_dossier", victim.parent, is_dir=True)
    assert [p.name for p in bk.list_prospects(prospects)] == ["Olympus"]
    result = bk.create_archive(saves, backups, "DedicatedServer/Prospects", NOW, json_retry_delay=0, root=data)
    assert result.prospects == ["Olympus"] and result.files == 1


def test_archive_refuses_symlinked_saves_root(tmp_path, victim):
    data = tmp_path / "data"
    (data / "Saved").mkdir(parents=True)
    symlink(data / "Saved" / "PlayerData", tmp_path, is_dir=True)
    with pytest.raises(bk.BackupError):
        bk.create_archive(data / "Saved" / "PlayerData", tmp_path / "backups", "DedicatedServer/Prospects", NOW, root=data)


def test_archives_of_same_second_are_ordered(tmp_path):
    for name in ("icarus-20261012-120000.tar.gz", "icarus-20261012-120000-1.tar.gz", "icarus-20261012-115959.tar.gz"):
        (tmp_path / name).write_bytes(b"")
    assert [p.name for p in bk.list_archives(tmp_path)] == [
        "icarus-20261012-120000-1.tar.gz",
        "icarus-20261012-120000.tar.gz",
        "icarus-20261012-115959.tar.gz",
    ]
    removed = bk.apply_retention(tmp_path, 1)
    assert [p.name for p in removed] == ["icarus-20261012-120000.tar.gz", "icarus-20261012-115959.tar.gz"]


def test_read_bytes_size_limit(tmp_path):
    path = tmp_path / "ServerSettings.ini"
    path.write_bytes(b"x" * 11)
    assert safefs.read_bytes(path, 11)[0] == b"x" * 11
    with pytest.raises(safefs.UnsafePathError):
        safefs.read_bytes(path, 10)


def test_endless_log_line_is_cut(tmp_path):
    from warden.docker_ctl import MAX_LOG_LINE, IcarusDocker

    class Container:
        name, short_id = "icarus", "abc"

        def logs(self, **kwargs):
            return iter([b"y" * (MAX_LOG_LINE + 10), b"fin\n"])

    class Client:
        containers = None

        def get(self, name):
            return Container()

    client = Client()
    client.containers = client
    lines = list(IcarusDocker("icarus", tmp_path, client).logs(since=0))
    assert lines == ["y" * MAX_LOG_LINE, "fin"]


def test_swapped_parent_folder_cannot_redirect_writes(tmp_path, victim):
    """A folder of data/ replaced by a link (even after any check) never redirects a write."""
    data = tmp_path / "data"
    data.mkdir()
    outside = victim.parent / "state"
    outside.mkdir()
    symlink(data / "Saved", outside, is_dir=True)
    target = data / "Saved" / "state.json"
    with pytest.raises(safefs.UnsafePathError):
        safefs.atomic_write(target, b'{"pwned": true}', root=data)
    with pytest.raises(safefs.UnsafePathError):
        safefs.read_bytes(target, root=data)
    assert list(outside.iterdir()) == []


def test_root_relative_helpers_without_links(tmp_path):
    data = tmp_path / "data"
    (data / "Saved").mkdir(parents=True)
    target = data / "Saved" / "x.json"
    assert not safefs.lexists(target, data)
    safefs.atomic_write(target, b"{}", 0o640, root=data)
    assert safefs.lexists(target, data)
    assert safefs.read_bytes(target, root=data)[0] == b"{}"
    assert [p.name for p in (data / "Saved").iterdir()] == ["x.json"]  # no temporary file left
    with pytest.raises(safefs.UnsafePathError):
        safefs.atomic_write(tmp_path / "ailleurs.json", b"{}", root=data)


def test_linux_uses_directory_descriptors():
    """On Linux the race-free path must really be active (not silently the fallback)."""
    import os
    import sys

    if not sys.platform.startswith("linux"):
        pytest.skip("directory descriptors: Linux only")
    assert safefs.DIR_FD_SUPPORTED, {f.__name__: f in os.supports_dir_fd for f in (os.open, os.stat, os.unlink, os.rename)}
