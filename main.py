"""GuildMaster entrypoint."""
from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from guildmaster.core.bot import GuildMasterBot
from guildmaster.core.config import load_settings


def main() -> int:
    load_dotenv()
    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    bot = GuildMasterBot(settings)
    bot.run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
