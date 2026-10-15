import json
import logging
import time
from typing import Any, Protocol

import httpx

from app.config import settings
from app.metacritic import RETRY_STATUSES

log = logging.getLogger(__name__)


class LLM(Protocol):
    def complete_json(self, system: str, prompt: str) -> dict[str, Any]: ...


class OpenAICompatibleLLM:
    """Chat completions on any OpenAI-compatible API: OpenAI, Ollama's /v1, OpenRouter, Groq."""

    def __init__(
        self,
        base_url: str = settings.llm_base_url,
        api_key: str = settings.llm_api_key,
        model: str = settings.llm_model,
        timeout: float = settings.llm_timeout_seconds,
        retries: int = settings.llm_max_retries,
        backoff: float = settings.retry_backoff_seconds,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.transport = transport

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        body = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            if error is not None:
                wait = min(self.backoff * 2 ** (attempt - 1), settings.max_backoff_seconds)
                log.warning("retrying LLM call in %.1fs after %s", wait, error)
                time.sleep(wait)
            try:
                with httpx.Client(transport=self.transport, timeout=self.timeout) as http:
                    response = http.post(self.url, json=body, headers=self.headers)
                    response.raise_for_status()
                return extract_json(response.json()["choices"][0]["message"]["content"])
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in RETRY_STATUSES:
                    raise
                error = exc
            # ValueError covers a reply that isn't valid JSON, which a second try usually fixes
            except (httpx.TransportError, ValueError) as exc:
                error = exc
        assert error is not None
        raise error


def extract_json(text: str) -> dict[str, Any]:
    # small models sometimes wrap the object in prose or ``` fences despite JSON mode
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError(f"no JSON object in LLM reply: {text[:200]!r}")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError(f"LLM reply is not a JSON object: {text[:200]!r}")
    return data
