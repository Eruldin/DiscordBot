"""OpenAI Structured Outputs integration: prompt -> ServerLayout."""
from __future__ import annotations

import logging
from typing import Optional

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from guildmaster.models.layout_schema import ServerLayout

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


class LayoutGenerationError(RuntimeError):
    """Raised when the model fails to produce a usable layout."""


class LayoutGenerator:
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
            messages.append(
                {
                    "role": "user",
                    "content": "Your previous answer could not be parsed into the "
                    "required schema. Reply again with a complete layout containing "
                    "at least one category with at least one channel.",
                }
            )
        raise LayoutGenerationError(
            f"Failed to generate a layout after {self.max_attempts} attempts: {last_error}"
        ) from last_error
