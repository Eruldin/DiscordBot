"""Web panel tests: FastAPI app against a fake guild + real sqlite DB."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx

import guildmaster.cogs.scaffolding as scaffolding_mod
from guildmaster.cogs.scaffolding import Scaffolding
from guildmaster.models.layout_schema import ServerLayout
from guildmaster.panel.server import create_app


class FakeRole:
    def __init__(self, position=1, name="role", id=1):
        self.position = position
        self.name = name
        self.id = id
        self.color = discord.Colour.default()

    def __le__(self, other):
        return self.position <= other.position

    def __lt__(self, other):
        return self.position < other.position

    def __gt__(self, other):
        return self.position > other.position

    def __ge__(self, other):
        return self.position >= other.position

    def __eq__(self, other):
        return isinstance(other, FakeRole) and self.position == other.position

    def __hash__(self):
        return hash(self.position)


def make_channel(cid, name, ctype=discord.ChannelType.text, category=None):
    return SimpleNamespace(
        id=cid,
        name=name,
        type=ctype,
        category=category,
        category_id=category.id if category else None,
        topic="t",
        slowmode_delay=0,
        nsfw=False,
        position=cid,
        overwrites={},
        channels=[],
        delete=AsyncMock(),
        set_permissions=AsyncMock(),
        edit=AsyncMock(),
        purge=AsyncMock(return_value=[object(), object()]),
    )


def make_member(uid, top_position=1, name="user"):
    return SimpleNamespace(
        id=uid,
        display_name=name,
        bot=False,
        top_role=FakeRole(top_position),
        timed_out_until=None,
        joined_at=None,
        created_at=None,
        timeout=AsyncMock(),
        kick=AsyncMock(),
        __str__=None,
    )


def make_guild():
    everyone = FakeRole(position=0, name="@everyone", id=0)
    cat = make_channel(10, "General", discord.ChannelType.text)
    cat.channels = []
    owner = make_member(1, top_position=100, name="owner")
    guild = SimpleNamespace(
        id=832109627150827520,
        name="Basement Secret Club",
        member_count=7,
        owner=owner,
        me=SimpleNamespace(
            id=42,
            top_role=FakeRole(50, "bot"),
            guild_permissions=SimpleNamespace(manage_channels=True, manage_roles=True),
            bot=True,
        ),
        default_role=everyone,
        roles=[everyone],
        categories=[],
        channels=[cat],
        rules_channel=None,
        public_updates_channel=None,
        get_channel=None,
        get_member=None,
        fetch_member=AsyncMock(
            side_effect=discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"), "not found"
            )
        ),
        ban=AsyncMock(),
        create_role=AsyncMock(return_value=FakeRole(1, "NewRole", 99)),
        create_category=AsyncMock(
            return_value=make_channel(50, "NewCat", discord.ChannelType.text)
        ),
        create_text_channel=AsyncMock(
            return_value=make_channel(51, "newchan", discord.ChannelType.text)
        ),
        create_voice_channel=AsyncMock(
            return_value=make_channel(52, "voice", discord.ChannelType.voice)
        ),
        create_forum=AsyncMock(
            return_value=make_channel(53, "forum", discord.ChannelType.forum)
        ),
        create_stage_channel=AsyncMock(),
    )
    guild.get_channel = lambda cid: next(
        (c for c in guild.channels if c.id == cid), None
    )
    guild.get_member = lambda uid: next(
        (m for m in guild._members if m.id == uid), None
    )
    guild._members = [make_member(7, name="target"), owner]
    return guild


def make_bot(db, guild, cogs=None):
    return SimpleNamespace(
        settings=SimpleNamespace(
            panel_token="test-token",
            panel_host="127.0.0.1",
            panel_port=8080,
            openai_api_key="x",
            openai_model="gpt-4o-mini",
        ),
        db=db,
        guilds=[guild],
        user=SimpleNamespace(id=42, __str__="GuildMaster#0001"),
        latency=0.05,
        is_ready=lambda: True,
        get_guild=lambda gid: guild if gid == guild.id else None,
        get_cog=lambda name: (cogs or {}).get(name),
        panel_task=None,
    )


def make_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://panel.test"
    )


AUTH = {"X-Panel-Token": "test-token"}
GID = 832109627150827520


async def test_auth_required(db):
    guild = make_guild()
    app = create_app(make_bot(db, guild))
    async with make_client(app) as c:
        assert (await c.get("/api/status")).status_code == 401
        assert (await c.get("/api/status", headers={"X-Panel-Token": "wrong"})).status_code == 401
        assert (await c.get("/api/status", headers=AUTH)).status_code == 200
        assert (await c.get("/api/status?token=test-token")).status_code == 200


async def test_status_guilds_channels(db):
    guild = make_guild()
    app = create_app(make_bot(db, guild))
    async with make_client(app) as c:
        st = (await c.get("/api/status", headers=AUTH)).json()
        assert st["online"] is True and st["guilds"] == 1
        gs = (await c.get("/api/guilds", headers=AUTH)).json()
        assert gs[0]["name"] == "Basement Secret Club"
        ch = (await c.get(f"/api/guilds/{GID}/channels", headers=AUTH)).json()
        assert ch["uncategorized"][0]["name"] == "General"
        assert ch["log_channel_id"] is None


async def test_channel_create_lock_slowmode_delete(db):
    guild = make_guild()
    app = create_app(make_bot(db, guild))
    async with make_client(app) as c:
        r = await c.post(
            f"/api/guilds/{GID}/channels",
            headers=AUTH,
            json={"name": "New Channel!", "type": "text"},
        )
        assert r.status_code == 200
        guild.create_text_channel.assert_awaited_once()
        assert guild.create_text_channel.await_args.args[0] == "new-channel"

        r = await c.post(f"/api/guilds/{GID}/channels/10/lock?lock=true", headers=AUTH)
        assert r.status_code == 200
        guild.channels[0].set_permissions.assert_awaited_once()
        assert guild.channels[0].set_permissions.await_args.kwargs["send_messages"] is False

        r = await c.post(
            f"/api/guilds/{GID}/channels/10/slowmode",
            headers=AUTH,
            json={"seconds": 99999},
        )
        assert r.json()["slowmode"] == 21600

        r = await c.delete(f"/api/guilds/{GID}/channels/10", headers=AUTH)
        assert r.status_code == 200
        guild.channels[0].delete.assert_awaited_once()

    async with make_client(app) as c:
        await c.post(
            f"/api/guilds/{GID}/settings/log-channel",
            headers=AUTH,
            json={"channel_id": 10},
        )
        r = await c.delete(f"/api/guilds/{GID}/channels/10", headers=AUTH)
        assert r.status_code == 400  # protected log channel


async def test_moderation_endpoints(db):
    guild = make_guild()
    app = create_app(make_bot(db, guild))
    async with make_client(app) as c:
        r = await c.post(
            f"/api/guilds/{GID}/members/7/timeout",
            headers=AUTH,
            json={"duration": "10m", "reason": "test"},
        )
        assert r.status_code == 200
        guild._members[0].timeout.assert_awaited_once()

        r = await c.post(
            f"/api/guilds/{GID}/members/7/ban",
            headers=AUTH,
            json={"delete_days": 2, "reason": "spam"},
        )
        assert r.json()["banned"]
        assert guild.ban.await_args.kwargs["delete_message_seconds"] == 172800

        # hierarchy: owner can't be moderated
        r = await c.post(
            f"/api/guilds/{GID}/members/1/kick", headers=AUTH, json={}
        )
        assert r.status_code == 400

        r = await c.post(
            f"/api/guilds/{GID}/members/7/warn",
            headers=AUTH,
            json={"reason": "rude"},
        )
        assert r.json()["strikes"] == 1
        ws = (await c.get(f"/api/guilds/{GID}/members/7/warnings", headers=AUTH)).json()
        assert len(ws["strikes"]) == 1
        await c.request(
            "DELETE", f"/api/guilds/{GID}/members/7/warnings", headers=AUTH
        )
        ws = (await c.get(f"/api/guilds/{GID}/members/7/warnings", headers=AUTH)).json()
        assert ws["strikes"] == []


async def test_backups_and_rollback_404(db):
    guild = make_guild()
    app = create_app(make_bot(db, guild))
    async with make_client(app) as c:
        r = await c.post(f"/api/guilds/{GID}/backups/rollback", headers=AUTH)
        assert r.status_code == 404
        r = await c.post(f"/api/guilds/{GID}/backups", headers=AUTH)
        assert r.status_code == 200
        rows = (await c.get(f"/api/guilds/{GID}/backups", headers=AUTH)).json()
        assert len(rows) == 1


class _FakeGen:
    async def generate(self, prompt):
        return ServerLayout(
            server_summary="test layout",
            categories=[
                {
                    "category_name": "HALL",
                    "channels": [
                        {"name": "lore", "type": "text"},
                        {"name": "tavern", "type": "voice"},
                    ],
                }
            ],
        )


async def test_scaffold_preview_apply_and_rollback(db, monkeypatch):
    guild = make_guild()
    # instant queue: no artificial 1.25s delay in tests
    real_queue = scaffolding_mod.ChannelOpQueue
    monkeypatch.setattr(
        scaffolding_mod,
        "ChannelOpQueue",
        lambda **kw: real_queue(min_delay=0, max_retries=1),
    )
    scaffold = Scaffolding(make_bot(db, guild))
    bot = make_bot(db, guild, cogs={"Scaffolding": scaffold})
    scaffold.bot = bot
    app = create_app(bot, layout_gen=_FakeGen())

    async with make_client(app) as c:
        r = await c.post(
            f"/api/guilds/{GID}/scaffold/preview",
            headers=AUTH,
            json={"prompt": "rpg server"},
        )
        assert r.status_code == 200
        layout = r.json()
        assert layout["categories"][0]["category_name"] == "HALL"

        r = await c.post(
            f"/api/guilds/{GID}/scaffold/apply",
            headers=AUTH,
            json={"layout": layout, "wipe_existing": False},
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        for _ in range(50):
            await asyncio.sleep(0.01)
            job = (await c.get(f"/api/scaffold/jobs/{job_id}", headers=AUTH)).json()
            if job["status"] in ("done", "error"):
                break
        assert job["status"] == "done", job
        assert set(job["created"]) == {"NewCat", "newchan", "voice"}
        assert job["backup_id"] == 1

        # rollback restores: all created channels (which aren't in guild.channels
        # fake list — snapshot had only #10) → delete ops only for extras present
        r = await c.post(f"/api/guilds/{GID}/backups/rollback", headers=AUTH)
        job_id = r.json()["job_id"]
        for _ in range(50):
            await asyncio.sleep(0.01)
            job = (await c.get(f"/api/scaffold/jobs/{job_id}", headers=AUTH)).json()
            if job["status"] in ("done", "error"):
                break
        assert job["status"] == "done", job


async def test_index_served(db):
    guild = make_guild()
    app = create_app(make_bot(db, guild))
    async with make_client(app) as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "GuildMaster" in r.text
