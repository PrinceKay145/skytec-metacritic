import json

import httpx
import pytest

from app.llm import OpenAICompatibleLLM, extract_json
from app.metacritic import Review, parse_reviews
from app.models import ReviewSource
from app.summaries import (
    MAX_POINTS,
    MAX_PROMPT_CHARS,
    MAX_QUOTE_CHARS,
    build_prompt,
    parse_summary,
    reviews_hash,
)
from tests.helpers import load_fixture


def test_reviews_hash_ignores_order_but_not_content() -> None:
    reviews = parse_reviews(load_fixture("user_reviews"))
    changed = [*reviews[:-1], Review(**{**reviews[-1].__dict__, "quote": "edited"})]

    assert reviews_hash(reviews) == reviews_hash(list(reversed(reviews)))
    assert reviews_hash(reviews) != reviews_hash(changed)


def test_build_prompt_lists_scored_quotes_within_budget() -> None:
    prompt = build_prompt(
        "Elden Ring", ReviewSource.critic, parse_reviews(load_fixture("critic_reviews"))
    )
    assert prompt.startswith("Game: Elden Ring\nCritic reviews:\n- (score 100) Elden Ring is")

    long_reviews = [Review("x" * 2000, 7.5, "someone", None)] * 100
    prompt = build_prompt("Game", ReviewSource.user, long_reviews)
    assert len(prompt) <= MAX_PROMPT_CHARS + 100
    assert "- (score 7.5) " + "x" * MAX_QUOTE_CHARS + "\n" in prompt


def test_parse_summary_cleans_points() -> None:
    summary = parse_summary({"likes": [" Combat ", "", "World"] + ["more"] * 10, "dislikes": None})
    assert summary.likes[:2] == ["Combat", "World"]
    assert len(summary.likes) == MAX_POINTS
    assert summary.dislikes == []

    with pytest.raises(ValueError):
        parse_summary({"likes": "great game"})


def test_extract_json_tolerates_fences_and_rejects_prose() -> None:
    assert extract_json('```json\n{"likes": ["a"]}\n```') == {"likes": ["a"]}
    with pytest.raises(ValueError):
        extract_json("I cannot summarize this.")


def test_openai_compatible_llm_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        content = '{"likes": ["Combat"], "dislikes": []}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    llm = OpenAICompatibleLLM(
        base_url="http://llm.test/v1/",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    assert llm.complete_json("system text", "user text") == {"likes": ["Combat"], "dislikes": []}
    request = seen[0]
    assert str(request.url) == "http://llm.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
