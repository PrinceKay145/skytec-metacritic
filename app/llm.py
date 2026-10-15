import json
from typing import Any, Protocol

import httpx

from app.config import settings


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
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.model = model
        self.timeout = timeout
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
        with httpx.Client(transport=self.transport, timeout=self.timeout) as http:
            response = http.post(self.url, json=body, headers=self.headers)
            response.raise_for_status()
        return extract_json(response.json()["choices"][0]["message"]["content"])


def extract_json(text: str) -> dict[str, Any]:
    # small models sometimes wrap the object in prose or ``` fences despite JSON mode
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError(f"no JSON object in LLM reply: {text[:200]!r}")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError(f"LLM reply is not a JSON object: {text[:200]!r}")
    return data
