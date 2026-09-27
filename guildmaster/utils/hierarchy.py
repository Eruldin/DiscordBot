"""Role-hierarchy and permission guardrails for moderation actions."""
from __future__ import annotations

from typing import Optional

import discord


def hierarchy_problem(
    guild: discord.Guild, invoker: discord.Member, target: discord.Member
) -> Optional[str]:
    """Return a human-readable reason the action is unsafe, else None.

    Discord enforces the role hierarchy: a member (or the bot) cannot act on
    someone whose top role is >= their own. The guild owner bypasses this.
    """
    if target == guild.owner:
        return "I can't moderate the server owner."
    if target.bot and target == guild.me:
        return "I can't moderate myself."
    if invoker != guild.owner and invoker.top_role <= target.top_role:
        return (
            f"{target.mention}'s top role is not below yours — "
            "Discord's role hierarchy forbids this."
        )
    if guild.me.top_role <= target.top_role:
        return (
            f"My role must be above {target.mention}'s top role. "
            "Move the bot's role higher in Server Settings → Roles."
        )
    return None


def bot_permission_problem(
    channel: discord.abc.GuildChannel, *perms: str
) -> Optional[str]:
    """Return a reason string if the bot lacks any of the named perms here."""
    me = channel.guild.me
    granted = channel.permissions_for(me)
    missing = [p for p in perms if not getattr(granted, p, False)]
    if missing:
        return "I'm missing permission(s): " + ", ".join(f"`{p}`" for p in missing)
    return None
