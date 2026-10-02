"""Point d'entrée : `python -m warden`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from .config import Config, ConfigError


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s : %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    try:
        config = Config.from_env()
    except ConfigError as e:
        logging.critical("Configuration invalide : %s", e)
        return 2

    from .bot import WardenBot

    async def runner() -> None:
        bot = WardenBot(config)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
            except NotImplementedError:  # Windows (développement)
                pass
        async with bot:
            await bot.start(config.discord_token)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
