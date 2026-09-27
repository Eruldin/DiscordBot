"""Master moderation suite: timeouts, bans, kicks, strikes, purge, locks, anti-raid."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from guildmaster.utils.duration import format_timedelta, parse_duration
from guildmaster.utils.hierarchy import bot_permission_problem, hierarchy_problem

log = logging.getLogger(__name__)

STRIKE_TIMEOUT_THRESHOLD = 3
STRIKE_BAN_THRESHOLD = 5

ANTIRAID_DEFAULTS = {
    "antiraid_enabled": False,
    "antiraid_min_age_hours": 24,
    "antiraid_join_threshold": 6,
    "antiraid_window_seconds": 30,
    "antiraid_cooldown_seconds": 120,
    "antiraid_timeout_minutes": 60,
    "antiraid_lock_channel_id": None,
}


@dataclass
class RaidState:
    joins: deque = field(default_factory=deque)
    active_until: float = 0.0
    locked_channel_id: Optional[int] = None
    saved_send_messages: Optional[bool] = None
    release_task: Optional[asyncio.Task] = None


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._raids: dict[int, RaidState] = {}

    # ---- helpers ----

    async def _log(self, guild: discord.Guild, **kwargs) -> None:
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is not None:
            await logging_cog.log_event(guild, **kwargs)

    async def _guard_target(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> bool:
        problem = hierarchy_problem(interaction.guild, interaction.user, member)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return False
        return True

    async def _apply_strike_consequences(
        self,
        guild: discord.Guild,
        member: discord.Member,
        count: int,
        reason: str,
    ) -> Optional[str]:
        """Auto-escalate on strike totals: >=5 ban, >=3 timeout."""
        try:
            if count >= STRIKE_BAN_THRESHOLD:
                await guild.ban(
                    member,
                    reason=f"[auto] {count} strikes — last: {reason}",
                    delete_message_seconds=0,
                )
                await self.bot.db.clear_strikes(guild.id, member.id)
                return f"Reached {count} strikes — **banned**."
            if count >= STRIKE_TIMEOUT_THRESHOLD:
                length = timedelta(hours=1) if count == 3 else timedelta(hours=24)
                await member.timeout(
                    length, reason=f"[auto] {count} strikes — last: {reason}"
                )
                return (
                    f"Reached {count} strikes — timed out for "
                    f"{format_timedelta(length)}."
                )
        except discord.Forbidden:
            return "Auto-action failed: I lack permission or the member outranks me."
        return None

    # ---- commands ----

    @app_commands.command(name="timeout", description="Temporarily mute a member")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(
        member="Member to time out",
        duration="e.g. 10m, 2h, 1d, 45 (bare number = minutes; max 28d)",
        reason="Reason for the timeout",
    )
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: Optional[str] = None,
    ) -> None:
        if not await self._guard_target(interaction, member):
            return
        try:
            delta = parse_duration(duration)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await member.timeout(delta, reason=reason)
        await interaction.response.send_message(
            f"⏳ {member.mention} timed out for {format_timedelta(delta)}."
            + (f" Reason: {reason}" if reason else "")
        )
        await self._log(
            interaction.guild,
            title="Member timed out",
            color=discord.Color.orange(),
            fields={
                "Member": f"{member} (`{member.id}`)",
                "Moderator": f"{interaction.user} (`{interaction.user.id}`)",
                "Duration": format_timedelta(delta),
                "Reason": reason or "—",
            },
        )

    @app_commands.command(name="untimeout", description="Remove a member's timeout")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    async def untimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: Optional[str] = None,
    ) -> None:
        if not member.is_timed_out():
            await interaction.response.send_message(
                f"{member.mention} isn't timed out.", ephemeral=True
            )
            return
        await member.timeout(None, reason=reason)
        await interaction.response.send_message(f"{member.mention}'s timeout removed.")

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: Optional[str] = None,
    ) -> None:
        if not await self._guard_target(interaction, member):
            return
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 {member} was kicked.")
        await self._log(
            interaction.guild,
            title="Member kicked",
            color=discord.Color.orange(),
            fields={
                "Member": f"{member} (`{member.id}`)",
                "Moderator": str(interaction.user),
                "Reason": reason or "—",
            },
        )

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.describe(
        member="Member to ban",
        delete_days="Delete their messages from the last N days (0-7)",
        reason="Reason for the ban",
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        delete_days: app_commands.Range[int, 0, 7] = 0,
        reason: Optional[str] = None,
    ) -> None:
        if not await self._guard_target(interaction, member):
            return
        await interaction.guild.ban(
            member, reason=reason, delete_message_seconds=delete_days * 86400
        )
        await interaction.response.send_message(
            f"🔨 {member} was banned."
            + (f" Reason: {reason}" if reason else "")
        )
        await self._log(
            interaction.guild,
            title="Member banned",
            color=discord.Color.red(),
            fields={
                "Member": f"{member} (`{member.id}`)",
                "Moderator": str(interaction.user),
                "Reason": reason or "—",
            },
        )

    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: Optional[str] = None,
    ) -> None:
        try:
            user = await self.bot.fetch_user(int(user_id))
        except (ValueError, discord.NotFound):
            await interaction.response.send_message(
                f"No user found with ID `{user_id}`.", ephemeral=True
            )
            return
        try:
            await interaction.guild.unban(user, reason=reason)
        except discord.NotFound:
            await interaction.response.send_message(
                f"{user} isn't banned.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"{user} was unbanned.")

    @app_commands.command(name="warn", description="Warn a member (strike system)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(member="Member to warn", reason="Reason for the warning")
    async def warn(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ) -> None:
        if not await self._guard_target(interaction, member):
            return
        count = await self.bot.db.add_strike(
            interaction.guild.id, member.id, interaction.user.id, reason
        )
        consequence = await self._apply_strike_consequences(
            interaction.guild, member, count, reason
        )
        text = f"⚠️ {member.mention} warned — strike **{count}/{STRIKE_BAN_THRESHOLD}**. Reason: {reason}"
        if consequence:
            text += f"\n{consequence}"
        await interaction.response.send_message(text)
        await self._log(
            interaction.guild,
            title="Member warned",
            color=discord.Color.gold(),
            fields={
                "Member": f"{member} (`{member.id}`)",
                "Moderator": str(interaction.user),
                "Strike": f"{count}/{STRIKE_BAN_THRESHOLD}",
                "Reason": reason,
            },
        )
        try:
            dm = f"You were warned in **{interaction.guild.name}**: {reason} (strike {count}/{STRIKE_BAN_THRESHOLD})"
            if consequence:
                dm += f"\n{consequence}"
            await member.send(dm)
        except discord.HTTPException:
            pass

    @app_commands.command(name="warnings", description="List a member's strikes")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        strikes = await self.bot.db.list_strikes(interaction.guild.id, member.id)
        if not strikes:
            await interaction.response.send_message(
                f"{member.mention} has no strikes.", ephemeral=True
            )
            return
        lines = [
            f"**#{s['id']}** — {s['reason']} "
            f"(<t:{int(s['created_at'])}:R>, by <@{s['moderator_id']}>)"
            for s in strikes[:15]
        ]
        embed = discord.Embed(
            title=f"Strikes for {member} ({len(strikes)})",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Clear a member's strikes")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarnings(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        removed = await self.bot.db.clear_strikes(interaction.guild.id, member.id)
        await interaction.response.send_message(
            f"Cleared {removed} strike(s) for {member.mention}."
        )

    @app_commands.command(name="purge", description="Bulk-delete recent messages")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        amount="How many recent messages to scan/delete (1-500)",
        member="Only delete messages by this member",
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 500],
        member: Optional[discord.Member] = None,
    ) -> None:
        channel = interaction.channel
        problem = bot_permission_problem(channel, "manage_messages", "read_message_history")
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        kwargs: dict = {"bulk": True, "reason": f"Purged by {interaction.user}"}
        if member is not None:
            kwargs["check"] = lambda m: m.author.id == member.id
        deleted = await channel.purge(limit=amount, **kwargs)
        scope = f" from {member.mention}" if member else ""
        await interaction.followup.send(
            f"🧹 Deleted **{len(deleted)}** message(s){scope}. "
            "Messages older than 14 days can't be bulk-deleted."
        )
        await self._log(
            interaction.guild,
            title="Messages purged",
            color=discord.Color.dark_orange(),
            fields={
                "Channel": channel.mention,
                "Moderator": str(interaction.user),
                "Deleted": str(len(deleted)),
                "Filter": str(member) if member else "everyone",
            },
        )

    @app_commands.command(name="lock", description="Lock a channel (deny @everyone)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.abc.GuildChannel] = None,
    ) -> None:
        channel = channel or interaction.channel
        await self._set_lock(interaction, channel, locked=True)

    @app_commands.command(name="unlock", description="Unlock a channel")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.abc.GuildChannel] = None,
    ) -> None:
        channel = channel or interaction.channel
        await self._set_lock(interaction, channel, locked=False)

    async def _set_lock(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
        locked: bool,
    ) -> None:
        problem = bot_permission_problem(channel, "manage_roles")
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            kwargs = {"connect": False} if locked else {"connect": None}
        else:
            kwargs = {"send_messages": False} if locked else {"send_messages": None}
        await channel.set_permissions(
            interaction.guild.default_role,
            reason=f"{'Lock' if locked else 'Unlock'} by {interaction.user}",
            **kwargs,
        )
        verb = "🔒 Locked" if locked else "🔓 Unlocked"
        await interaction.response.send_message(f"{verb} {channel.mention}.")
        await self._log(
            interaction.guild,
            title=f"Channel {'locked' if locked else 'unlocked'}",
            color=discord.Color.red() if locked else discord.Color.green(),
            fields={"Channel": channel.mention, "Moderator": str(interaction.user)},
        )

    @app_commands.command(name="slowmode", description="Set channel slowmode delay")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.describe(seconds="Delay in seconds (0 to disable, max 21600)")
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: Optional[discord.abc.GuildChannel] = None,
    ) -> None:
        channel = channel or interaction.channel
        if not hasattr(channel, "slowmode_delay"):
            await interaction.response.send_message(
                "Slowmode only works on text/forum channels.", ephemeral=True
            )
            return
        await channel.edit(
            slowmode_delay=seconds, reason=f"Slowmode set by {interaction.user}"
        )
        await interaction.response.send_message(
            f"🐌 {channel.mention} slowmode set to **{seconds}s**."
            if seconds
            else f"{channel.mention} slowmode disabled."
        )

    # ---- anti-raid ----

    @app_commands.command(name="antiraid", description="Configure anti-raid protection")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        enabled="Turn anti-raid on or off",
        lock_channel="Channel to lock during a raid (default: rules/system/first text)",
        min_age_hours="Accounts younger than this get timed out during a raid",
        join_threshold="Joins within the window that count as a raid",
        window_seconds="Join-surge detection window in seconds",
    )
    async def antiraid(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        lock_channel: Optional[discord.TextChannel] = None,
        min_age_hours: app_commands.Range[int, 1, 720] = 24,
        join_threshold: app_commands.Range[int, 3, 50] = 6,
        window_seconds: app_commands.Range[int, 5, 600] = 30,
    ) -> None:
        gid = interaction.guild.id
        await self.bot.db.set_setting(gid, "antiraid_enabled", enabled)
        await self.bot.db.set_setting(gid, "antiraid_min_age_hours", min_age_hours)
        await self.bot.db.set_setting(gid, "antiraid_join_threshold", join_threshold)
        await self.bot.db.set_setting(gid, "antiraid_window_seconds", window_seconds)
        await self.bot.db.set_setting(
            gid, "antiraid_lock_channel_id", lock_channel.id if lock_channel else None
        )
        state = "🛡️ **enabled**" if enabled else "**disabled**"
        await interaction.response.send_message(
            f"Anti-raid {state}. Join threshold: {join_threshold}/{window_seconds}s, "
            f"min account age: {min_age_hours}h, "
            f"lock channel: {lock_channel.mention if lock_channel else 'auto'}."
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        db = self.bot.db
        if not await db.get_setting(guild.id, "antiraid_enabled", False):
            return
        state = self._raids.setdefault(guild.id, RaidState())
        now = time.time()
        window = await db.get_setting(
            guild.id, "antiraid_window_seconds", ANTIRAID_DEFAULTS["antiraid_window_seconds"]
        )
        cooldown = await db.get_setting(
            guild.id,
            "antiraid_cooldown_seconds",
            ANTIRAID_DEFAULTS["antiraid_cooldown_seconds"],
        )
        threshold = await db.get_setting(
            guild.id, "antiraid_join_threshold", ANTIRAID_DEFAULTS["antiraid_join_threshold"]
        )
        state.joins.append(now)
        while state.joins and now - state.joins[0] > window:
            state.joins.popleft()

        raiding = now < state.active_until
        if not raiding and len(state.joins) >= threshold:
            raiding = True
            state.active_until = now + cooldown
            await self._enter_raid_mode(guild, state)
        if raiding:
            state.active_until = now + cooldown
            min_age_h = await db.get_setting(
                guild.id, "antiraid_min_age_hours", ANTIRAID_DEFAULTS["antiraid_min_age_hours"]
            )
            timeout_min = await db.get_setting(
                guild.id,
                "antiraid_timeout_minutes",
                ANTIRAID_DEFAULTS["antiraid_timeout_minutes"],
            )
            age = discord.utils.utcnow() - member.created_at
            if age < timedelta(hours=min_age_h):
                try:
                    await member.timeout(
                        timedelta(minutes=timeout_min),
                        reason=f"Anti-raid: account younger than {min_age_h}h during join surge",
                    )
                    await self._log(
                        guild,
                        title="Anti-raid timeout",
                        color=discord.Color.red(),
                        fields={
                            "Member": f"{member} (`{member.id}`)",
                            "Account age": f"{age.days}d {age.seconds // 3600}h",
                            "Duration": f"{timeout_min}m",
                        },
                    )
                except discord.Forbidden:
                    log.warning("Anti-raid timeout denied for %s in %s", member, guild)

    async def _enter_raid_mode(self, guild: discord.Guild, state: RaidState) -> None:
        channel_id = await self.bot.db.get_setting(guild.id, "antiraid_lock_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            channel = (
                guild.rules_channel
                or guild.system_channel
                or next(
                    (c for c in guild.text_channels if c.permissions_for(guild.me).manage_roles),
                    None,
                )
            )
        if channel is None:
            log.warning("Anti-raid triggered in %s but no lockable channel found", guild)
            return
        overwrite = channel.overwrites_for(guild.default_role)
        state.saved_send_messages = overwrite.send_messages
        state.locked_channel_id = channel.id
        try:
            await channel.set_permissions(
                guild.default_role, send_messages=False, reason="Anti-raid lockdown"
            )
        except discord.Forbidden:
            log.warning("Anti-raid lock denied on %s in %s", channel, guild)
            return
        await self._log(
            guild,
            title="Anti-raid lockdown engaged",
            color=discord.Color.red(),
            fields={"Locked channel": channel.mention},
        )
        if state.release_task is None or state.release_task.done():
            state.release_task = asyncio.create_task(self._release_raid(guild, state))

    async def _release_raid(self, guild: discord.Guild, state: RaidState) -> None:
        try:
            while True:
                wait = state.active_until - time.time()
                if wait <= 0:
                    break
                await asyncio.sleep(min(wait, 30))
            channel = guild.get_channel(state.locked_channel_id)
            if channel is not None:
                try:
                    await channel.set_permissions(
                        guild.default_role,
                        send_messages=state.saved_send_messages,
                        reason="Anti-raid lockdown lifted",
                    )
                    await self._log(
                        guild,
                        title="Anti-raid lockdown lifted",
                        color=discord.Color.green(),
                        fields={"Channel": channel.mention},
                    )
                except discord.Forbidden:
                    pass
        finally:
            state.locked_channel_id = None
            state.joins.clear()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
