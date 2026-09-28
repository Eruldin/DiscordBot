"""GuildMasterBot: discord.py client wiring, extension loading, error handling."""
from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from guildmaster.core.config import Settings
from guildmaster.core.database import Database

log = logging.getLogger(__name__)

COGS = (
    "guildmaster.cogs.moderation",
    "guildmaster.cogs.scaffolding",
    "guildmaster.cogs.logging",
)


class GuildMasterTree(app_commands.CommandTree):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        error = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            text = "You don't have permission to use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            text = "I'm missing permission(s): " + ", ".join(
                f"`{p}`" for p in error.missing_permissions
            )
        elif isinstance(error, app_commands.CheckFailure):
            text = "You can't use this command here."
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"Slow down — try again in {error.retry_after:.0f}s."
        else:
            log.exception("Unhandled app command error", exc_info=error)
            text = "Something went wrong while running that command."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


class GuildMasterBot(commands.Bot):
    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        intents = discord.Intents.default()
        intents.members = True  # privileged: Server Members Intent
        intents.message_content = True  # privileged: Message Content Intent
        intents.moderation = True  # ban/unban audit events
        super().__init__(
            command_prefix="!",  # slash-only bot; prefix is unused
            intents=intents,
            tree_cls=GuildMasterTree,
            **kwargs,
        )
        self.settings = settings
        self.db = Database(settings.database_path)
        self.panel_task: Any = None

    async def setup_hook(self) -> None:
        await self.db.connect()
        for ext in COGS:
            await self.load_extension(ext)
        if self.settings.panel_enabled:
            from guildmaster.panel.server import start_panel

            await start_panel(self)
        if self.settings.sync_guild_id:
            guild = discord.Object(id=self.settings.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user and self.user.id)

    async def close(self) -> None:
        if self.panel_task is not None:
            self.panel_task.cancel()
        await self.db.close()
        await super().close()
