from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from guildmaster.core.llm_parser import (
    GeminiLayoutGenerator,
    LayoutGenerationError,
    LayoutGenerator,
    create_layout_generator,
)
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


# ---- Gemini backend ----


def _gemini_gen(gen_mock):
    """GeminiLayoutGenerator with a stubbed google.genai client."""
    fake_types = SimpleNamespace(GenerateContentConfig=MagicMock())
    gen = GeminiLayoutGenerator.__new__(GeminiLayoutGenerator)
    gen._types = fake_types
    gen.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=gen_mock)))
    gen.model = "gemini-test"
    gen.max_attempts = 2
    return gen


async def test_gemini_returns_parsed_layout():
    call = AsyncMock(return_value=SimpleNamespace(parsed=VALID_LAYOUT))
    gen = _gemini_gen(call)

    layout = await gen.generate("a quiet guild")
    assert layout is VALID_LAYOUT
    kwargs = call.await_args.kwargs
    assert kwargs["model"] == "gemini-test"
    assert "a quiet guild" in kwargs["contents"]


async def test_gemini_retries_on_error_then_raises():
    call = AsyncMock(side_effect=RuntimeError("boom"))
    gen = _gemini_gen(call)
    with pytest.raises(LayoutGenerationError):
        await gen.generate("p")
    assert call.await_count == 2


async def test_gemini_rejects_unparsed():
    call = AsyncMock(return_value=SimpleNamespace(parsed=None))
    gen = _gemini_gen(call)
    with pytest.raises(LayoutGenerationError):
        await gen.generate("p")


def _settings(**kw):
    base = dict(
        llm_provider="openai",
        openai_api_key="k",
        openai_model="gpt-4o-mini",
        gemini_api_key=None,
        gemini_model="gemini-3.8-flash",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_factory_openai_default():
    gen = create_layout_generator(_settings())
    assert isinstance(gen, LayoutGenerator)
    assert gen.model == "gpt-4o-mini"


def test_factory_gemini():
    with patch("google.genai.Client") as client_cls:
        gen = create_layout_generator(
            _settings(llm_provider="gemini", gemini_api_key="gk")
        )
        client_cls.assert_called_once_with(api_key="gk")
    assert isinstance(gen, GeminiLayoutGenerator)
    assert gen.model == "gemini-3.8-flash"


def test_factory_gemini_requires_key():
    with pytest.raises(LayoutGenerationError):
        create_layout_generator(_settings(llm_provider="gemini"))


def test_factory_openai_requires_key():
    with pytest.raises(LayoutGenerationError):
        create_layout_generator(_settings(openai_api_key=None))
