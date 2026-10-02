import json
import os
import tarfile
import time
from datetime import datetime, timedelta

import pytest

from warden import backups as bk

REL = "DedicatedServer/Prospects"


@pytest.fixture
def layout(tmp_path):
    saves = tmp_path / "data" / "Saved" / "PlayerData"
    prospects = saves / REL
    prospects.mkdir(parents=True)
    backup_dir = tmp_path / "backups"
    return saves, prospects, backup_dir


def write_json(path, data, mtime=None):
    path.write_text(json.dumps(data), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_list_prospects_ignores_backups(layout):
    _, prospects, _ = layout
    write_json(prospects / "Olympus.json", {"v": 1}, mtime=time.time() - 100)
    write_json(prospects / "Styx.json", {"v": 1})
    write_json(prospects / "Olympus.json.backup_1", {"v": 0})
    write_json(prospects / "Olympus.json.pre_restore_20260101-060000", {"v": 0})
    names = [p.name for p in bk.list_prospects(prospects)]
    assert names == ["Styx", "Olympus"]  # plus récente d'abord


def test_create_archive_contents_and_manifest(layout):
    saves, prospects, backup_dir = layout
    write_json(prospects / "Olympus.json", {"v": 1})
    write_json(prospects / "Olympus.json.backup_1", {"v": 0})
    (saves / "123456").mkdir()
    write_json(saves / "123456" / "Characters.json", {"c": 1})

    result = bk.create_archive(saves, backup_dir, REL, datetime(2026, 10, 2, 6, 0, 0), json_retry_delay=0)

    assert result.path.name == "icarus-20261002-060000.tar.gz"
    assert result.prospects == ["Olympus"]
    with tarfile.open(result.path) as tar:
        names = sorted(tar.getnames())
    assert names == ["123456/Characters.json", f"{REL}/Olympus.json"]  # sans .backup
    manifest = json.loads((backup_dir / "icarus-20261002-060000.manifest.json").read_text())
    assert manifest["prospects"] == ["Olympus"]
    assert not list(backup_dir.glob("*.tmp"))


def test_create_archive_same_second_gets_unique_name(layout):
    saves, prospects, backup_dir = layout
    write_json(prospects / "A.json", {})
    now = datetime(2026, 10, 2, 6, 0, 0)
    first = bk.create_archive(saves, backup_dir, REL, now, json_retry_delay=0)
    second = bk.create_archive(saves, backup_dir, REL, now, json_retry_delay=0)
    assert first.path != second.path
    assert second.path.name == "icarus-20261002-060000-1.tar.gz"


def test_create_archive_flags_invalid_json(layout):
    saves, prospects, backup_dir = layout
    (prospects / "Broken.json").write_text("{not json", encoding="utf-8")
    result = bk.create_archive(saves, backup_dir, REL, datetime(2026, 1, 1), json_retry_delay=0)
    assert result.warnings and "Broken.json" in result.warnings[0]


def test_create_archive_missing_dir(tmp_path):
    with pytest.raises(bk.BackupError):
        bk.create_archive(tmp_path / "nope", tmp_path / "b", REL, datetime(2026, 1, 1))


def test_retention_keeps_most_recent(layout):
    saves, prospects, backup_dir = layout
    write_json(prospects / "A.json", {})
    base = datetime(2026, 10, 1, 6, 0, 0)
    for i in range(5):
        bk.create_archive(saves, backup_dir, REL, base + timedelta(hours=i), json_retry_delay=0)
    removed = bk.apply_retention(backup_dir, 3)
    assert [p.name for p in removed] == ["icarus-20261001-070000.tar.gz", "icarus-20261001-060000.tar.gz"]
    remaining = [p.name for p in bk.list_archives(backup_dir)]
    assert remaining == [
        "icarus-20261001-100000.tar.gz",
        "icarus-20261001-090000.tar.gz",
        "icarus-20261001-080000.tar.gz",
    ]
    assert not (backup_dir / "icarus-20261001-060000.manifest.json").exists()


def test_retention_ignores_foreign_files(layout):
    _, _, backup_dir = layout
    backup_dir.mkdir()
    (backup_dir / "manual-copy.tar.gz").write_bytes(b"x")
    assert bk.apply_retention(backup_dir, 1) == []
    assert (backup_dir / "manual-copy.tar.gz").exists()


def test_restore_from_game_backup_keeps_source(layout):
    _, prospects, backup_dir = layout
    write_json(prospects / "Olympus.json", {"v": "current"})
    write_json(prospects / "Olympus.json.backup_2", {"v": "old"})
    now = datetime(2026, 10, 2, 7, 30, 0)

    result = bk.restore_prospect(prospects, backup_dir, "Olympus", "game", "Olympus.json.backup_2", REL, now)

    assert json.loads((prospects / "Olympus.json").read_text()) == {"v": "old"}
    assert result.pre_restore.name == "Olympus.json.pre_restore_20261002-073000"
    assert json.loads(result.pre_restore.read_text()) == {"v": "current"}
    assert (prospects / "Olympus.json.backup_2").exists()  # copie, pas déplacement


def test_restore_from_archive(layout):
    saves, prospects, backup_dir = layout
    write_json(prospects / "Olympus.json", {"v": "archived"})
    archive = bk.create_archive(saves, backup_dir, REL, datetime(2026, 10, 1, 6, 0), json_retry_delay=0)
    write_json(prospects / "Olympus.json", {"v": "newer"})

    points = bk.list_restore_points(prospects, backup_dir, "Olympus", REL)
    assert any(p.source == "archive" and p.ref == archive.path.name for p in points)

    bk.restore_prospect(prospects, backup_dir, "Olympus", "archive", archive.path.name, REL, datetime(2026, 10, 2))
    assert json.loads((prospects / "Olympus.json").read_text()) == {"v": "archived"}


def test_restore_point_tokens(layout):
    _, prospects, backup_dir = layout
    write_json(prospects / "Olympus.json", {})
    write_json(prospects / "Olympus.json.backup_1", {})
    write_json(prospects / "Olympus.json.pre_restore_20260101-000000", {})
    write_json(prospects / "Other.json.backup_1", {})
    points = bk.list_restore_points(prospects, backup_dir, "Olympus", REL)
    assert sorted(p.kind for p in points) == ["backup", "pre_restore"]
    assert all(p.token.startswith("g:Olympus.json.") for p in points)


def test_restore_refuses_invalid_json(layout):
    _, prospects, backup_dir = layout
    write_json(prospects / "Olympus.json", {"v": 1})
    (prospects / "Olympus.json.backup_1").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(bk.BackupError, match="JSON"):
        bk.restore_prospect(prospects, backup_dir, "Olympus", "game", "Olympus.json.backup_1", REL, datetime(2026, 1, 1))
    assert json.loads((prospects / "Olympus.json").read_text()) == {"v": 1}
    assert not list(prospects.glob("*.pre_restore_*"))


@pytest.mark.parametrize("ref", ["../../etc/passwd", "Other.json.backup_1", "/abs", "..", ".hidden"])
def test_restore_rejects_foreign_or_traversal_refs(layout, ref):
    _, prospects, backup_dir = layout
    write_json(prospects / "Olympus.json", {})
    write_json(prospects / "Other.json.backup_1", {})
    with pytest.raises(bk.BackupError):
        bk.restore_prospect(prospects, backup_dir, "Olympus", "game", ref, REL, datetime(2026, 1, 1))


@pytest.mark.parametrize("prospect", ["../x", "a/b", "", ".."])
def test_rejects_bad_prospect_names(layout, prospect):
    _, prospects, backup_dir = layout
    with pytest.raises(bk.BackupError):
        bk.list_restore_points(prospects, backup_dir, prospect, REL)


def test_restore_rejects_bad_archive_name(layout):
    _, prospects, backup_dir = layout
    with pytest.raises(bk.BackupError):
        bk.restore_prospect(prospects, backup_dir, "Olympus", "archive", "evil.tar.gz", REL, datetime(2026, 1, 1))


def test_restore_without_current_file(layout):
    _, prospects, backup_dir = layout
    write_json(prospects / "Olympus.json.backup_1", {"v": 1})
    result = bk.restore_prospect(prospects, backup_dir, "Olympus", "game", "Olympus.json.backup_1", REL, datetime(2026, 1, 1))
    assert result.pre_restore is None
    assert (prospects / "Olympus.json").exists()


def test_human_size():
    assert bk.human_size(512) == "0 Ko"
    assert bk.human_size(2048) == "2 Ko"
    assert bk.human_size(5 * 1024 * 1024) == "5.0 Mo"
