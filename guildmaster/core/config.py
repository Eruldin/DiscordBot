"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    discord_token: str
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    llm_provider: str = "openai"  # "openai" | "gemini"
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-3.8-flash"
    database_path: str = "data/guildmaster.db"
    sync_guild_id: Optional[int] = None
    log_level: str = "INFO"
    panel_enabled: bool = True
    panel_host: str = "127.0.0.1"
    panel_port: int = 8080
    panel_token: Optional[str] = None


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def load_settings() -> Settings:
    guild_id = os.environ.get("DEV_GUILD_ID", "").strip()
    provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower() or "openai"
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip() or None
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip() or None
    if provider not in ("openai", "gemini"):
        raise RuntimeError(f"Unknown LLM_PROVIDER {provider!r} — use 'openai' or 'gemini'")
    if provider == "openai" and not openai_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set (or set LLM_PROVIDER=gemini with GEMINI_API_KEY)"
        )
    if provider == "gemini" and not gemini_key:
        raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
    return Settings(
        discord_token=_required("DISCORD_BOT_TOKEN"),
        openai_api_key=openai_key,
        llm_provider=provider,
        gemini_api_key=gemini_key,
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
        or "gemini-3.8-flash",
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        database_path=os.environ.get("DATABASE_PATH", "data/guildmaster.db").strip()
        or "data/guildmaster.db",
        sync_guild_id=int(guild_id) if guild_id else None,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
        panel_enabled=os.environ.get("PANEL_ENABLED", "true").strip().lower()
        not in ("0", "false", "no"),
        panel_host=os.environ.get("PANEL_HOST", "127.0.0.1").strip() or "127.0.0.1",
        panel_port=int(os.environ.get("PANEL_PORT", "8080").strip() or "8080"),
        panel_token=os.environ.get("PANEL_TOKEN", "").strip() or None,
    )
