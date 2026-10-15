from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.crawler import run_crawl
from app.db import SessionLocal
from app.metacritic import API_URL, BROWSE_PAGE_SIZE, MetacriticClient
from app.models import CrawlPhase, CrawlState, Game, PlatformScore, RunStatus
from tests.helpers import load_fixture


class FakeMetacritic:
    """Serves fixture payloads for any slug; `failing` paths answer 500."""

    def __init__(
        self,
        new_releases: list[str],
        browse_pages: list[list[str]],
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self.new_releases = new_releases
        self.browse_pages = browse_pages
        self.failing = failing

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in self.failing:
            return httpx.Response(500)
        if path == "/finder/metacritic/web":
            if "metaScoreMin" in request.url.params:
                return httpx.Response(200, json=finder(self.new_releases))
            page = int(request.url.params["offset"]) // BROWSE_PAGE_SIZE
            slugs = self.browse_pages[page] if page < len(self.browse_pages) else []
            return httpx.Response(200, json=finder(slugs))
        if path.endswith("/stats/web"):
            return httpx.Response(200, json=load_fixture(f"{path.split('/')[3]}_stats"))
        slug = path.split("/")[3]
        game = load_fixture("game")
        game["data"]["item"].update(slug=slug, title=slug.upper())
        return httpx.Response(200, json=game)

    def client(self) -> MetacriticClient:
        http = httpx.Client(transport=httpx.MockTransport(self), base_url=API_URL)
        return MetacriticClient(http, delay=0, backoff=0)


def finder(slugs: list[str]) -> dict:
    data = load_fixture("finder")
    template = data["data"]["items"][0]
    data["data"]["items"] = [{**template, "slug": slug} for slug in slugs]
    return data


def today_state() -> CrawlState:
    with SessionLocal() as session:
        state = session.get(CrawlState, datetime.now(UTC).date())
        assert state is not None
        return state


def game_slugs() -> list[str]:
    with SessionLocal() as session:
        return sorted(session.scalars(select(Game.slug)))


def test_first_runs_take_new_releases_then_page_through_browse() -> None:
    fake = FakeMetacritic(
        new_releases=["a", "b", "c"],
        browse_pages=[["c", "d", "e", "f"], ["g", "h", "a", "i", "j", "k"]],
    )

    first = run_crawl(fake.client(), limit=5)
    assert (first.status, first.processed, first.errors) == (RunStatus.success, 5, 0)
    assert game_slugs() == ["a", "b", "c", "d", "e"]
    state = today_state()
    assert (state.phase, state.next_page) == (CrawlPhase.browse, 1)

    second = run_crawl(fake.client(), limit=5)
    assert second.processed == 5
    assert game_slugs() == ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
    assert today_state().next_page == 2


def test_game_details_and_scores_are_saved() -> None:
    run_crawl(FakeMetacritic(["elden-ring"], []).client(), limit=1)

    with SessionLocal() as session:
        game = session.scalars(select(Game)).one()
        assert game.developer == "From Software"
        assert game.video_url == "https://cdn.jwplayer.com/players/OXExoEAD.html"
        scores = {score.platform: (score.metascore, score.userscore) for score in game.scores}
        assert len(scores) == 5
        assert scores["PlayStation 5"] == (96, 8.4)


def test_new_day_starts_over_and_updates_existing_games() -> None:
    fake = FakeMetacritic(new_releases=["a", "b"], browse_pages=[])
    run_crawl(fake.client(), limit=2)
    with SessionLocal() as session:
        state = today_state()
        session.delete(session.get(CrawlState, state.date))
        session.add(
            CrawlState(
                date=state.date - timedelta(days=1),
                phase=CrawlPhase.browse,
                next_page=7,
                processed_slugs=["a", "b"],
            )
        )
        session.commit()

    run = run_crawl(fake.client(), limit=2)

    assert run.processed == 2
    assert game_slugs() == ["a", "b"]
    assert today_state().processed_slugs == ["a", "b"]
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(PlatformScore)) == 10


def test_failed_game_and_platform_are_logged_and_skipped() -> None:
    fake = FakeMetacritic(
        new_releases=["a", "b", "c"],
        browse_pages=[],
        failing=frozenset(
            {
                "/games/metacritic/b/web",
                "/reviews/metacritic/user/games/c/platform/pc/stats/web",
            }
        ),
    )

    run = run_crawl(fake.client(), limit=3)

    assert (run.status, run.processed, run.errors) == (RunStatus.success, 2, 2)
    assert [(e["source"], e["url"]) for e in run.error_log] == [
        ("game", f"{API_URL}/games/metacritic/b/web"),
        ("score", f"{API_URL}/reviews/metacritic/user/games/c/platform/pc/stats/web"),
    ]
    assert game_slugs() == ["a", "c"]
    assert today_state().processed_slugs == ["a", "b", "c"]
    with SessionLocal() as session:
        c_platforms = session.scalars(
            select(PlatformScore.platform).join(Game).where(Game.slug == "c")
        ).all()
        assert "PC" not in c_platforms
        assert len(c_platforms) == 4


def test_page_cap_stops_paging_and_is_logged() -> None:
    fake = FakeMetacritic(new_releases=["a"], browse_pages=[["a"]] * 50)

    run = run_crawl(fake.client(), limit=5, max_pages=3)

    assert (run.status, run.processed, run.errors) == (RunStatus.success, 1, 1)
    assert run.error_log[0]["source"] == "browse"
    assert "page cap of 3" in run.error_log[0]["message"]


def test_end_of_catalog_stops_without_error() -> None:
    run = run_crawl(FakeMetacritic(["a"], [["b"]]).client(), limit=5)
    assert (run.processed, run.errors) == (2, 0)


def test_listing_failure_fails_the_run() -> None:
    fake = FakeMetacritic([], [], failing=frozenset({"/finder/metacritic/web"}))

    run = run_crawl(fake.client())

    assert run.status == RunStatus.failed
    assert run.error_log[0]["source"] == "run"
    assert today_state().phase == CrawlPhase.new_releases
