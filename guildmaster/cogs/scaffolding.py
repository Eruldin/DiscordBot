"""AI-driven server scaffolder: /scaffold, /layout-backup, /layout-rollback."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from guildmaster.core.llm_parser import (
    LayoutGenerationError,
    LayoutGenerator,
    create_layout_generator,
)
from guildmaster.models.layout_schema import ChannelDefinition
from guildmaster.utils.rate_limit import ChannelOpQueue, QueuedOp

log = logging.getLogger(__name__)

_CHANNEL_TYPE_NAMES = {
    discord.ChannelType.text: "text",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.stage_voice: "stage",
    discord.ChannelType.forum: "forum",
    discord.ChannelType.news: "news",
}


def serialize_overwrites(channel: discord.abc.GuildChannel) -> list[dict]:
    out = []
    for target, ow in channel.overwrites.items():
        allow, deny = ow.pair()
        if not allow.value and not deny.value:
            continue
        out.append(
            {
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "target_id": target.id,
                "target_name": getattr(target, "name", str(target)),
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return out


def capture_layout(guild: discord.Guild) -> dict:
    """Snapshot the guild's channel tree for rollback."""
    snapshot: dict[str, Any] = {
        "guild_id": guild.id,
        "captured_at": time.time(),
        "categories": [],
        "uncategorized": [],
    }
    for category in guild.categories:
        cat = {
            "name": category.name,
            "overwrites": serialize_overwrites(category),
            "channels": [],
        }
        for ch in category.channels:
            cat["channels"].append(
                {
                    "name": ch.name,
                    "type": _CHANNEL_TYPE_NAMES.get(ch.type, "text"),
                    "topic": getattr(ch, "topic", None),
                    "slowmode": getattr(ch, "slowmode_delay", 0),
                    "nsfw": getattr(ch, "nsfw", False),
                    "overwrites": serialize_overwrites(ch),
                }
            )
        snapshot["categories"].append(cat)
    for ch in guild.channels:
        if ch.category is None and not isinstance(ch, discord.CategoryChannel):
            snapshot["uncategorized"].append(
                {
                    "name": ch.name,
                    "type": _CHANNEL_TYPE_NAMES.get(ch.type, "text"),
                    "topic": getattr(ch, "topic", None),
                    "overwrites": serialize_overwrites(ch),
                }
            )
    return snapshot


def find_role(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    lowered = name.lstrip("@").lower()
    if lowered == "everyone":
        return guild.default_role
    for role in guild.roles:
        if role.name.lower() == lowered:
            return role
    return None


def build_channel_overwrites(
    guild: discord.Guild, channel_def: ChannelDefinition
) -> dict:
    """roles_allowed restricts visibility; roles_denied is always applied."""
    overwrites: dict[Any, discord.PermissionOverwrite] = {}
    allowed_names = {n.lower() for n in channel_def.roles_allowed}
    if channel_def.is_restricted and allowed_names != {"@everyone"}:
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
    for name in channel_def.roles_allowed:
        role = find_role(guild, name)
        if role is not None and role != guild.default_role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, connect=True, speak=True
            )
    for name in channel_def.roles_denied:
        role = find_role(guild, name)
        if role is not None:
            overwrites[role] = discord.PermissionOverwrite(view_channel=False)
    return overwrites


def build_category_overwrites(guild: discord.Guild, category_def) -> dict:
    """Hide a whole category from @everyone when every channel inside is gated."""
    channels = category_def.channels
    if not channels or not all(c.is_restricted for c in channels):
        return {}
    overwrites: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False)
    }
    for c in channels:
        for name in c.roles_allowed:
            role = find_role(guild, name)
            if role is not None and role != guild.default_role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, connect=True
                )
    return overwrites


def overwrites_from_snapshot(guild: discord.Guild, saved: list[dict]) -> dict:
    out: dict[Any, discord.PermissionOverwrite] = {}
    for entry in saved:
        target: Any = None
        if entry["target_type"] == "role":
            target = guild.get_role(entry["target_id"]) or find_role(
                guild, entry["target_name"]
            )
        elif entry["target_type"] == "member":
            target = guild.get_member(entry["target_id"])
        if target is None:
            continue
        out[target] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(entry["allow"]), discord.Permissions(entry["deny"])
        )
    return out


class Scaffolding(commands.Cog):
    def __init__(self, bot: commands.Bot, layout_gen: Optional[LayoutGenerator] = None) -> None:
        self.bot = bot
        self._layout_gen = layout_gen

    @property
    def layout_gen(self):
        if self._layout_gen is None:
            self._layout_gen = create_layout_generator(self.bot.settings)
        return self._layout_gen

    # ---- helpers ----

    def _protected_channel_ids(self, guild: discord.Guild, interaction_channel_id: int) -> set:
        protected = {interaction_channel_id}
        # community-required channels can't be deleted anyway, but skip them deliberately
        for ch in (guild.rules_channel, guild.public_updates_channel):
            if ch is not None:
                protected.add(ch.id)
        return protected

    async def _log(self, guild: discord.Guild, **kwargs) -> None:
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is not None:
            await logging_cog.log_event(guild, **kwargs)

    def _create_channel_factory(self, guild: discord.Guild, cdef: ChannelDefinition, category):
        kwargs: dict[str, Any] = {"category": category}
        overwrites = build_channel_overwrites(guild, cdef)
        if overwrites:
            kwargs["overwrites"] = overwrites
        if cdef.type == "text":
            kwargs.update(topic=cdef.topic, slowmode_delay=cdef.slowmode or None, nsfw=cdef.nsfw)
            factory = guild.create_text_channel
        elif cdef.type == "voice":
            factory = guild.create_voice_channel
        elif cdef.type == "stage":
            kwargs["topic"] = cdef.topic
            factory = guild.create_stage_channel
        elif cdef.type == "forum":
            kwargs.update(topic=cdef.topic, slowmode_delay=cdef.slowmode or None, nsfw=cdef.nsfw)
            factory = guild.create_forum
        else:
            raise ValueError(f"Unknown channel type {cdef.type!r}")

        async def run() -> discord.abc.GuildChannel:
            return await factory(cdef.name, reason="GuildMaster scaffold", **kwargs)

        return run

    async def _run_queue(self, ops: list[QueuedOp], title: str, progress=None) -> list[QueuedOp]:
        """Run ops through the throttled queue.

        ``progress`` is an optional async callable (title, op, done, total)
        invoked after every operation — Discord embeds and the web panel each
        wrap it with their own rendering.
        """
        queue = ChannelOpQueue(min_delay=1.25)
        if progress is not None:
            async def on_progress(op: QueuedOp, done: int, total: int) -> None:
                await progress(title, op, done, total)
            queue.on_progress = on_progress
        await queue.start()
        for op in ops:
            await queue.submit(op)
        await queue.join()
        await queue.stop()
        return ops

    # ---- /scaffold ----

    @app_commands.command(
        name="scaffold",
        description="Design and build a server layout from a natural-language prompt",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        prompt="Describe the community/server you want (language, theme, gated areas)",
        wipe_existing="Delete the current channel layout first",
    )
    async def scaffold(
        self,
        interaction: discord.Interaction,
        prompt: str,
        wipe_existing: bool = False,
    ) -> None:
        guild = interaction.guild
        perms = guild.me.guild_permissions
        if not (perms.manage_channels and perms.manage_roles):
            await interaction.response.send_message(
                "I need **Manage Channels** and **Manage Roles** to scaffold a server.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            layout = await self.layout_gen.generate(prompt)
        except LayoutGenerationError as exc:
            await interaction.followup.send(f"❌ Couldn't design a layout: {exc}")
            return

        if not layout.categories or layout.channel_count() == 0:
            await interaction.followup.send(
                "❌ The generated layout had no channels. Try a more specific prompt."
            )
            return
        if guild.me.top_role != max(guild.roles) and any(
            c.is_restricted for cat in layout.categories for c in cat.channels
        ):
            log.info(
                "Bot role is not the top role in %s; gated channels may fail",
                guild,
            )

        # Snapshot BEFORE any mutation so /layout-rollback can undo this run.
        snapshot = capture_layout(guild)
        backup_id = await self.bot.db.save_layout_backup(guild.id, snapshot)

        plan_lines = [
            f"**{cat.category_name}** — " + ", ".join(f"`#{c.name}`" for c in cat.channels)
            for cat in layout.categories
        ]
        plan = discord.Embed(
            title="Generated server plan",
            description=layout.server_summary[:400] + "\n\n" + "\n".join(plan_lines)[:3500],
            color=discord.Color.blurple(),
        )
        plan.set_footer(text=f"Backup #{backup_id} saved — /layout-rollback restores it")
        progress_msg = await interaction.followup.send(embed=plan)

        protected = self._protected_channel_ids(guild, interaction.channel_id)
        last_edit = 0.0

        async def progress(title: str, op: QueuedOp, done: int, total: int) -> None:
            nonlocal last_edit
            if time.monotonic() - last_edit < 1.0 and done < total:
                return  # don't spam message edits — Discord rate-limits them
            last_edit = time.monotonic()
            status = "✅" if op.error is None else "❌"
            embed = discord.Embed(
                title=title,
                description=f"[{done}/{total}] {status} {op.label}",
                color=discord.Color.blurple(),
            )
            try:
                await progress_msg.edit(embed=embed)
            except discord.HTTPException:
                pass

        created, errors = await self.execute_layout(
            guild,
            layout,
            wipe_existing=wipe_existing,
            protected_ids=protected,
            progress=progress,
        )

        color = discord.Color.green() if not errors else discord.Color.orange()
        done = discord.Embed(
            title="Scaffold complete" if not errors else "Scaffold complete (with errors)",
            color=color,
            description=(
                f"**{layout.server_summary[:300]}**\n\n"
                f"Created {len(created)} item(s). "
                + (f"{len(errors)} operation(s) failed:\n" + "\n".join(f"• {e}" for e in errors[:10]) if errors else "")
            ),
        )
        done.set_footer(text=f"Not happy? /layout-rollback restores backup #{backup_id}")
        try:
            await progress_msg.edit(embed=done)
        except discord.HTTPException:
            await interaction.followup.send(embed=done)
        await self._log(
            guild,
            title="Server scaffolded",
            color=color,
            fields={
                "Moderator": str(interaction.user),
                "Channels created": str(len(created)),
                "Errors": str(len(errors)),
                "Backup": f"#{backup_id}",
            },
        )

    # ---- execution engine (shared by /scaffold and the web panel) ----

    async def execute_layout(
        self,
        guild: discord.Guild,
        layout,
        *,
        wipe_existing: bool = False,
        protected_ids: Optional[set] = None,
        progress=None,
    ) -> tuple[list[str], list[str]]:
        """Apply a generated layout to a guild: wipe (optional) -> roles -> channels.

        ``protected_ids`` are channel IDs that must never be deleted (the
        invoking channel, the audit-log channel, community channels). The
        log channel is always added here. Returns (created_names, errors).
        """
        protected = set(protected_ids or set())
        log_channel_id = await self.bot.db.get_setting(guild.id, "log_channel_id")
        if log_channel_id:
            protected.add(int(log_channel_id))
        errors: list[str] = []
        created: list[str] = []

        # Phase A: wipe
        if wipe_existing:
            delete_ops = []
            for ch in guild.channels:
                if ch.id in protected:
                    continue
                delete_ops.append(
                    QueuedOp(
                        label=f"Delete `#{ch.name}`",
                        factory=lambda c=ch: c.delete(reason="GuildMaster wipe"),
                    )
                )
            results = await self._run_queue(delete_ops, "Wiping existing channels", progress)
            errors.extend(f"{op.label}: {op.error}" for op in results if op.error)

        # Phase B: roles referenced by the layout that don't exist yet
        role_ops = []
        for rdef in layout.roles:
            if find_role(guild, rdef.name) is None:
                kwargs: dict[str, Any] = {
                    "name": rdef.name,
                    "hoist": rdef.hoist,
                    "mentionable": rdef.mentionable,
                }
                if rdef.color:
                    kwargs["colour"] = discord.Colour(int(rdef.color.lstrip("#"), 16))
                role_ops.append(
                    QueuedOp(
                        label=f"Create role `@{rdef.name}`",
                        factory=lambda k=kwargs: guild.create_role(reason="GuildMaster scaffold", **k),
                    )
                )
        if role_ops:
            results = await self._run_queue(role_ops, "Creating roles", progress)
            errors.extend(f"{op.label}: {op.error}" for op in results if op.error)
            await asyncio.sleep(0)  # let guild role cache settle

        # Phase C: categories + channels
        channel_ops: list[QueuedOp] = []
        for cat_def in layout.categories:
            cat_overwrites = build_category_overwrites(guild, cat_def)
            cat_kwargs: dict[str, Any] = {"reason": "GuildMaster scaffold"}
            if cat_overwrites:
                cat_kwargs["overwrites"] = cat_overwrites
            holder: dict[str, Any] = {}

            async def make_category(d=cat_def, k=cat_kwargs, h=holder):
                h["channel"] = await guild.create_category(d.category_name, **k)
                return h["channel"]

            channel_ops.append(
                QueuedOp(label=f"Create category **{cat_def.category_name}**", factory=make_category)
            )
            for cdef in cat_def.channels:

                async def make_channel(cd=cdef, h=holder):
                    category = h.get("channel")
                    if category is None:
                        raise RuntimeError("parent category failed to create")
                    return await self._create_channel_factory(guild, cd, category)()

                channel_ops.append(
                    QueuedOp(label=f"Create `#{cdef.name}`", factory=make_channel)
                )

        results = await self._run_queue(channel_ops, "Building server layout", progress)
        for op in results:
            if op.error:
                errors.append(f"{op.label}: {op.error}")
            elif name := getattr(op.result, "name", None):
                created.append(name)
        return created, errors

    async def execute_rollback(
        self,
        guild: discord.Guild,
        snapshot: dict,
        *,
        protected_ids: Optional[set] = None,
        progress=None,
    ) -> list[QueuedOp]:
        """Diff a snapshot vs. the live layout and reconcile via the queue:
        delete channels not in the snapshot, recreate missing ones.
        Returns the executed ops (check ``op.error`` for failures)."""
        protected = set(protected_ids or set())
        log_channel_id = await self.bot.db.get_setting(guild.id, "log_channel_id")
        if log_channel_id:
            protected.add(int(log_channel_id))

        wanted_cats = {c["name"] for c in snapshot["categories"]}
        wanted_channels = {
            (ch["name"], ch["type"], cat["name"])
            for cat in snapshot["categories"]
            for ch in cat["channels"]
        } | {
            (ch["name"], ch["type"], None) for ch in snapshot["uncategorized"]
        }

        ops: list[QueuedOp] = []
        # Delete channels that aren't in the snapshot (non-categories first)
        existing = sorted(
            guild.channels,
            key=lambda c: isinstance(c, discord.CategoryChannel),
        )
        for ch in existing:
            if ch.id in protected:
                continue
            if isinstance(ch, discord.CategoryChannel):
                wanted = ch.name in wanted_cats
            else:
                type_name = _CHANNEL_TYPE_NAMES.get(ch.type, "text")
                parent = ch.category.name if ch.category else None
                wanted = (ch.name, type_name, parent) in wanted_channels
            if not wanted:
                ops.append(
                    QueuedOp(
                        label=f"Delete `#{ch.name}`",
                        factory=lambda c=ch: c.delete(reason="GuildMaster rollback"),
                    )
                )

        # Create missing categories, then missing channels under them
        cat_by_name = {c.name: c for c in guild.categories}
        for cat_snap in snapshot["categories"]:
            holder: dict[str, Any] = {"channel": cat_by_name.get(cat_snap["name"])}

            async def make_cat(s=cat_snap, h=holder):
                if h["channel"] is None:
                    h["channel"] = await guild.create_category(
                        s["name"],
                        overwrites=overwrites_from_snapshot(guild, s["overwrites"]),
                        reason="GuildMaster rollback",
                    )
                return h["channel"]

            if holder["channel"] is None:
                ops.append(
                    QueuedOp(label=f"Create category **{cat_snap['name']}**", factory=make_cat)
                )
            existing_children = (
                {c.name for c in holder["channel"].channels} if holder["channel"] else set()
            )
            for ch_snap in cat_snap["channels"]:
                if ch_snap["name"] in existing_children:
                    continue
                cdef = ChannelDefinition(
                    name=ch_snap["name"],
                    type=ch_snap["type"] if ch_snap["type"] in ("text", "voice", "stage", "forum") else "text",
                    topic=ch_snap.get("topic"),
                    slowmode=ch_snap.get("slowmode") or 0,
                    nsfw=ch_snap.get("nsfw") or False,
                )

                async def make_ch(cd=cdef, h=holder, s=cat_snap):
                    if h.get("channel") is None:
                        h["channel"] = await guild.create_category(
                            s["name"],
                            overwrites=overwrites_from_snapshot(guild, s["overwrites"]),
                            reason="GuildMaster rollback",
                        )
                    return await self._create_channel_factory(guild, cd, h["channel"])()

                ops.append(QueuedOp(label=f"Create `#{ch_snap['name']}`", factory=make_ch))

        return await self._run_queue(ops, "Restoring layout", progress)

    # ---- backups ----

    @app_commands.command(name="layout-backup", description="Snapshot the current channel layout")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def layout_backup(self, interaction: discord.Interaction) -> None:
        snapshot = capture_layout(interaction.guild)
        backup_id = await self.bot.db.save_layout_backup(interaction.guild.id, snapshot)
        n_channels = sum(len(c["channels"]) for c in snapshot["categories"]) + len(
            snapshot["uncategorized"]
        )
        await interaction.response.send_message(
            f"💾 Backup **#{backup_id}** saved — "
            f"{len(snapshot['categories'])} categories, {n_channels} channels. "
            "Restore with `/layout-rollback`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="layout-rollback", description="Restore the most recent layout backup"
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def layout_rollback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        perms = guild.me.guild_permissions
        if not (perms.manage_channels and perms.manage_roles):
            await interaction.response.send_message(
                "I need **Manage Channels** and **Manage Roles** for a rollback.",
                ephemeral=True,
            )
            return
        backup = await self.bot.db.latest_backup(guild.id)
        if backup is None:
            await interaction.response.send_message(
                "No layout backup exists for this server yet.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        progress_msg = await interaction.followup.send(
            embed=discord.Embed(
                title=f"Rolling back to backup #{backup['id']}",
                description="Reconciling channels…",
                color=discord.Color.blurple(),
            )
        )
        snapshot = backup["snapshot"]
        protected = self._protected_channel_ids(guild, interaction.channel_id)
        last_edit = 0.0

        async def rollback_progress(title: str, op: QueuedOp, done: int, total: int) -> None:
            nonlocal last_edit
            if time.monotonic() - last_edit < 1.0 and done < total:
                return
            last_edit = time.monotonic()
            status = "✅" if op.error is None else "❌"
            embed = discord.Embed(
                title=title,
                description=f"[{done}/{total}] {status} {op.label}",
                color=discord.Color.blurple(),
            )
            try:
                await progress_msg.edit(embed=embed)
            except discord.HTTPException:
                pass

        results = await self.execute_rollback(
            guild, snapshot, protected_ids=protected, progress=rollback_progress
        )
        failed = [f"{op.label}: {op.error}" for op in results if op.error]
        embed = discord.Embed(
            title="Rollback complete" if not failed else "Rollback complete (with errors)",
            color=discord.Color.green() if not failed else discord.Color.orange(),
            description=(
                f"Restored from backup **#{backup['id']}** "
                f"({len(results) - len(failed)}/{len(results)} operations succeeded)."
                + ("\n" + "\n".join(f"• {e}" for e in failed[:10]) if failed else "")
            ),
        )
        try:
            await progress_msg.edit(embed=embed)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Scaffolding(bot))
