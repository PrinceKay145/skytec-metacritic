from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from app import metacritic
from app.metacritic import (
    API_URL,
    IMAGE_URL,
    MetacriticClient,
    Platform,
    parse_finder,
    parse_game,
    parse_reviews,
    parse_score,
)
from tests.helpers import load_fixture


def test_parse_finder() -> None:
    assert parse_finder(load_fixture("finder")) == [
        "enshrouded",
        "disney-pixar-toy-story-retro-roundup",
        "blueys-happy-snaps",
    ]


def test_parse_game() -> None:
    game = parse_game(load_fixture("game"))
    assert game.slug == "elden-ring"
    assert game.title == "Elden Ring"
    assert game.developer == "From Software"
    assert game.description and game.description.startswith("A New World Created")
    assert game.cover_url == f"{IMAGE_URL}/provider/6/3/6-1-824862-13.jpg"
    assert game.video_url == "https://cdn.jwplayer.com/players/OXExoEAD.html"
    assert Platform("playstation-5", "PlayStation 5") in game.platforms
    assert len(game.platforms) == 5


def test_parse_game_without_video_or_images() -> None:
    data = load_fixture("game")
    data["data"]["item"].update(video=None, images=[], production=None)
    game = parse_game(data)
    assert game.video_url is None
    assert game.cover_url is None
    assert game.developer is None


def test_parse_score() -> None:
    assert parse_score(load_fixture("critic_stats")) == 96
    assert parse_score(load_fixture("user_stats")) == 8.4
    assert parse_score(load_fixture("user_stats_unrated")) is None


def test_parse_reviews() -> None:
    critic = parse_reviews(load_fixture("critic_reviews"))
    assert len(critic) == 3
    assert critic[0].score == 100
    assert critic[0].author == "LEVEL (Czech Republic)"
    assert critic[0].quote.startswith("Elden Ring is simply")

    user_data = load_fixture("user_reviews")
    user_data["data"]["items"][1]["quote"] = "   "
    user = parse_reviews(user_data)
    assert [review.author for review in user] == ["JoshZaWarudo", "chuddyunc"]
    assert user[0].score == 0


def make_client(handler: httpx.MockTransport, **kwargs: float) -> MetacriticClient:
    http = httpx.Client(transport=handler, base_url=API_URL)
    return MetacriticClient(http, **{"delay": 0, "backoff": 0, **kwargs})


def test_get_retries_server_errors_then_succeeds() -> None:
    statuses = iter([503, 429, 200])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(next(statuses), json={"ok": True})

    assert make_client(httpx.MockTransport(handler)).get("/x") == {"ok": True}
    assert len(calls) == 3


def test_get_gives_up_after_max_retries() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(httpx.ConnectError):
        make_client(httpx.MockTransport(handler), retries=2).get("/x")
    assert len(calls) == 3


def test_get_does_not_retry_client_errors() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404)

    with pytest.raises(httpx.HTTPStatusError):
        make_client(httpx.MockTransport(handler)).get("/x")
    assert len(calls) == 1


def test_get_waits_for_backoff_and_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(metacritic.time, "sleep", sleeps.append)
    statuses = iter([500, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), json={})

    make_client(httpx.MockTransport(handler), delay=1.0, backoff=2.0).get("/x")
    assert sleeps[0] == 2.0
    assert 0.9 < sleeps[1] <= 1.0


def http_date(seconds_from_now: float) -> str:
    return format_datetime(datetime.now(UTC) + timedelta(seconds=seconds_from_now), usegmt=True)


@pytest.mark.parametrize(
    ("status", "retry_after", "expected"),
    [
        (429, "5", 5.0),
        (503, "120", 10.0),
        (429, http_date(8), 7.5),
        (429, None, 2.0),
        (429, "soon", 2.0),
        (500, "5", 2.0),
    ],
)
def test_get_honors_retry_after_up_to_max_backoff(
    monkeypatch: pytest.MonkeyPatch, status: int, retry_after: str | None, expected: float
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(metacritic.time, "sleep", sleeps.append)
    headers = {"Retry-After": retry_after} if retry_after else {}
    responses = iter([httpx.Response(status, headers=headers), httpx.Response(200, json={})])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    make_client(httpx.MockTransport(handler), backoff=2.0, max_backoff=10.0).get("/x")
    assert sleeps == [pytest.approx(expected, abs=1.0)]
