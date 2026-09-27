"""Moderation logic tests with mocked discord.py objects."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from guildmaster.cogs.moderation import Moderation
from guildmaster.utils.hierarchy import hierarchy_problem


class FakeRole:
    def __init__(self, position, name="role"):
        self.position = position
        self.name = name

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


def make_member(id, top_position=1, bot=False):
    return SimpleNamespace(
        id=id,
        top_role=FakeRole(top_position),
        bot=bot,
        mention=f"<@{id}>",
        timeout=AsyncMock(),
        kick=AsyncMock(),
        send=AsyncMock(),
    )


def make_guild():
    return SimpleNamespace(
        id=1,
        name="TestGuild",
        owner=make_member(99, top_position=100),
        me=make_member(4, top_position=50, bot=True),
        ban=AsyncMock(),
    )


def make_interaction(guild, invoker):
    return SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        user=invoker,
        channel_id=10,
        channel=SimpleNamespace(mention="#mod", guild=guild),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            defer=AsyncMock(),
            is_done=lambda: False,
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def make_cog(db):
    bot = SimpleNamespace(db=db, get_cog=lambda name: None)
    return Moderation(bot)


def test_hierarchy_blocks_owner_target():
    guild = make_guild()
    invoker = make_member(1, top_position=80)
    assert hierarchy_problem(guild, invoker, guild.owner)


def test_hierarchy_blocks_self_target():
    guild = make_guild()
    invoker = make_member(1, top_position=80)
    assert hierarchy_problem(guild, invoker, guild.me)


def test_hierarchy_blocks_outranked_invoker():
    guild = make_guild()
    invoker = make_member(1, top_position=5)
    target = make_member(2, top_position=7)
    assert "hierarchy" in hierarchy_problem(guild, invoker, target)


def test_hierarchy_blocks_when_bot_outranked():
    guild = make_guild()  # bot top_role=50
    invoker = make_member(1, top_position=80)
    target = make_member(2, top_position=60)
    result = hierarchy_problem(guild, invoker, target)
    assert "My role" in result


def test_hierarchy_allows_valid_action():
    guild = make_guild()
    invoker = make_member(1, top_position=40)
    target = make_member(2, top_position=5)
    assert hierarchy_problem(guild, invoker, target) is None


async def test_warn_escalates_to_timeout_at_three_strikes(db):
    cog = make_cog(db)
    guild = make_guild()
    invoker = make_member(1, top_position=40)
    target = make_member(2, top_position=5)
    interaction = make_interaction(guild, invoker)

    for _ in range(2):
        await Moderation.warn.callback(cog, interaction, target, reason="spam")
    assert target.timeout.await_count == 0
    assert await cog.bot.db.count_strikes(guild.id, target.id) == 2

    await Moderation.warn.callback(cog, interaction, target, reason="spam again")
    target.timeout.assert_awaited_once()
    assert target.timeout.await_args.kwargs["reason"].startswith("[auto]")


async def test_warn_escalates_to_ban_at_five_strikes(db):
    cog = make_cog(db)
    guild = make_guild()
    invoker = make_member(1, top_position=40)
    target = make_member(2, top_position=5)
    interaction = make_interaction(guild, invoker)

    for _ in range(5):
        await Moderation.warn.callback(cog, interaction, target, reason="spam")

    guild.ban.assert_awaited_once()
    assert await cog.bot.db.count_strikes(guild.id, target.id) == 0  # cleared on ban


async def test_warn_refuses_outranked_target(db):
    cog = make_cog(db)
    guild = make_guild()
    invoker = make_member(1, top_position=2)  # lower than target
    target = make_member(2, top_position=5)
    interaction = make_interaction(guild, invoker)

    await Moderation.warn.callback(cog, interaction, target, reason="spam")

    assert await cog.bot.db.count_strikes(guild.id, target.id) == 0
    call = interaction.response.send_message.await_args
    assert call.kwargs.get("ephemeral") is True
