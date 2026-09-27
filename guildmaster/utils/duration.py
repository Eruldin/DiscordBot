"""Parsing/formatting helpers for human durations like '10m', '2h', '1d'."""
from __future__ import annotations

import re
from datetime import timedelta

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}
_PATTERN = re.compile(r"^(\d+)\s*([smhdw]?)$", re.IGNORECASE)


def parse_duration(text: str) -> timedelta:
    """'90' or '90m' -> 90 minutes (bare numbers are minutes); '2h' -> 2 hours.

    Raises ValueError for empty, malformed, zero, or absurdly large values.
    """
    m = _PATTERN.match(text.strip())
    if not m:
        raise ValueError(f"Invalid duration {text!r}. Examples: 30m, 12h, 7d, 45")
    amount = int(m.group(1))
    unit = (m.group(2) or "m").lower()
    if amount <= 0:
        raise ValueError("Duration must be positive.")
    seconds = amount * _UNIT_SECONDS[unit]
    if seconds > 28 * 86400:
        raise ValueError("Duration cannot exceed 28 days.")
    return timedelta(seconds=seconds)


def format_timedelta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
