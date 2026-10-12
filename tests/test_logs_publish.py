"""Publishing to the logs channel: whitelist, real update, /logs."""

import asyncio
from types import SimpleNamespace

from warden.bot import WardenBot
from warden.commands import MESSAGE_LIMIT, format_logs
from warden.logpatterns import LogPatterns

STAMP = "2026-10-12T18:38:27.416174+00:00"


def real(message, program="icarus-server"):
    return f"{STAMP} icarus supervisord: {program} {message}#015"


class FakeNotifier:
    def __init__(self):
        self.lines, self.posts = [], []

    async def queue_log_line(self, line):
        self.lines.append(line)

    async def post(self, text, *args, **kwargs):
        self.posts.append(text)


def fake_bot(error_logs=False, updated=False, detection="a2s"):
    async def real_update():
        return updated

    return SimpleNamespace(
        patterns=LogPatterns.load(None),
        config=SimpleNamespace(error_logs_to_discord=error_logs, player_detection=detection),
        notifier=FakeNotifier(),
        _real_update_happened=real_update,
    )


def handle(bot, line):
    asyncio.run(WardenBot._handle_log_line(bot, line))


def test_noise_and_errors_are_not_queued():
    bot = fake_bot()
    for message in (
        "LogTalentGame: Warning: UnlockTalent Failed - Seed_Pouch 1",
        "IcarusOSSLog: Error: Login() failed: Empty Id",
        "LogNet: Error: connection refused",
    ):
        handle(bot, real(message))
    handle(bot, f"{STAMP} icarus supervisord: message repeated 6 times: [ icarus-server LogTemp: Warning: x]#015")
    assert bot.notifier.lines == [] and bot.notifier.posts == []


def test_errors_are_queued_with_option():
    bot = fake_bot(error_logs=True)
    handle(bot, real("LogNet: Error: connection refused"))
    assert bot.notifier.lines == ["❌ [icarus-server] LogNet: Error: connection refused"]


def test_startup_and_crash_are_queued():
    bot = fake_bot()
    handle(bot, real("LogInit: Display: Engine is initialized."))
    handle(bot, real("Ran out of memory allocating 0 bytes"))
    assert bot.notifier.lines == [
        "✅ [icarus-server] LogInit: Display: Engine is initialized.",
        "💥 [icarus-server] Ran out of memory allocating 0 bytes",
    ]


def test_update_line_only_after_a_real_update():
    bot = fake_bot(updated=False)
    handle(bot, real("Success! App '2089300' fully installed.", "icarus-updater"))
    handle(bot, real("Success! App '2089300' already up to date.", "icarus-updater"))
    assert bot.notifier.lines == []
    bot = fake_bot(updated=True)
    handle(bot, real("Success! App '2089300' fully installed.", "icarus-updater"))
    assert bot.notifier.lines == ["⬆️ [icarus-updater] Success! App '2089300' fully installed."]


def test_format_logs_inline_or_file():
    assert format_logs([]) == "ℹ️ Aucune ligne de logs."
    text = format_logs(["[icarus-server] a", "b ``` c"])
    assert text == "```\n[icarus-server] a\nb ''' c\n```"
    assert format_logs(["x" * 100] * 30) is None  # too long: sent as .txt
    assert len(format_logs(["x" * 100] * 15)) <= MESSAGE_LIMIT


def test_recent_logs_are_normalized_and_skip_empty_lines():
    bot = SimpleNamespace(docker=SimpleNamespace(tail=lambda n: [real("LogInit: Display: Engine is initialized."), real(""), ""]))
    lines = asyncio.run(WardenBot.recent_logs(bot, 50))
    assert lines == ["[icarus-server] LogInit: Display: Engine is initialized."]


def test_logs_file_keeps_the_most_recent_lines():
    from warden.commands import logs_file

    lines = [f"ligne {i}" for i in range(10)]
    payload, kept = logs_file(lines, 10_000)
    assert kept == 10 and payload.decode() == "\n".join(lines) + "\n"
    payload, kept = logs_file(lines, 25)  # "ligne N\n" = 8 bytes: 3 lines fit
    assert kept == 3 and payload.decode() == "ligne 7\nligne 8\nligne 9\n"
    assert logs_file(["x" * 100], 10) == (b"", 0)


def test_docker_stats_are_cached_for_open_commands():
    calls = []

    def stats():
        calls.append(1)
        return "stats"

    bot = SimpleNamespace(
        docker=SimpleNamespace(stats=stats), _stats_lock=asyncio.Lock(), _stats_cache=None
    )

    async def many_clicks():
        return await asyncio.gather(*(WardenBot._cached_stats(bot) for _ in range(20)))

    assert asyncio.run(many_clicks()) == ["stats"] * 20
    assert len(calls) == 1  # 20 clicks, a single docker stats call
