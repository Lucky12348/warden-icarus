"""Map download from the backups panel: zip export, split above the Discord limit, admin only."""

import asyncio
import io
import json
import os
import zipfile
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from warden import backups as bk
from warden import handlers

TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 12, 18, 0, 0, tzinfo=TZ)
SAVE = {"ProspectInfo": {"ProspectID": "Olympus"}, "Blob": "x" * 5000}


@pytest.fixture
def layout(tmp_path):
    data = tmp_path / "data"
    prospects = data / "Saved" / "PlayerData" / "DedicatedServer" / "Prospects"
    prospects.mkdir(parents=True)
    return data, prospects


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


# --------------------------------------------------------------------------- export
def test_export_zips_the_current_save(layout):
    data, prospects = layout
    raw = json.dumps(SAVE).encode()
    (prospects / "Olympus.json").write_bytes(raw)
    (prospects / "Olympus.json.backup_1").write_text("{}")
    export = bk.export_prospect(prospects, "Olympus", NOW, root=data)
    assert export.filename == "icarus-Olympus-20261012-180000.zip"
    assert unzip(export.data) == {"Olympus.json": raw}  # only the current save
    assert export.json_size == len(raw) and export.valid
    assert len(export.data) < len(raw)  # compressed


def test_export_filename_is_safe_for_any_os(layout):
    data, prospects = layout
    (prospects / "Ma map: été 2026.json").write_text("{}")
    export = bk.export_prospect(prospects, "Ma map: été 2026", NOW, root=data)
    assert export.filename == "icarus-Ma_map_t_2026-20261012-180000.zip"
    assert list(unzip(export.data)) == ["Ma map: été 2026.json"]


def test_export_errors(layout):
    data, prospects = layout
    with pytest.raises(bk.BackupError, match="introuvable"):
        bk.export_prospect(prospects, "Absent", NOW, root=data)
    with pytest.raises(bk.BackupError):
        bk.export_prospect(prospects, "../../etc/passwd", NOW, root=data)
    (prospects / "Partial.json").write_text('{"cut')
    export = bk.export_prospect(prospects, "Partial", NOW, root=data, json_retry_delay=0)
    assert not export.valid and unzip(export.data) == {"Partial.json": b'{"cut'}


def test_split_parts_rebuilds_the_file():
    data = bytes(range(256)) * 41
    parts = bk.split_parts(data, 1000)
    assert len(parts) == 11 and all(len(p) <= 1000 for p in parts)
    assert b"".join(parts) == data
    assert bk.split_parts(b"abc", 10) == [b"abc"]


# --------------------------------------------------------------------------- Discord flow
class FakeResponse:
    def __init__(self):
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, **kwargs):
        self.done = True

    async def edit_message(self, **kwargs):
        self.done = True


class FakeInteraction:
    def __init__(self, filesize_limit, values=None):
        self.guild = SimpleNamespace(filesize_limit=filesize_limit)
        self.user = SimpleNamespace(id=42, display_name="Admin")
        self.data = {"values": values or []}
        self.response = FakeResponse()
        self.sent: list[tuple[str, bytes]] = []
        self.edits = 0
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, file=None, ephemeral=False, **kwargs):
        assert ephemeral  # the save is only visible to the admin who asked
        self.sent.append((file.filename, file.fp.read()))

    async def edit_original_response(self, **kwargs):
        self.edits += 1


def fake_bot(data, prospects, admin=True):
    actions = []

    async def require_admin(interaction, action):
        return admin

    async def action(user, what, result, *args):
        actions.append(what)

    config = SimpleNamespace(prospects_dir=prospects, data_dir=data, timezone=TZ)
    return SimpleNamespace(config=config, require_admin=require_admin, notifier=SimpleNamespace(action=action), actions=actions)


def run(coro):
    asyncio.run(coro)


def test_single_map_is_sent_in_one_click(layout):
    data, prospects = layout
    (prospects / "Olympus.json").write_text(json.dumps(SAVE))
    bot, interaction = fake_bot(data, prospects), FakeInteraction(10 * 1024 * 1024)
    run(handlers.backups_component(bot, interaction, "download", ""))
    assert len(interaction.sent) == 1 and interaction.sent[0][0].startswith("icarus-Olympus-")
    assert unzip(interaction.sent[0][1]) == {"Olympus.json": json.dumps(SAVE).encode()}
    assert bot.actions == ["Téléchargement — Olympus"]


def test_several_maps_ask_which_one(layout):
    data, prospects = layout
    for name in ("Olympus", "Styx"):
        (prospects / f"{name}.json").write_text(json.dumps(SAVE))
    bot, interaction = fake_bot(data, prospects), FakeInteraction(10 * 1024 * 1024)
    run(handlers.backups_component(bot, interaction, "download", ""))
    assert interaction.sent == []  # a menu is shown first
    pick = FakeInteraction(10 * 1024 * 1024, values=["Styx"])
    run(handlers.backups_component(bot, pick, "dlpick", ""))
    assert [name for name, _ in pick.sent][0].startswith("icarus-Styx-")


def test_big_map_is_split_into_parts(layout, monkeypatch):
    data, prospects = layout
    raw = os.urandom(1_500_000)  # random: does not compress, ~1.5 MB once zipped
    (prospects / "Olympus.json").write_text(json.dumps({"b": raw.hex()}))
    monkeypatch.setattr(handlers, "UPLOAD_MARGIN", 0)
    bot, interaction = fake_bot(data, prospects), FakeInteraction(1024 * 1024)  # 1 MB limit
    run(handlers.backups_component(bot, interaction, "download", ""))
    names = [name for name, _ in interaction.sent]
    assert len(names) > 1 and names[0].endswith(".zip.001") and names[1].endswith(".zip.002")
    assert all(len(part) <= 1024 * 1024 for _, part in interaction.sent)
    rebuilt = b"".join(part for _, part in interaction.sent)
    assert list(unzip(rebuilt)) == ["Olympus.json"]


def test_too_big_map_is_refused(layout, monkeypatch):
    data, prospects = layout
    (prospects / "Olympus.json").write_text(json.dumps({"b": os.urandom(1_500_000).hex()}))
    monkeypatch.setattr(handlers, "MAX_UPLOAD_PARTS", 1)
    bot, interaction = fake_bot(data, prospects), FakeInteraction(1024 * 1024)
    run(handlers.backups_component(bot, interaction, "download", ""))
    assert interaction.sent == [] and bot.actions == []


def test_download_requires_admin(layout):
    data, prospects = layout
    (prospects / "Olympus.json").write_text(json.dumps(SAVE))
    bot = fake_bot(data, prospects, admin=False)
    for action, values in (("download", None), ("dlpick", ["Olympus"])):
        interaction = FakeInteraction(10 * 1024 * 1024, values=values)
        run(handlers.backups_component(bot, interaction, action, ""))
        assert interaction.sent == []
