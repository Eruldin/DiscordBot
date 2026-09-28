"""FastAPI control panel — served inside the bot's event loop via uvicorn.

Exposes guild introspection, scaffold preview/apply, channel & role ops,
moderation actions, and layout backups under /api (token-protected).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

import discord
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from guildmaster.cogs.scaffolding import capture_layout, describe_layout_for_llm
from guildmaster.core.llm_parser import LayoutGenerationError, create_layout_generator
from guildmaster.models.layout_schema import (
    LayoutEdit,
    ServerLayout,
    sanitize_channel_name,
)
from guildmaster.utils.duration import format_timedelta, parse_duration

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ---- request bodies ----


class PreviewBody(BaseModel):
    prompt: str


class ApplyBody(BaseModel):
    layout: dict
    wipe_existing: bool = False


class EditApplyBody(BaseModel):
    edit: LayoutEdit


class ChannelCreateBody(BaseModel):
    name: str
    type: str = "text"  # text | voice | category | forum
    category_id: Optional[int] = None
    topic: Optional[str] = None


class SlowmodeBody(BaseModel):
    seconds: int


class RoleCreateBody(BaseModel):
    name: str
    color: Optional[str] = None


class TimeoutBody(BaseModel):
    duration: str = "10m"
    reason: Optional[str] = None


class BanBody(BaseModel):
    delete_days: int = 0
    reason: Optional[str] = None


class ReasonBody(BaseModel):
    reason: Optional[str] = None


class WarnBody(BaseModel):
    reason: str


class LogChannelBody(BaseModel):
    channel_id: Optional[int]


class PurgeBody(BaseModel):
    amount: int = 50
    member_id: Optional[int] = None


def _type_name(ch: discord.abc.GuildChannel) -> str:
    return getattr(ch.type, "name", str(ch.type))


def _serialize_channel(ch: discord.abc.GuildChannel) -> dict:
    return {
        "id": str(ch.id),
        "name": ch.name,
        "type": _type_name(ch),
        "category_id": str(ch.category_id) if getattr(ch, "category_id", None) else None,
        "topic": getattr(ch, "topic", None),
        "slowmode": getattr(ch, "slowmode_delay", 0),
        "position": getattr(ch, "position", 0),
    }


def _serialize_role(r: discord.Role) -> dict:
    return {"id": str(r.id), "name": r.name, "position": r.position, "color": str(r.color)}


def _serialize_member(m: discord.Member) -> dict:
    return {
        "id": str(m.id),
        "name": m.display_name,
        "tag": str(m),
        "bot": m.bot,
        "top_role": m.top_role.name if m.top_role else None,
        "timed_out": bool(getattr(m, "timed_out_until", None)),
        "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        "account_age_days": (
            (discord.utils.utcnow() - m.created_at).days if m.created_at else None
        ),
    }


def create_app(bot: Any, layout_gen: Optional[Any] = None) -> FastAPI:
    app = FastAPI(title="GuildMaster Panel", docs_url=None, redoc_url=None)
    panel_token: Optional[str] = bot.settings.panel_token
    jobs: dict[str, dict] = {}
    _layout_gen = layout_gen

    def layout_generator() -> Any:
        nonlocal _layout_gen
        if _layout_gen is None:
            _layout_gen = create_layout_generator(bot.settings)
        return _layout_gen

    async def auth(
        x_panel_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> None:
        if not panel_token:
            return
        if x_panel_token != panel_token and token != panel_token:
            raise HTTPException(401, "Invalid panel token")

    def guild_or_404(gid: int) -> discord.Guild:
        guild = bot.get_guild(gid)
        if guild is None:
            raise HTTPException(404, "Guild not found or bot not ready")
        return guild

    async def member_or_404(guild: discord.Guild, uid: int) -> discord.Member:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                raise HTTPException(404, "Member not found")
        return member

    def mod_guard(guild: discord.Guild, member: discord.Member) -> None:
        if member == guild.owner:
            raise HTTPException(400, "Can't moderate the server owner")
        if member == guild.me:
            raise HTTPException(400, "Can't moderate the bot")
        if guild.me.top_role <= member.top_role:
            raise HTTPException(403, "Bot's top role must be above the target's top role")

    # ---- status & introspection ----

    @app.get("/api/status")
    async def status(_: Any = Depends(auth)) -> dict:
        return {
            "online": bot.is_ready(),
            "user": str(bot.user) if bot.user else None,
            "latency_ms": round(bot.latency * 1000) if bot.is_ready() else None,
            "guilds": len(bot.guilds),
        }

    @app.get("/api/guilds")
    async def guilds(_: Any = Depends(auth)) -> list[dict]:
        return [
            {
                "id": str(g.id),
                "name": g.name,
                "member_count": g.member_count,
                "channels": len(g.channels),
                "roles": len(g.roles),
            }
            for g in bot.guilds
        ]

    @app.get("/api/guilds/{gid}/channels")
    async def channels(gid: int, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        log_channel_id = await bot.db.get_setting(gid, "log_channel_id")
        return {
            "log_channel_id": str(log_channel_id) if log_channel_id else None,
            "categories": [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "channels": [_serialize_channel(ch) for ch in c.channels],
                }
                for c in guild.categories
            ],
            "uncategorized": [
                _serialize_channel(ch)
                for ch in guild.channels
                if ch.category is None and not isinstance(ch, discord.CategoryChannel)
            ],
        }

    @app.get("/api/guilds/{gid}/roles")
    async def roles(gid: int, _: Any = Depends(auth)) -> list[dict]:
        guild = guild_or_404(gid)
        return [_serialize_role(r) for r in guild.roles]

    @app.get("/api/guilds/{gid}/members")
    async def members(gid: int, limit: int = 200, _: Any = Depends(auth)) -> list[dict]:
        guild = guild_or_404(gid)
        out = []
        async for m in guild.fetch_members(limit=min(limit, 1000)):
            out.append(_serialize_member(m))
        return out

    # ---- scaffold ----

    @app.post("/api/guilds/{gid}/scaffold/preview")
    async def scaffold_preview(gid: int, body: PreviewBody, _: Any = Depends(auth)) -> dict:
        guild_or_404(gid)
        try:
            layout = await layout_generator().generate(body.prompt)
        except LayoutGenerationError as exc:
            raise HTTPException(502, f"Layout generation failed: {exc}")
        return layout.model_dump()

    @app.post("/api/guilds/{gid}/scaffold/apply")
    async def scaffold_apply(gid: int, body: ApplyBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        perms = guild.me.guild_permissions
        if not (perms.manage_channels and perms.manage_roles):
            raise HTTPException(403, "Bot needs Manage Channels + Manage Roles")
        layout = ServerLayout.model_validate(body.layout)
        if not layout.categories or layout.channel_count() == 0:
            raise HTTPException(400, "Layout has no channels")
        job_id = uuid.uuid4().hex[:12]
        job = jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "phase": "starting",
            "done": 0,
            "total": 0,
            "current": "",
            "created": [],
            "errors": [],
            "backup_id": None,
        }

        async def run() -> None:
            try:
                backup_id = await bot.db.save_layout_backup(gid, capture_layout(guild))
                job["backup_id"] = backup_id
                cog = bot.get_cog("Scaffolding")

                async def progress(title: str, op: Any, done: int, total: int) -> None:
                    job.update(phase=title, done=done, total=total, current=op.label)

                created, errors = await cog.execute_layout(
                    guild,
                    layout,
                    wipe_existing=body.wipe_existing,
                    progress=progress,
                )
                job.update(created=created, errors=errors, status="done")
            except Exception as exc:
                log.exception("scaffold job %s failed", job_id)
                job.update(status="error")
                job["errors"].append(str(exc))

        asyncio.get_running_loop().create_task(run())
        return {"job_id": job_id, "backup": "pre-apply snapshot is taken inside the job"}

    @app.get("/api/scaffold/jobs/{job_id}")
    async def scaffold_job(job_id: str, _: Any = Depends(auth)) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Unknown job")
        return job

    # ---- layout edit (targeted prompt-driven changes) ----

    @app.post("/api/guilds/{gid}/edit/preview")
    async def edit_preview(gid: int, body: PreviewBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        try:
            edit = await layout_generator().generate_edit(
                body.prompt, describe_layout_for_llm(guild)
            )
        except LayoutGenerationError as exc:
            raise HTTPException(502, f"Edit plan generation failed: {exc}")
        return edit.model_dump()

    @app.post("/api/guilds/{gid}/edit/apply")
    async def edit_apply(gid: int, body: EditApplyBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        perms = guild.me.guild_permissions
        if not perms.manage_channels:
            raise HTTPException(403, "Bot needs Manage Channels")
        edit = body.edit
        if not edit.operations and not edit.roles:
            raise HTTPException(400, "Edit plan has no operations")
        job_id = uuid.uuid4().hex[:12]
        job = jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "phase": "starting",
            "done": 0,
            "total": 0,
            "current": "",
            "created": [],
            "errors": [],
            "backup_id": None,
        }

        async def run() -> None:
            try:
                backup_id = await bot.db.save_layout_backup(gid, capture_layout(guild))
                job["backup_id"] = backup_id
                cog = bot.get_cog("Scaffolding")

                async def progress(title: str, op: Any, done: int, total: int) -> None:
                    job.update(phase=title, done=done, total=total, current=op.label)

                applied, errors = await cog.execute_edit(guild, edit, progress=progress)
                job.update(created=applied, errors=errors, status="done")
            except Exception as exc:
                log.exception("edit job %s failed", job_id)
                job.update(status="error")
                job["errors"].append(str(exc))

        asyncio.get_running_loop().create_task(run())
        return {"job_id": job_id, "backup": "pre-apply snapshot is taken inside the job"}

    # ---- channel ops ----

    @app.post("/api/guilds/{gid}/channels")
    async def create_channel(gid: int, body: ChannelCreateBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        name = sanitize_channel_name(body.name)
        category = guild.get_channel(body.category_id) if body.category_id else None
        kwargs: dict[str, Any] = {"category": category, "reason": "GuildMaster panel"}
        try:
            if body.type == "category":
                ch = await guild.create_category(name, reason="GuildMaster panel")
            elif body.type == "voice":
                ch = await guild.create_voice_channel(name, **kwargs)
            elif body.type == "forum":
                ch = await guild.create_forum(name, topic=body.topic, **kwargs)
            else:
                ch = await guild.create_text_channel(name, topic=body.topic, **kwargs)
        except discord.Forbidden:
            raise HTTPException(403, "Bot lacks Manage Channels")
        return _serialize_channel(ch)

    @app.delete("/api/guilds/{gid}/channels/{cid}")
    async def delete_channel(gid: int, cid: int, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        ch = guild.get_channel(cid)
        if ch is None:
            raise HTTPException(404, "Channel not found")
        log_channel_id = await bot.db.get_setting(gid, "log_channel_id")
        if log_channel_id and int(log_channel_id) == cid:
            raise HTTPException(400, "Refusing to delete the configured log channel")
        await ch.delete(reason="GuildMaster panel")
        return {"deleted": ch.name}

    @app.post("/api/guilds/{gid}/channels/{cid}/lock")
    async def lock_channel(gid: int, cid: int, lock: bool = True, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        ch = guild.get_channel(cid)
        if ch is None:
            raise HTTPException(404, "Channel not found")
        if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            kwargs = {"connect": False} if lock else {"connect": None}
        else:
            kwargs = {"send_messages": False} if lock else {"send_messages": None}
        await ch.set_permissions(
            guild.default_role, reason="GuildMaster panel lock", **kwargs
        )
        return {"channel": ch.name, "locked": lock}

    @app.post("/api/guilds/{gid}/channels/{cid}/slowmode")
    async def slowmode(gid: int, cid: int, body: SlowmodeBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        ch = guild.get_channel(cid)
        if ch is None:
            raise HTTPException(404, "Channel not found")
        if not hasattr(ch, "slowmode_delay"):
            raise HTTPException(400, "Slowmode applies to text/forum channels")
        await ch.edit(
            slowmode_delay=max(0, min(body.seconds, 21600)),
            reason="GuildMaster panel",
        )
        return {"channel": ch.name, "slowmode": max(0, min(body.seconds, 21600))}

    @app.post("/api/guilds/{gid}/channels/{cid}/purge")
    async def purge_channel(gid: int, cid: int, body: PurgeBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        ch = guild.get_channel(cid)
        if ch is None:
            raise HTTPException(404, "Channel not found")
        kwargs: dict[str, Any] = {
            "bulk": True,
            "reason": "GuildMaster panel purge",
            "limit": max(1, min(body.amount, 500)),
        }
        if body.member_id:
            kwargs["check"] = lambda m: m.author.id == body.member_id
        deleted = await ch.purge(**kwargs)
        return {"channel": ch.name, "deleted": len(deleted)}

    # ---- roles ----

    @app.post("/api/guilds/{gid}/roles")
    async def create_role(gid: int, body: RoleCreateBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        kwargs: dict[str, Any] = {"name": body.name[:100], "reason": "GuildMaster panel"}
        if body.color:
            try:
                kwargs["colour"] = discord.Colour(int(body.color.lstrip("#"), 16))
            except ValueError:
                raise HTTPException(400, "Invalid hex color")
        role = await guild.create_role(**kwargs)
        return _serialize_role(role)

    # ---- moderation ----

    @app.post("/api/guilds/{gid}/members/{uid}/timeout")
    async def timeout_member(gid: int, uid: int, body: TimeoutBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        member = await member_or_404(guild, uid)
        mod_guard(guild, member)
        try:
            delta = parse_duration(body.duration)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        await member.timeout(delta, reason=body.reason)
        return {"member": str(member), "timeout": format_timedelta(delta)}

    @app.post("/api/guilds/{gid}/members/{uid}/untimeout")
    async def untimeout_member(gid: int, uid: int, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        member = await member_or_404(guild, uid)
        await member.timeout(None, reason="GuildMaster panel")
        return {"member": str(member), "timeout": None}

    @app.post("/api/guilds/{gid}/members/{uid}/kick")
    async def kick_member(gid: int, uid: int, body: ReasonBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        member = await member_or_404(guild, uid)
        mod_guard(guild, member)
        await member.kick(reason=body.reason)
        return {"kicked": str(member)}

    @app.post("/api/guilds/{gid}/members/{uid}/ban")
    async def ban_member(gid: int, uid: int, body: BanBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        member = await member_or_404(guild, uid)
        mod_guard(guild, member)
        await guild.ban(
            member,
            reason=body.reason,
            delete_message_seconds=max(0, min(body.delete_days, 7)) * 86400,
        )
        return {"banned": str(member)}

    @app.post("/api/guilds/{gid}/members/{uid}/warn")
    async def warn_member(gid: int, uid: int, body: WarnBody, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        member = await member_or_404(guild, uid)
        mod_guard(guild, member)
        count = await bot.db.add_strike(gid, uid, int(bot.user.id), body.reason)
        cog = bot.get_cog("Moderation")
        consequence = (
            await cog._apply_strike_consequences(guild, member, count, body.reason)
            if cog is not None
            else None
        )
        return {"member": str(member), "strikes": count, "consequence": consequence}

    @app.get("/api/guilds/{gid}/members/{uid}/warnings")
    async def warnings(gid: int, uid: int, _: Any = Depends(auth)) -> dict:
        guild_or_404(gid)
        return {"strikes": await bot.db.list_strikes(gid, uid)}

    @app.delete("/api/guilds/{gid}/members/{uid}/warnings")
    async def clear_warnings(gid: int, uid: int, _: Any = Depends(auth)) -> dict:
        guild_or_404(gid)
        return {"cleared": await bot.db.clear_strikes(gid, uid)}

    # ---- settings & backups ----

    @app.post("/api/guilds/{gid}/settings/log-channel")
    async def set_log_channel(gid: int, body: LogChannelBody, _: Any = Depends(auth)) -> dict:
        guild_or_404(gid)
        await bot.db.set_setting(gid, "log_channel_id", body.channel_id)
        return {"log_channel_id": body.channel_id}

    @app.get("/api/guilds/{gid}/backups")
    async def list_backups(gid: int, _: Any = Depends(auth)) -> list[dict]:
        guild_or_404(gid)
        return await bot.db.list_backups(gid)

    @app.post("/api/guilds/{gid}/backups")
    async def create_backup(gid: int, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        backup_id = await bot.db.save_layout_backup(gid, capture_layout(guild))
        return {"backup_id": backup_id}

    @app.post("/api/guilds/{gid}/backups/rollback")
    async def rollback(gid: int, _: Any = Depends(auth)) -> dict:
        guild = guild_or_404(gid)
        backup = await bot.db.latest_backup(gid)
        if backup is None:
            raise HTTPException(404, "No layout backup for this guild")
        job_id = uuid.uuid4().hex[:12]
        job = jobs[job_id] = {
            "job_id": job_id,
            "kind": "rollback",
            "status": "running",
            "phase": "starting",
            "done": 0,
            "total": 0,
            "current": "",
            "errors": [],
            "backup_id": backup["id"],
        }

        async def run() -> None:
            try:
                cog = bot.get_cog("Scaffolding")

                async def progress(title: str, op: Any, done: int, total: int) -> None:
                    job.update(phase=title, done=done, total=total, current=op.label)

                results = await cog.execute_rollback(
                    guild, backup["snapshot"], progress=progress
                )
                job["errors"] = [f"{op.label}: {op.error}" for op in results if op.error]
                job["status"] = "done"
            except Exception as exc:
                log.exception("rollback job %s failed", job_id)
                job.update(status="error")
                job["errors"].append(str(exc))

        asyncio.get_running_loop().create_task(run())
        return {"job_id": job_id, "backup_id": backup["id"]}

    # ---- UI ----

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


async def start_panel(bot: Any) -> Optional[uvicorn.Server]:
    """Launch uvicorn as a task on the bot's loop; returns the Server."""
    app = create_app(bot)
    config = uvicorn.Config(
        app,
        host=bot.settings.panel_host,
        port=bot.settings.panel_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.get_running_loop().create_task(server.serve())
    bot.panel_task = task
    log.info(
        "Control panel on http://%s:%s (token auth %s)",
        bot.settings.panel_host,
        bot.settings.panel_port,
        "required" if bot.settings.panel_token else "disabled — set PANEL_TOKEN to protect non-local binds",
    )
    return server
