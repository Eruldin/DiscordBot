"""AI-driven server scaffolder: /scaffold, /layout-backup, /layout-rollback."""
from __future__ import annotations

import asyncio
import json
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
from guildmaster.models.layout_schema import (
    ChannelDefinition,
    EditOperation,
    LayoutEdit,
    sanitize_channel_name,
)
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


def describe_layout_for_llm(guild: discord.Guild) -> str:
    """Compact JSON of the current channel tree for the LLM's context.

    Permission overwrites are reduced to role names — raw bitfields are
    noise to the model.
    """
    def _channel_summary(ch: discord.abc.GuildChannel) -> dict:
        allowed = [
            e["target_name"]
            for e in serialize_overwrites(ch)
            if e["allow"] & discord.Permissions.view_channel.flag
        ]
        hidden = any(
            e["target_type"] == "role" and e["target_name"] == "@everyone"
            and e["deny"] & discord.Permissions.view_channel.flag
            for e in serialize_overwrites(ch)
        )
        return {
            "name": ch.name,
            "type": _CHANNEL_TYPE_NAMES.get(ch.type, "text"),
            "topic": getattr(ch, "topic", None),
            "slowmode": getattr(ch, "slowmode_delay", 0),
            "nsfw": getattr(ch, "nsfw", False),
            "hidden_from_everyone": hidden,
            "visible_to": allowed,
        }

    snapshot = {
        "guild": guild.name,
        "categories": [
            {
                "name": cat.name,
                "channels": [_channel_summary(ch) for ch in cat.channels],
            }
            for cat in guild.categories
        ],
        "uncategorized": [
            _channel_summary(ch)
            for ch in guild.channels
            if ch.category is None and not isinstance(ch, discord.CategoryChannel)
        ],
    }
    return json.dumps(snapshot, ensure_ascii=False)


def find_category(guild: discord.Guild, name: str) -> Optional[discord.CategoryChannel]:
    lowered = name.strip().lower()
    for cat in guild.categories:
        if cat.name.lower() == lowered:
            return cat
    return None


def find_channel(
    guild: discord.Guild, name: str, category_name: Optional[str] = None
) -> Optional[discord.abc.GuildChannel]:
    lowered = name.strip().lower()
    matches = [
        c
        for c in guild.channels
        if not isinstance(c, discord.CategoryChannel) and c.name.lower() == lowered
    ]
    if category_name:
        cat_lower = category_name.strip().lower()
        for c in matches:
            if c.category is not None and c.category.name.lower() == cat_lower:
                return c
    return matches[0] if matches else None


def _describe_op(e: EditOperation) -> str:
    """One-line human-readable description of an EditOperation."""
    if e.action == "create_category":
        inner = f" with {len(e.channels)} channel(s)" if e.channels else ""
        return f"create category **{e.category_name or e.new_name}**{inner}"
    if e.action == "rename_category":
        return f"rename category **{e.category_name}** → **{e.new_name}**"
    if e.action == "delete_category":
        return f"delete category **{e.category_name}**{' (+children)' if e.delete_children else ''}"
    if e.action == "create_channel":
        where = f" under **{e.category_name}**" if e.category_name else ""
        return f"create `#{(e.channel.name if e.channel else e.channel_name)}`{where}"
    if e.action == "rename_channel":
        return f"rename `#{e.channel_name}` → `#{e.new_name}`"
    if e.action == "move_channel":
        return f"move `#{e.channel_name}` → **{e.move_to_category or 'top level'}**"
    if e.action == "update_channel":
        bits = []
        if e.new_name:
            bits.append(f"name→`#{e.new_name}`")
        if e.topic is not None:
            bits.append("topic")
        if e.slowmode is not None:
            bits.append(f"slowmode={e.slowmode}s")
        if e.nsfw is not None:
            bits.append(f"nsfw={e.nsfw}")
        if e.make_private_for is not None:
            bits.append("private→" + ",".join(e.make_private_for))
        if e.make_public:
            bits.append("public")
        return f"update `#{e.channel_name}` ({', '.join(bits) or 'no-op'})"
    if e.action == "delete_channel":
        return f"delete `#{e.channel_name}`"
    return e.action


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

    # ---- /layout-edit: targeted edits to the existing layout ----

    @app_commands.command(
        name="layout-edit",
        description="Edit existing channels/categories from a natural-language request",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        prompt="What to change, e.g. 'rename #genel to #chat, move it under Community, delete #old'"
    )
    async def layout_edit(
        self,
        interaction: discord.Interaction,
        prompt: str,
    ) -> None:
        guild = interaction.guild
        perms = guild.me.guild_permissions
        if not perms.manage_channels:
            await interaction.response.send_message(
                "I need **Manage Channels** (and **Manage Roles** for new roles/gating).",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            edit = await self.layout_gen.generate_edit(
                prompt, describe_layout_for_llm(guild)
            )
        except LayoutGenerationError as exc:
            await interaction.followup.send(f"❌ Couldn't plan the edit: {exc}")
            return

        if not edit.operations and not edit.roles:
            await interaction.followup.send(f"ℹ️ {edit.summary} — nothing to change.")
            return

        snapshot = capture_layout(guild)
        backup_id = await self.bot.db.save_layout_backup(guild.id, snapshot)

        op_lines = [f"• {_describe_op(o)}" for o in edit.operations][:18]
        plan = discord.Embed(
            title="Planned layout edits",
            description=edit.summary[:400] + "\n\n" + "\n".join(op_lines),
            color=discord.Color.blurple(),
        )
        plan.set_footer(text=f"Backup #{backup_id} saved — /layout-rollback restores it")
        progress_msg = await interaction.followup.send(embed=plan)

        protected = self._protected_channel_ids(guild, interaction.channel_id)
        last_edit = 0.0

        async def progress(title: str, op: QueuedOp, done: int, total: int) -> None:
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

        applied, errors = await self.execute_edit(
            guild, edit, protected_ids=protected, progress=progress
        )

        color = discord.Color.green() if not errors else discord.Color.orange()
        done = discord.Embed(
            title="Edit complete" if not errors else "Edit complete (with errors)",
            color=color,
            description=(
                f"**{edit.summary[:300]}**\n\n"
                f"Applied {len(applied)} operation(s). "
                + (f"{len(errors)} failed:\n" + "\n".join(f"• {e}" for e in errors[:10]) if errors else "")
            ),
        )
        done.set_footer(text=f"Not happy? /layout-rollback restores backup #{backup_id}")
        try:
            await progress_msg.edit(embed=done)
        except discord.HTTPException:
            await interaction.followup.send(embed=done)
        await self._log(
            guild,
            title="Layout edited",
            color=color,
            fields={
                "Moderator": str(interaction.user),
                "Operations applied": str(len(applied)),
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

    async def execute_edit(
        self,
        guild: discord.Guild,
        edit: LayoutEdit,
        *,
        protected_ids: Optional[set] = None,
        progress=None,
    ) -> tuple[list[str], list[str]]:
        """Apply a LayoutEdit's ordered operations to the guild.

        Lookups happen inside each queued op so the LLM's ordering is
        honored (e.g. create_category before moving channels into it,
        delete before reusing a name). Returns (applied_labels, errors).
        """
        protected = set(protected_ids or set())
        log_channel_id = await self.bot.db.get_setting(guild.id, "log_channel_id")
        if log_channel_id:
            protected.add(int(log_channel_id))
        ops: list[QueuedOp] = []

        def _resolve_channel(e: EditOperation):
            ch = find_channel(guild, e.channel_name or "", e.category_name)
            if ch is None:
                raise RuntimeError(f"channel '#{e.channel_name}' not found")
            if ch.id in protected:
                raise RuntimeError(f"refusing to touch protected channel #{ch.name}")
            return ch

        def _resolve_category(e: EditOperation):
            cat = find_category(guild, e.category_name or "")
            if cat is None:
                raise RuntimeError(f"category '{e.category_name}' not found")
            if cat.id in protected:
                raise RuntimeError(f"refusing to touch protected category '{cat.name}'")
            return cat

        # Missing roles referenced by the plan are created first.
        for rdef in edit.roles:
            if find_role(guild, rdef.name) is None:
                kwargs: dict[str, Any] = {
                    "name": rdef.name,
                    "hoist": rdef.hoist,
                    "mentionable": rdef.mentionable,
                }
                if rdef.color:
                    kwargs["colour"] = discord.Colour(int(rdef.color.lstrip("#"), 16))
                ops.append(
                    QueuedOp(
                        label=f"Create role `@{rdef.name}`",
                        factory=lambda k=kwargs: guild.create_role(reason="GuildMaster edit", **k),
                    )
                )

        for e in edit.operations:
            if e.action == "create_category":
                cat_name = e.category_name or e.new_name or "New Category"
                holder: dict[str, Any] = {}

                async def make_cat(n=cat_name, h=holder):
                    h["channel"] = await guild.create_category(n, reason="GuildMaster edit")
                    return h["channel"]

                ops.append(QueuedOp(label=f"Create category **{cat_name}**", factory=make_cat))
                for cdef in e.channels:

                    async def make_ch(cd=cdef, h=holder):
                        cat = h.get("channel")
                        if cat is None:
                            raise RuntimeError("parent category failed to create")
                        return await self._create_channel_factory(guild, cd, cat)()

                    ops.append(QueuedOp(label=f"Create `#{cdef.name}`", factory=make_ch))

            elif e.action == "rename_category":

                async def rename_cat(e=e):
                    cat = _resolve_category(e)
                    return await cat.edit(name=e.new_name or cat.name, reason="GuildMaster edit")

                ops.append(
                    QueuedOp(
                        label=f"Rename category '{e.category_name}' → **{e.new_name}**",
                        factory=rename_cat,
                    )
                )

            elif e.action == "delete_category":

                async def delete_cat(e=e):
                    cat = _resolve_category(e)
                    if e.delete_children:
                        for ch in list(cat.channels):
                            if ch.id in protected:
                                raise RuntimeError(
                                    f"category '{cat.name}' contains protected #{ch.name}"
                                )
                        for ch in list(cat.channels):
                            await ch.delete(reason="GuildMaster edit")
                    return await cat.delete(reason="GuildMaster edit")

                ops.append(
                    QueuedOp(label=f"Delete category '{e.category_name}'", factory=delete_cat)
                )

            elif e.action == "create_channel":
                cdef = e.channel or ChannelDefinition(name=e.channel_name or e.new_name or "channel")

                async def create_ch(cd=cdef, e=e):
                    cat = find_category(guild, e.category_name) if e.category_name else None
                    if e.category_name and cat is None:
                        raise RuntimeError(f"category '{e.category_name}' not found")
                    return await self._create_channel_factory(guild, cd, cat)()

                ops.append(QueuedOp(label=f"Create `#{cdef.name}`", factory=create_ch))

            elif e.action == "rename_channel":

                async def rename_ch(e=e):
                    ch = _resolve_channel(e)
                    return await ch.edit(
                        name=sanitize_channel_name(e.new_name or ch.name),
                        reason="GuildMaster edit",
                    )

                ops.append(
                    QueuedOp(
                        label=f"Rename `#{e.channel_name}` → `#{e.new_name}`",
                        factory=rename_ch,
                    )
                )

            elif e.action == "move_channel":

                async def move_ch(e=e):
                    ch = _resolve_channel(e)
                    target = (e.move_to_category or "").strip()
                    if not target:
                        return await ch.move(beginning=True, category=None, reason="GuildMaster edit")
                    cat = find_category(guild, target)
                    if cat is None:
                        raise RuntimeError(f"target category '{target}' not found")
                    return await ch.move(end=True, category=cat, reason="GuildMaster edit")

                ops.append(
                    QueuedOp(
                        label=f"Move `#{e.channel_name}` → **{e.move_to_category or 'top level'}**",
                        factory=move_ch,
                    )
                )

            elif e.action == "update_channel":

                async def update_ch(e=e):
                    ch = _resolve_channel(e)
                    kwargs: dict[str, Any] = {}
                    if e.new_name:
                        kwargs["name"] = sanitize_channel_name(e.new_name)
                    if e.topic is not None and isinstance(
                        ch, (discord.TextChannel, discord.ForumChannel)
                    ):
                        kwargs["topic"] = e.topic
                    if e.slowmode is not None and hasattr(ch, "slowmode_delay"):
                        kwargs["slowmode_delay"] = max(0, e.slowmode)
                    if e.nsfw is not None:
                        kwargs["nsfw"] = e.nsfw
                    if kwargs:
                        await ch.edit(reason="GuildMaster edit", **kwargs)
                    if e.make_private_for is not None:
                        await ch.set_permissions(guild.default_role, view_channel=False)
                        for name in e.make_private_for:
                            role = find_role(guild, name)
                            if role is not None:
                                await ch.set_permissions(role, view_channel=True)
                    if e.make_public:
                        await ch.set_permissions(guild.default_role, overwrite=None)
                        # clear stale allow-only overwrites left by make_private_for
                        view_only = discord.Permissions.view_channel.flag
                        for target, ow in list(ch.overwrites.items()):
                            allow, deny = ow.pair()
                            if target != guild.default_role and allow.value == view_only and not deny.value:
                                await ch.set_permissions(target, overwrite=None)
                    return ch

                ops.append(QueuedOp(label=f"Update `#{e.channel_name}`", factory=update_ch))

            elif e.action == "delete_channel":

                async def delete_ch(e=e):
                    ch = _resolve_channel(e)
                    return await ch.delete(reason="GuildMaster edit")

                ops.append(QueuedOp(label=f"Delete `#{e.channel_name}`", factory=delete_ch))

        results = await self._run_queue(ops, "Applying layout edits", progress)
        applied, errors = [], []
        for op in results:
            if op.error:
                errors.append(f"{op.label}: {op.error}")
            else:
                applied.append(op.label)
        return applied, errors

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
