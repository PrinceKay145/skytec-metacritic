import json

import httpx
import pytest

from app.db import SessionLocal
from app.llm import OpenAICompatibleLLM
from app.models import Game, PlatformScore
from app.similar import pack, similar_games, unpack

MODEL = "embed-model"


def add(
    slug: str,
    vector: list[float] | None,
    developer: str | None = None,
    platforms: tuple[str, ...] = (),
    model: str = MODEL,
) -> None:
    with SessionLocal() as session:
        game = Game(slug=slug, title=slug.upper(), developer=developer)
        if vector is not None:
            game.embedding, game.embedding_model = pack(vector), model
        game.scores = [PlatformScore(platform=platform) for platform in platforms]
        session.add(game)
        session.commit()


def similar_slugs(slug: str, limit: int = 6) -> list[str]:
    with SessionLocal() as session:
        game = session.query(Game).filter_by(slug=slug).one()
        return [other.slug for other in similar_games(session, game, MODEL, limit)]


def test_pack_normalizes_to_float32() -> None:
    vector = unpack(pack([3.0, 4.0]))
    assert vector.tolist() == pytest.approx([0.6, 0.8])
    assert vector.itemsize == 4


def test_ranking_uses_cosine_then_developer_and_platform_boosts() -> None:
    add("target", [1, 0], developer="Studio A, Studio B", platforms=("PC", "PlayStation 5"))
    add("close", [0.95, 0.31])
    add("same-dev", [0.8, 0.6], developer="studio b")
    add("same-platforms", [0.8, 0.6], platforms=("PC", "PlayStation 5"))
    add("plain", [0.8, 0.6])
    add("far", [0, 1])
    add("no-vector", None)
    add("other-model", [1, 0], model="old-model")
    add("other-dims", [1, 0, 0])

    assert similar_slugs("target") == ["close", "same-dev", "same-platforms", "plain", "far"]
    assert similar_slugs("target", limit=2) == ["close", "same-dev"]


def test_no_similar_games_without_a_current_vector() -> None:
    add("unembedded", None)
    add("stale", [1, 0], model="old-model")
    add("other", [1, 0])

    assert similar_slugs("unembedded") == []
    assert similar_slugs("stale") == []


def test_embed_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        data = [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]
        return httpx.Response(200, json={"data": data})

    llm = OpenAICompatibleLLM(
        base_url="http://llm.test/v1", embed_model=MODEL, transport=httpx.MockTransport(handler)
    )

    assert llm.embed(["first", "second"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert str(seen[0].url) == "http://llm.test/v1/embeddings"
    assert json.loads(seen[0].content) == {"model": MODEL, "input": ["first", "second"]}
