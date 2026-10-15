import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, NamedTuple, Self

import httpx

from app.config import settings
from app.models import ReviewSource

API_URL = "https://backend.metacritic.com"
IMAGE_URL = "https://www.metacritic.com/a/img/catalog"
FINDER_PATH = "/finder/metacritic/web"
GAMES_TYPE_ID = 13
NEW_RELEASES_LIMIT = 20
BROWSE_PAGE_SIZE = 24
REVIEW_PAGE_SIZE = {ReviewSource.critic: 10, ReviewSource.user: 50}
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_AFTER_STATUSES = {429, 503}

log = logging.getLogger(__name__)


class Platform(NamedTuple):
    slug: str
    name: str


@dataclass(frozen=True)
class GameDetails:
    slug: str
    title: str
    description: str | None
    developer: str | None
    cover_url: str | None
    video_url: str | None
    genres: list[str]
    platforms: list[Platform]


@dataclass(frozen=True)
class Review:
    quote: str
    score: float | None
    author: str | None
    date: str | None


def parse_finder(data: dict[str, Any]) -> list[str]:
    return [item["slug"] for item in data["data"]["items"]]


def parse_game(data: dict[str, Any]) -> GameDetails:
    item = data["data"]["item"]
    companies = (item.get("production") or {}).get("companies") or []
    developers = dict.fromkeys(c["name"] for c in companies if c.get("typeName") == "Developer")
    images = {image.get("typeName"): image for image in item.get("images") or []}
    cover = images.get("cardImage") or images.get("mainImage")
    return GameDetails(
        slug=item["slug"],
        title=item["title"],
        description=(item.get("description") or "").strip() or None,
        developer=", ".join(developers) or None,
        cover_url=f"{IMAGE_URL}{cover['bucketPath']}"
        if cover and cover.get("bucketPath")
        else None,
        video_url=(item.get("video") or {}).get("embedUrl"),
        genres=[genre["name"] for genre in item.get("genres") or [] if genre.get("name")],
        platforms=[Platform(p["slug"], p["name"]) for p in item.get("platforms") or []],
    )


def parse_score(data: dict[str, Any]) -> float | None:
    item = data["data"]["item"]
    # until there are enough ratings the API sends score 0 with no sentiment; the site shows "tbd"
    return item.get("score") if item.get("sentiment") else None


def parse_reviews(data: dict[str, Any]) -> list[Review]:
    return [
        Review(
            quote=quote,
            score=None if item.get("score") is None else float(item["score"]),
            author=item.get("publicationName") or item.get("author") or None,
            date=item.get("date"),
        )
        for item in data["data"]["items"]
        if (quote := (item.get("quote") or "").strip())
    ]


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None or response.status_code not in RETRY_AFTER_STATUSES:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class MetacriticClient:
    def __init__(
        self,
        http: httpx.Client | None = None,
        delay: float = settings.request_delay_seconds,
        retries: int = settings.max_retries,
        backoff: float = settings.retry_backoff_seconds,
        max_backoff: float = settings.max_backoff_seconds,
    ) -> None:
        self.http = http or httpx.Client(
            base_url=API_URL,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout_seconds,
        )
        self.delay = delay
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self._last_request = float("-inf")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.http.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        error: httpx.HTTPError | None = None
        wait = 0.0
        for attempt in range(self.retries + 1):
            if error is not None:
                log.warning("retrying %s in %.1fs after %s", path, wait, error)
                time.sleep(wait)
            self._throttle()
            retry_after = None
            try:
                response = self.http.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in RETRY_STATUSES:
                    raise
                error = exc
                retry_after = retry_after_seconds(exc.response)
            except httpx.TransportError as exc:
                error = exc
            if retry_after is None:
                retry_after = self.backoff * 2**attempt
            wait = min(retry_after, self.max_backoff)
        assert error is not None
        raise error

    def _throttle(self) -> None:
        wait = self._last_request + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def new_releases(self) -> list[str]:
        params = {
            "sortBy": "-releaseDate",
            "metaScoreMin": 1,
            "mcoTypeId": GAMES_TYPE_ID,
            "offset": 0,
            "limit": NEW_RELEASES_LIMIT,
        }
        return parse_finder(self.get(FINDER_PATH, params))

    def browse_page(self, page: int) -> list[str]:
        params = {
            "sortBy": "-releaseDate",
            "mcoTypeId": GAMES_TYPE_ID,
            "offset": (page - 1) * BROWSE_PAGE_SIZE,
            "limit": BROWSE_PAGE_SIZE,
        }
        return parse_finder(self.get(FINDER_PATH, params))

    def game(self, slug: str) -> GameDetails:
        return parse_game(self.get(f"/games/metacritic/{slug}/web"))

    def score(self, slug: str, platform: str, source: ReviewSource) -> float | None:
        path = f"/reviews/metacritic/{source}/games/{slug}/platform/{platform}/stats/web"
        return parse_score(self.get(path))

    def reviews(self, slug: str, source: ReviewSource) -> list[Review]:
        params = {"offset": 0, "limit": REVIEW_PAGE_SIZE[source], "sort": "date"}
        return parse_reviews(self.get(f"/reviews/metacritic/{source}/games/{slug}/web", params))
