"""Tests for prompt-driven targeted layout edits (LayoutEdit + execute_edit)."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

import guildmaster.cogs.scaffolding as scaffolding_mod
from guildmaster.cogs.scaffolding import (
    Scaffolding,
    describe_layout_for_llm,
    find_channel,
)
from guildmaster.models.layout_schema import LayoutEdit
from guildmaster.panel.server import create_app
from tests.test_panel import (
    AUTH,
    GID,
    FakeRole,
    make_bot,
    make_channel,
    make_guild,
    make_client,
)


def _no_delay(monkeypatch):
    real_queue = scaffolding_mod.ChannelOpQueue
    monkeypatch.setattr(
        scaffolding_mod,
        "ChannelOpQueue",
        lambda **kw: real_queue(min_delay=0, max_retries=1),
    )


def _guild_with_channels():
    guild = make_guild()
    cat = SimpleNamespace(
        id=20,
        name="Lounge",
        type=discord.ChannelType.category,
        channels=[],
        overwrites={},
        edit=AsyncMock(),
        delete=AsyncMock(),
        move=AsyncMock(),
        set_permissions=AsyncMock(),
    )
    ch1 = make_channel(11, "chat", category=cat)
    ch2 = make_channel(12, "memes", category=cat)
    ch3 = make_channel(13, "old-stuff", category=None)
    for ch in (ch1, ch2, ch3):
        ch.move = AsyncMock()
    cat.channels = [ch1, ch2]
    guild.categories = [cat]
    guild.channels = [*guild.channels, ch1, ch2, ch3]
    return guild, cat, ch1, ch2, ch3


async def test_describe_layout_for_llm(db):
    guild, cat, ch1, _, _ = _guild_with_channels()
    payload = json.loads(describe_layout_for_llm(guild))
    assert payload["guild"] == "Basement Secret Club"
    lounge = next(c for c in payload["categories"] if c["name"] == "Lounge")
    assert {c["name"] for c in lounge["channels"]} == {"chat", "memes"}
    assert payload["categories"][0]["channels"][0]["hidden_from_everyone"] is False
    assert any(c["name"] == "old-stuff" for c in payload["uncategorized"])


async def test_find_channel_prefers_category_hint(db):
    guild, cat, ch1, ch2, _ = _guild_with_channels()
    other = make_channel(14, "chat", category=None)
    guild.channels.append(other)
    assert find_channel(guild, "chat", "lounge") is ch1
    assert find_channel(guild, "CHAT", None) is ch1  # first match wins
    assert find_channel(guild, "nope") is None


async def test_execute_edit_rename_move_delete(db, monkeypatch):
    _no_delay(monkeypatch)
    guild, cat, ch1, ch2, ch3 = _guild_with_channels()
    cog = Scaffolding(make_bot(db, guild))
    edit = LayoutEdit(
        summary="tidy up",
        operations=[
            {"action": "rename_channel", "channel_name": "chat", "category_name": "Lounge", "new_name": "lounge-chat"},
            {"action": "move_channel", "channel_name": "old-stuff", "move_to_category": "Lounge"},
            {"action": "delete_channel", "channel_name": "memes"},
        ],
    )
    applied, errors = await cog.execute_edit(guild, edit)
    assert errors == []
    assert len(applied) == 3
    ch1.edit.assert_awaited_once()
    assert ch1.edit.await_args.kwargs["name"] == "lounge-chat"
    ch3.move.assert_awaited_once()
    assert ch3.move.await_args.kwargs["category"] is cat
    ch2.delete.assert_awaited_once()


async def test_execute_edit_create_category_and_channel(db, monkeypatch):
    _no_delay(monkeypatch)
    guild, *_ = _guild_with_channels()
    cog = Scaffolding(make_bot(db, guild))
    edit = LayoutEdit(
        summary="add zone",
        roles=[{"name": "VIP"}],
        operations=[
            {
                "action": "create_category",
                "category_name": "VIP Zone",
                "channels": [{"name": "vip-chat", "type": "text", "roles_allowed": ["VIP"]}],
            }
        ],
    )
    applied, errors = await cog.execute_edit(guild, edit)
    assert errors == []
    assert len(applied) == 3  # role + category + channel
    guild.create_role.assert_awaited_once()
    guild.create_category.assert_awaited_once_with("VIP Zone", reason="GuildMaster edit")
    guild.create_text_channel.assert_awaited_once()


async def test_execute_edit_protected_and_missing(db, monkeypatch):
    _no_delay(monkeypatch)
    guild, cat, ch1, ch2, _ = _guild_with_channels()
    cog = Scaffolding(make_bot(db, guild))
    edit = LayoutEdit(
        summary="x",
        operations=[
            {"action": "delete_channel", "channel_name": "chat"},       # protected → error
            {"action": "rename_channel", "channel_name": "ghost", "new_name": "x"},  # missing → error
            {"action": "delete_channel", "channel_name": "memes"},      # works
        ],
    )
    applied, errors = await cog.execute_edit(guild, edit, protected_ids={11})
    assert len(applied) == 1
    assert len(errors) == 2
    assert "protected" in errors[0] and "not found" in errors[1]
    ch1.delete.assert_not_awaited()
    ch2.delete.assert_awaited_once()


async def test_execute_edit_delete_category_children_guard(db, monkeypatch):
    _no_delay(monkeypatch)
    guild, cat, ch1, ch2, _ = _guild_with_channels()
    cog = Scaffolding(make_bot(db, guild))
    edit = LayoutEdit(
        summary="x",
        operations=[
            {"action": "delete_category", "category_name": "Lounge", "delete_children": True},
        ],
    )
    # ch1 is protected → the whole op aborts before deleting anything
    applied, errors = await cog.execute_edit(guild, edit, protected_ids={11})
    assert applied == []
    assert len(errors) == 1 and "protected" in errors[0]
    ch1.delete.assert_not_awaited()
    ch2.delete.assert_not_awaited()
    cat.delete.assert_not_awaited()

    applied, errors = await cog.execute_edit(guild, edit)  # unprotected
    assert errors == [] and len(applied) == 1
    ch1.delete.assert_awaited_once()
    ch2.delete.assert_awaited_once()
    cat.delete.assert_awaited_once()


async def test_execute_edit_update_channel_privacy(db, monkeypatch):
    _no_delay(monkeypatch)
    guild, cat, ch1, _, _ = _guild_with_channels()
    vip = SimpleNamespace(name="VIP", id=55, position=3)
    guild.roles.append(vip)
    cog = Scaffolding(make_bot(db, guild))
    edit = LayoutEdit(
        summary="lock chat",
        operations=[
            {
                "action": "update_channel",
                "channel_name": "chat",
                "slowmode": 30,
                "make_private_for": ["VIP"],
            }
        ],
    )
    applied, errors = await cog.execute_edit(guild, edit)
    assert errors == []
    ch1.edit.assert_awaited_once()
    assert ch1.edit.await_args.kwargs["slowmode_delay"] == 30
    calls = ch1.set_permissions.await_args_list
    assert calls[0].kwargs["view_channel"] is False
    assert calls[1].args[0] is vip and calls[1].kwargs["view_channel"] is True


async def test_execute_edit_move_into_category_created_same_plan(db, monkeypatch):
    _no_delay(monkeypatch)
    guild, cat, ch1, _, _ = _guild_with_channels()
    new_cat = SimpleNamespace(id=60, name="NewZone", channels=[], overwrites={})

    async def _mk(name, **kw):
        guild.categories.append(new_cat)
        return new_cat

    guild.create_category = AsyncMock(side_effect=_mk)
    cog = Scaffolding(make_bot(db, guild))
    edit = LayoutEdit(
        summary="x",
        operations=[
            {"action": "create_category", "category_name": "NewZone"},
            {"action": "move_channel", "channel_name": "chat", "move_to_category": "NewZone"},
        ],
    )
    # Lookups run at execution time: NewZone doesn't exist when ops are built.
    applied, errors = await cog.execute_edit(guild, edit)
    assert errors == [] and len(applied) == 2
    ch1.move.assert_awaited_once()
    assert ch1.move.await_args.kwargs["category"] is new_cat


class _FakeEditGen:
    async def generate_edit(self, prompt, layout_json):
        assert json.loads(layout_json)["guild"] == "Basement Secret Club"
        return LayoutEdit(
            summary="rename general",
            operations=[
                {"action": "rename_channel", "channel_name": "General", "new_name": "hall"}
            ],
        )


async def test_panel_edit_preview_apply(db, monkeypatch):
    _no_delay(monkeypatch)
    guild = make_guild()
    scaffold = Scaffolding(make_bot(db, guild))
    bot = make_bot(db, guild, cogs={"Scaffolding": scaffold})
    scaffold.bot = bot
    app = create_app(bot, layout_gen=_FakeEditGen())

    async with make_client(app) as c:
        r = await c.post(
            f"/api/guilds/{GID}/edit/preview",
            headers=AUTH,
            json={"prompt": "rename general to hall"},
        )
        assert r.status_code == 200
        edit = r.json()
        assert edit["operations"][0]["action"] == "rename_channel"

        r = await c.post(
            f"/api/guilds/{GID}/edit/apply", headers=AUTH, json={"edit": edit}
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        for _ in range(50):
            await asyncio.sleep(0.01)
            job = (await c.get(f"/api/scaffold/jobs/{job_id}", headers=AUTH)).json()
            if job["status"] in ("done", "error"):
                break
        assert job["status"] == "done", job
        assert job["backup_id"] == 1
        assert job["errors"] == []
        guild.channels[0].edit.assert_awaited_once()
        assert guild.channels[0].edit.await_args.kwargs["name"] == "hall"


async def test_panel_edit_apply_empty_ops_rejected(db):
    guild = make_guild()
    app = create_app(make_bot(db, guild))
    async with make_client(app) as c:
        r = await c.post(
            f"/api/guilds/{GID}/edit/apply",
            headers=AUTH,
            json={"edit": {"summary": "nothing", "operations": []}},
        )
        assert r.status_code == 400
        # invalid edit payload → 422 (FastAPI validation), not 500
        r = await c.post(
            f"/api/guilds/{GID}/edit/apply",
            headers=AUTH,
            json={"edit": {"summary": "x", "operations": [{"action": "explode_server"}]}},
        )
        assert r.status_code == 422


async def test_make_public_clears_stale_role_allows(db, monkeypatch):
    _no_delay(monkeypatch)
    guild, cat, ch1, _, _ = _guild_with_channels()
    vip = FakeRole(name="VIP", id=55, position=5)
    everyone = guild.default_role
    ow = discord.PermissionOverwrite(view_channel=True)
    ch1.overwrites = {everyone: discord.PermissionOverwrite(view_channel=False), vip: ow}
    cog = Scaffolding(make_bot(db, guild))
    edit = LayoutEdit(
        summary="reopen",
        operations=[{"action": "update_channel", "channel_name": "chat", "make_public": True}],
    )
    applied, errors = await cog.execute_edit(guild, edit)
    assert errors == []
    calls = ch1.set_permissions.await_args_list
    assert calls[0].args[0] is everyone and calls[0].kwargs.get("overwrite") is None
    assert calls[1].args[0] is vip and calls[1].kwargs.get("overwrite") is None
