"""LLM integrations: prompt -> ServerLayout via OpenAI or Google Gemini."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Union

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from guildmaster.models.layout_schema import LayoutEdit, ServerLayout

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are GuildMaster, an architect that designs Discord server layouts.
Given a description of a community, produce a complete channel structure.

Rules:
- Group channels into descriptive categories (3-8 categories is typical).
- Use lowercase hyphenated channel names with emoji-free plain names unless asked otherwise.
- Every channel needs a short topic/description in 'topic'.
- Use 'roles_allowed'/'roles_denied' to express gating. '@everyone' means public.
  Restricted channels (staff, admin) should list specific role names like 'Admin' or 'Moderator';
  put those names in the top-level 'roles' list so they can be created if missing.
- 'type' must be one of: text, voice, stage, forum.
- Keep total channels under 50 unless the prompt asks for something larger.
- nsfw should be false unless the prompt explicitly asks for adult areas.
"""

_RETRY_HINT = (
    "Your previous answer could not be parsed into the required schema. "
    "Reply again with a complete layout containing at least one category "
    "with at least one channel."
)

EDIT_SYSTEM_PROMPT = """\
You are GuildMaster, an architect that EDITS Discord server layouts.
You are given the guild's CURRENT channel structure as JSON plus a
natural-language change request. Produce ONLY the operations needed to
satisfy it — never recreate channels that already exist.

Rules:
- Reference existing channels/categories by their CURRENT names exactly as
  given in the snapshot (matching is case-insensitive). Set 'category_name'
  as a hint when a channel name may be ambiguous.
- rename_channel / rename_category: set 'new_name'.
- move_channel: set 'move_to_category' to the target category name, or ""
  to move the channel out of all categories (top level).
- update_channel: sets topic/slowmode/nsfw and/or access via
  'make_private_for' (role names that keep view access — everyone else
  loses it) or 'make_public': true (clears the @everyone restriction).
- delete_channel / delete_category: for categories, delete_children=true
  also deletes the channels inside; with false the category is removed
  only once empty (move its channels out first if needed).
- create_channel: fill 'channel' with a full channel definition and set
  'category_name' to the parent category (or omit for top level).
- create_category: set 'category_name'; optionally put channels in
  'channels' to populate it immediately.
- Put any new roles the edit needs (e.g. for make_private_for or gated
  create_category channels) in the top-level 'roles' list so they can be
  created first.
- Order matters: operations run top-to-bottom — emit create_category
  before moving channels into it, deletions before reusing names.
- If nothing needs to change, return an empty 'operations' list and say
  so in 'summary'.
"""

_EDIT_RETRY_HINT = (
    "Your previous answer could not be parsed into the required schema. "
    "Reply again with a valid edit plan (an empty 'operations' list is "
    "acceptable only when no changes are needed)."
)


class LayoutGenerationError(RuntimeError):
    """Raised when the model fails to produce a usable layout."""


class LayoutGenerator:
    """OpenAI Structured Outputs backend."""

    def __init__(self, client: AsyncOpenAI, model: str = "gpt-4o-mini", max_attempts: int = 2) -> None:
        self.client = client
        self.model = model
        self.max_attempts = max_attempts

    async def generate(self, prompt: str) -> ServerLayout:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self.client.beta.chat.completions.parse(
                    model=self.model,
                    messages=messages,
                    response_format=ServerLayout,
                    temperature=0.4,
                )
            except Exception as exc:  # API error or schema validation failure
                last_error = exc
                log.warning("Layout generation attempt %d failed: %s", attempt, exc)
                continue

            parsed: Optional[ServerLayout] = resp.choices[0].message.parsed
            if parsed is not None and parsed.categories:
                return parsed
            last_error = LayoutGenerationError(
                "Model returned no parseable layout "
                f"(finish_reason={resp.choices[0].finish_reason!r})"
            )
            messages.append({"role": "user", "content": _RETRY_HINT})
        raise LayoutGenerationError(
            f"Failed to generate a layout after {self.max_attempts} attempts: {last_error}"
        ) from last_error

    async def generate_edit(self, prompt: str, layout_json: str) -> LayoutEdit:
        """Produce a LayoutEdit for the given current-layout JSON + request."""
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": EDIT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"CURRENT LAYOUT (JSON):\n{layout_json}\n\nCHANGE REQUEST: {prompt}",
            },
        ]
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self.client.beta.chat.completions.parse(
                    model=self.model,
                    messages=messages,
                    response_format=LayoutEdit,
                    temperature=0.3,
                )
            except Exception as exc:
                last_error = exc
                log.warning("Edit generation attempt %d failed: %s", attempt, exc)
                continue
            parsed: Optional[LayoutEdit] = resp.choices[0].message.parsed
            if parsed is not None:
                return parsed
            last_error = LayoutGenerationError(
                "Model returned no parseable edit plan "
                f"(finish_reason={resp.choices[0].finish_reason!r})"
            )
            messages.append({"role": "user", "content": _EDIT_RETRY_HINT})
        raise LayoutGenerationError(
            f"Failed to generate an edit plan after {self.max_attempts} attempts: {last_error}"
        ) from last_error


class GeminiLayoutGenerator:
    """Google Gemini backend (google-genai SDK, pydantic response_schema)."""

    def __init__(self, api_key: str, model: str = "gemini-3.8-flash", max_attempts: int = 3) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.max_attempts = max_attempts

    async def generate(self, prompt: str) -> ServerLayout:
        types = self._types
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ServerLayout,
            temperature=0.4,
        )
        last_error: Optional[Exception] = None
        user_prompt = f"{SYSTEM_PROMPT}\n\nCommunity description: {prompt}"
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=user_prompt,
                    config=config,
                )
            except Exception as exc:
                last_error = exc
                log.warning("Gemini layout attempt %d failed: %s", attempt, exc)
                # 429/503 are transient on free tier — back off before retrying
                if "429" in str(exc) or "503" in str(exc) or "EXHAUSTED" in str(exc):
                    await asyncio.sleep(min(2**attempt * 3, 30))
                continue

            parsed = getattr(resp, "parsed", None)
            if isinstance(parsed, ServerLayout) and parsed.categories:
                return parsed
            last_error = LayoutGenerationError("Gemini returned no parseable layout")
            user_prompt += "\n\n" + _RETRY_HINT
        raise LayoutGenerationError(
            f"Failed to generate a layout after {self.max_attempts} attempts: {last_error}"
        ) from last_error

    async def generate_edit(self, prompt: str, layout_json: str) -> LayoutEdit:
        types = self._types
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=LayoutEdit,
            temperature=0.3,
        )
        last_error: Optional[Exception] = None
        user_prompt = (
            f"{EDIT_SYSTEM_PROMPT}\n\nCURRENT LAYOUT (JSON):\n{layout_json}\n\n"
            f"CHANGE REQUEST: {prompt}"
        )
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=user_prompt,
                    config=config,
                )
            except Exception as exc:
                last_error = exc
                log.warning("Gemini edit attempt %d failed: %s", attempt, exc)
                if "429" in str(exc) or "503" in str(exc) or "EXHAUSTED" in str(exc):
                    await asyncio.sleep(min(2**attempt * 3, 30))
                continue
            parsed = getattr(resp, "parsed", None)
            if isinstance(parsed, LayoutEdit):
                return parsed
            last_error = LayoutGenerationError("Gemini returned no parseable edit plan")
            user_prompt += "\n\n" + _EDIT_RETRY_HINT
        raise LayoutGenerationError(
            f"Failed to generate an edit plan after {self.max_attempts} attempts: {last_error}"
        ) from last_error


def create_layout_generator(settings) -> Union[LayoutGenerator, GeminiLayoutGenerator]:
    """Build the layout generator for the configured provider."""
    provider = (settings.llm_provider or "openai").lower()
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise LayoutGenerationError(
                "LLM_PROVIDER=gemini but GEMINI_API_KEY is not set"
            )
        return GeminiLayoutGenerator(
            api_key=settings.gemini_api_key, model=settings.gemini_model
        )
    if not settings.openai_api_key:
        raise LayoutGenerationError("OPENAI_API_KEY is not set")
    return LayoutGenerator(
        AsyncOpenAI(api_key=settings.openai_api_key), model=settings.openai_model
    )
