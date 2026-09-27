"""Pydantic schema for AI-generated server layouts (OpenAI Structured Outputs)."""
from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

MAX_SLOWMODE = 21600  # Discord's 6h cap
MAX_CHANNELS_PER_GUILD = 500

_ILLEGAL_NAME_CHARS = re.compile(r"[^a-z0-9\-_ ]")
_SPACES = re.compile(r"\s+")


def sanitize_channel_name(name: str) -> str:
    """Discord channel names are lowercase and cannot contain spaces."""
    name = _SPACES.sub("-", name.strip().lower())
    name = _ILLEGAL_NAME_CHARS.sub("", name)
    name = name.strip("-") or "channel"
    return name[:100]


class RoleDefinition(BaseModel):
    """A role the layout references; created only if missing on the guild."""

    name: str = Field(min_length=1, max_length=100)
    color: Optional[str] = Field(default=None, description="Hex color like '#A44CD3'")
    hoist: bool = False
    mentionable: bool = True

    @field_validator("color")
    @classmethod
    def _valid_hex(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", v):
            return None
        return v if v.startswith("#") else f"#{v}"


class ChannelDefinition(BaseModel):
    name: str
    type: Literal["text", "voice", "stage", "forum"] = "text"
    topic: Optional[str] = Field(default=None, max_length=1024)
    slowmode: int = 0
    nsfw: bool = False
    roles_allowed: List[str] = Field(default_factory=lambda: ["@everyone"])
    roles_denied: List[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _sanitize(cls, v: str) -> str:
        return sanitize_channel_name(v)

    @field_validator("slowmode")
    @classmethod
    def _clamp_slowmode(cls, v: int) -> int:
        return max(0, min(v or 0, MAX_SLOWMODE))

    @property
    def is_restricted(self) -> bool:
        allowed = {r.lower() for r in self.roles_allowed}
        return allowed != {"@everyone"} or bool(self.roles_denied)


class CategoryDefinition(BaseModel):
    category_name: str
    channels: List[ChannelDefinition] = Field(default_factory=list)

    @field_validator("category_name")
    @classmethod
    def _cap_name(cls, v: str) -> str:
        v = v.strip()
        return (v or "Category")[:100]


class ServerLayout(BaseModel):
    server_summary: str
    roles: List[RoleDefinition] = Field(default_factory=list)
    categories: List[CategoryDefinition] = Field(default_factory=list)

    def channel_count(self) -> int:
        return sum(len(c.channels) for c in self.categories)
