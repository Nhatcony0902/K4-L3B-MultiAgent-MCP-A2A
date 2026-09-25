"""Optional small-LLM second opinion (<10B params) via an OpenAI-compatible API.

The LLM never writes output fields; it only independently labels the primary issue from
compact facts so the verifier can lower confidence on disagreement. Missing key or any
API failure disables it for that case and is surfaced in the trace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx2

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "meta-llama/llama-3.1-8b-instruct"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 2

SYSTEM_PROMPT = (
    "You audit e-commerce complaint investigations. Given facts about ONE order, answer "
    "with exactly one label from the allowed list and nothing else."
)


@dataclass(frozen=True)
class LlmConfig:
    api_key: str
    base_url: str
    model: str

    @classmethod
    def from_env(cls) -> LlmConfig | None:
        api_key = os.getenv("LLM_API_KEY", "").strip()
        if not api_key:
            return None
        base_url = os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
        model = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip()
        return cls(api_key, base_url, model)


class LlmVerifier:
    def __init__(self, config: LlmConfig) -> None:
        self.config = config
        self._client = httpx2.AsyncClient(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    async def label(self, facts_text: str, allowed: list[str]) -> str | None:
        """Return one allowed label, or None when the model is unavailable/unparseable."""
        prompt = f"Allowed labels: {', '.join(allowed)}\n\nFacts:\n{facts_text}\n\nLabel:"
        body = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": 12,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        for _ in range(MAX_ATTEMPTS):
            try:
                response = await self._client.post("/chat/completions", json=body)
                response.raise_for_status()
                text = response.json()["choices"][0]["message"]["content"] or ""
            except (httpx2.HTTPError, KeyError, IndexError, ValueError):
                continue
            return _match_label(text, allowed)
        return None

    async def aclose(self) -> None:
        await self._client.aclose()


def _match_label(text: str, allowed: list[str]) -> str | None:
    normalized = text.strip().strip("`'\". ").lower()
    if normalized in allowed:
        return normalized
    hits = [label for label in allowed if label in normalized]
    return max(hits, key=len) if hits else None
