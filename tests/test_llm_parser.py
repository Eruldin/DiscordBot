from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from guildmaster.core.llm_parser import LayoutGenerationError, LayoutGenerator
from guildmaster.models.layout_schema import ServerLayout


def _response(parsed, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(parsed=parsed), finish_reason=finish_reason)
        ]
    )


def _client(parse_mock):
    return SimpleNamespace(
        beta=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(parse=parse_mock))
        )
    )


VALID_LAYOUT = ServerLayout(
    server_summary="tavern",
    categories=[{"category_name": "Inn", "channels": [{"name": "chat"}]}],
)


async def test_generate_returns_parsed_layout():
    parse = AsyncMock(return_value=_response(VALID_LAYOUT))
    gen = LayoutGenerator(_client(parse), model="test-model")

    layout = await gen.generate("make me a tavern")

    assert layout.server_summary == "tavern"
    assert layout.categories[0].category_name == "Inn"
    parse.assert_awaited_once()
    _, kwargs = parse.call_args
    assert kwargs["model"] == "test-model"
    assert kwargs["response_format"] is ServerLayout


async def test_generate_retries_when_unparsed():
    parse = AsyncMock(
        side_effect=[
            _response(None, finish_reason="length"),
            _response(VALID_LAYOUT),
        ]
    )
    gen = LayoutGenerator(_client(parse), model="m")
    layout = await gen.generate("prompt")
    assert layout is VALID_LAYOUT
    assert parse.await_count == 2


async def test_generate_retries_on_api_error():
    parse = AsyncMock(side_effect=[RuntimeError("boom"), _response(VALID_LAYOUT)])
    gen = LayoutGenerator(_client(parse), model="m")
    assert await gen.generate("prompt") is VALID_LAYOUT


async def test_generate_raises_after_max_attempts():
    parse = AsyncMock(return_value=_response(None, finish_reason="stop"))
    gen = LayoutGenerator(_client(parse), model="m", max_attempts=2)
    with pytest.raises(LayoutGenerationError):
        await gen.generate("prompt")
    assert parse.await_count == 2


async def test_generate_rejects_empty_categories():
    empty = ServerLayout(server_summary="x", categories=[])
    parse = AsyncMock(return_value=_response(empty))
    gen = LayoutGenerator(_client(parse), model="m", max_attempts=1)
    with pytest.raises(LayoutGenerationError):
        await gen.generate("prompt")
