"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    discord_token: str
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    database_path: str = "data/guildmaster.db"
    sync_guild_id: Optional[int] = None
    log_level: str = "INFO"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def load_settings() -> Settings:
    guild_id = os.environ.get("DEV_GUILD_ID", "").strip()
    return Settings(
        discord_token=_required("DISCORD_BOT_TOKEN"),
        openai_api_key=_required("OPENAI_API_KEY"),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        database_path=os.environ.get("DATABASE_PATH", "data/guildmaster.db").strip()
        or "data/guildmaster.db",
        sync_guild_id=int(guild_id) if guild_id else None,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
    )
