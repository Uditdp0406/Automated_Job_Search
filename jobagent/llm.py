"""Thin wrapper around the Anthropic SDK for structured (Pydantic) calls."""
from __future__ import annotations

import logging
from typing import Optional, TypeVar

import anthropic
from pydantic import BaseModel

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, model: str = DEFAULT_MODEL, use_fallbacks: bool = True):
        # Credentials come from ANTHROPIC_API_KEY (or an `ant auth login` profile).
        self.client = anthropic.Anthropic(max_retries=4)
        self.model = model
        self.use_fallbacks = use_fallbacks

    def parse(
        self,
        *,
        system: str,
        user: str | list,
        schema: type[T],
        effort: str = "medium",
        max_tokens: int = 16000,
        cache_system: bool = True,
    ) -> T:
        """One Claude call whose answer is validated against `schema`."""
        system_block = {"type": "text", "text": system}
        if cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[system_block],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
        )
        if self.use_fallbacks:
            # On a safety decline, let the API re-run on a fallback model automatically.
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            resp = self.client.beta.messages.parse(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed - set ANTHROPIC_API_KEY.") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Bad request to Claude: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Anthropic API.") from e

        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            raise LLMError(f"Claude declined this request: {getattr(detail, 'explanation', '')}")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Claude's answer was cut off (max_tokens); raise max_tokens.")
        parsed: Optional[T] = resp.parsed_output
        if parsed is None:
            raise LLMError("Claude returned no parseable output.")
        u = resp.usage
        log.debug("claude usage in=%s out=%s cache_read=%s", u.input_tokens, u.output_tokens,
                  getattr(u, "cache_read_input_tokens", 0))
        return parsed

    def web_search_text(self, prompt: str, allowed_domains: list[str], max_uses: int = 8) -> str:
        """Run Claude with the server-side web search tool; return all text + result URLs."""
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": max_uses,
                "allowed_domains": allowed_domains,
            }],
            output_config={"effort": "low"},
        )
        chunks: list[str] = []
        for block in resp.content:
            if block.type == "text":
                chunks.append(block.text)
            elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                chunks.extend(r.url for r in block.content if getattr(r, "url", None))
        return "\n".join(chunks)
