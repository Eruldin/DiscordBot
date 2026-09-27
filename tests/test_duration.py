from datetime import timedelta

import pytest

from guildmaster.utils.duration import format_timedelta, parse_duration


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30m", timedelta(minutes=30)),
        ("2h", timedelta(hours=2)),
        ("1d", timedelta(days=1)),
        ("1w", timedelta(weeks=1)),
        ("45", timedelta(minutes=45)),
        ("90s", timedelta(seconds=90)),
        ("10 M", timedelta(minutes=10)),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "0m", "-5m", "29d", "100w", "x1h"])
def test_parse_duration_invalid(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=30), "30s"),
        (timedelta(minutes=10), "10m"),
        (timedelta(hours=2), "2h"),
        (timedelta(days=1), "1d"),
        (timedelta(seconds=90), "90s"),
    ],
)
def test_format_timedelta(delta, expected):
    assert format_timedelta(delta) == expected
