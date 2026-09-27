"""Audit logging: /setup-logs plus listeners for message/member/channel events."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

MAX_FIELD = 1000


def _clip(text: Optional[str], limit: int = MAX_FIELD) -> str:
    if not text:
        return "*(empty / no content)*"
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Logging(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def log_event(
        self,
        guild: Optional[discord.Guild],
        *,
        title: str,
        color: discord.Color = discord.Color.blurple(),
        description: Optional[str] = None,
        fields: Optional[dict] = None,
    ) -> None:
        """Post an embed to the guild's configured log channel (silent if unset)."""
        if guild is None:
            return
        channel_id = await self.bot.db.get_setting(guild.id, "log_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden):
                return
        embed = discord.Embed(title=title, description=description, color=color)
        for name, value in (fields or {}).items():
            embed.add_field(name=name, value=_clip(value), inline=True)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("No send permission in log channel %s (%s)", channel, guild)

    @app_commands.command(name="setup-logs", description="Set the audit-log channel")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="Channel that receives moderation/audit logs")
    async def setup_logs(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self.bot.db.set_setting(
            interaction.guild.id, "log_channel_id", channel.id
        )
        await interaction.response.send_message(
            f"📋 Audit log channel set to {channel.mention}."
        )

    @app_commands.command(name="disable-logs", description="Stop audit logging")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def disable_logs(self, interaction: discord.Interaction) -> None:
        await self.bot.db.set_setting(interaction.guild.id, "log_channel_id", None)
        await interaction.response.send_message("Audit logging disabled.")

    # ---- listeners ----

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        await self.log_event(
            message.guild,
            title="Message deleted",
            color=discord.Color.dark_red(),
            fields={
                "Author": f"{message.author} (`{message.author.id}`)",
                "Channel": message.channel.mention,
                "Content": _clip(message.content),
            },
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages or messages[0].guild is None:
            return
        await self.log_event(
            messages[0].guild,
            title="Bulk message delete",
            color=discord.Color.dark_red(),
            fields={
                "Channel": messages[0].channel.mention,
                "Count": str(len(messages)),
            },
        )

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        if before.guild is None or before.author.bot:
            return
        if before.content == after.content:
            return  # embed/unfurl updates, not real edits
        await self.log_event(
            before.guild,
            title="Message edited",
            color=discord.Color.orange(),
            fields={
                "Author": f"{before.author} (`{before.author.id}`)",
                "Channel": before.channel.mention,
                "Before": _clip(before.content),
                "After": _clip(after.content),
            },
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        age = discord.utils.utcnow() - member.created_at
        await self.log_event(
            member.guild,
            title="Member joined",
            color=discord.Color.green(),
            fields={
                "Member": f"{member.mention} (`{member.id}`)",
                "Account age": f"{age.days} days",
            },
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.log_event(
            member.guild,
            title="Member left",
            color=discord.Color.dark_grey(),
            fields={"Member": f"{member} (`{member.id}`)"},
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        await self.log_event(
            guild,
            title="Member banned",
            color=discord.Color.red(),
            fields={"User": f"{user} (`{user.id}`)"},
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        await self.log_event(
            guild,
            title="Member unbanned",
            color=discord.Color.green(),
            fields={"User": f"{user} (`{user.id}`)"},
        )

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self.log_event(
            channel.guild,
            title="Channel created",
            color=discord.Color.green(),
            fields={"Channel": f"{channel.mention} (`{channel.id}`)", "Type": str(channel.type)},
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.log_event(
            channel.guild,
            title="Channel deleted",
            color=discord.Color.dark_red(),
            fields={"Channel": f"#{channel.name} (`{channel.id}`)", "Type": str(channel.type)},
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        changes = []
        if before.name != after.name:
            changes.append(f"name: `#{before.name}` → `#{after.name}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append("topic changed")
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(
                f"slowmode: {getattr(before, 'slowmode_delay', 0)}s → {getattr(after, 'slowmode_delay', 0)}s"
            )
        if not changes:
            return
        await self.log_event(
            after.guild,
            title="Channel updated",
            color=discord.Color.orange(),
            fields={"Channel": after.mention, "Changes": "\n".join(changes)},
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Logging(bot))
