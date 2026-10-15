from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.crawler import claim_run, run_crawl
from app.db import SessionLocal
from app.metacritic import API_URL, BROWSE_PAGE_SIZE, MetacriticClient
from app.models import (
    CrawlPhase,
    CrawlRun,
    CrawlState,
    Game,
    PlatformScore,
    ReviewSource,
    ReviewSummary,
    RunStatus,
)
from app.similar import unpack
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
        self.reviews = {source: load_fixture(f"{source}_reviews") for source in ReviewSource}

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
        if path.startswith("/reviews/"):
            return httpx.Response(200, json=self.reviews[path.split("/")[3]])
        slug = path.split("/")[3]
        game = load_fixture("game")
        game["data"]["item"].update(slug=slug, title=slug.upper())
        return httpx.Response(200, json=game)

    def client(self) -> MetacriticClient:
        http = httpx.Client(transport=httpx.MockTransport(self), base_url=API_URL)
        return MetacriticClient(http, delay=0, backoff=0)

    def crawl(self, llm: "FakeLLM | None" = None, **kwargs: int) -> CrawlRun | None:
        return run_crawl(self.client(), llm or FakeLLM(), **kwargs)


class FakeLLM:
    embed_model = "fake-embed"

    def __init__(self, fail: bool = False, fail_embed: bool = False) -> None:
        self.prompts: list[str] = []
        self.embedded: list[str] = []
        self.fail = fail
        self.fail_embed = fail_embed

    def complete_json(self, system: str, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("LLM unavailable")
        return {"likes": ["Combat"], "dislikes": ["Camera"]}

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded += texts
        if self.fail_embed:
            raise RuntimeError('model "fake-embed" not found')
        return [[3.0, 4.0] for _ in texts]


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

    first = fake.crawl(limit=5)
    assert (first.status, first.processed, first.errors) == (RunStatus.success, 5, 0)
    assert game_slugs() == ["a", "b", "c", "d", "e"]
    state = today_state()
    assert (state.phase, state.next_page) == (CrawlPhase.browse, 1)

    second = fake.crawl(limit=5)
    assert second.processed == 5
    assert game_slugs() == ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
    assert today_state().next_page == 2


def test_game_details_and_scores_are_saved() -> None:
    FakeMetacritic(["elden-ring"], []).crawl(limit=1)

    with SessionLocal() as session:
        game = session.scalars(select(Game)).one()
        assert game.developer == "From Software"
        assert game.video_url == "https://cdn.jwplayer.com/players/OXExoEAD.html"
        scores = {score.platform: (score.metascore, score.userscore) for score in game.scores}
        assert len(scores) == 5
        assert scores["PlayStation 5"] == (96, 8.4)


def test_new_day_starts_over_and_updates_existing_games() -> None:
    fake = FakeMetacritic(new_releases=["a", "b"], browse_pages=[])
    fake.crawl(limit=2)
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

    run = fake.crawl(limit=2)

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

    run = fake.crawl(limit=3)

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

    run = fake.crawl(limit=5, max_pages=3)

    assert (run.status, run.processed, run.errors) == (RunStatus.success, 1, 1)
    assert run.error_log[0]["source"] == "browse"
    assert "page cap of 3" in run.error_log[0]["message"]


def test_end_of_catalog_stops_without_error() -> None:
    run = FakeMetacritic(["a"], [["b"]]).crawl(limit=5)
    assert (run.processed, run.errors) == (2, 0)


def test_listing_failure_fails_the_run() -> None:
    fake = FakeMetacritic([], [], failing=frozenset({"/finder/metacritic/web"}))

    run = fake.crawl()

    assert run.status == RunStatus.failed
    assert run.error_log[0]["source"] == "run"
    assert today_state().phase == CrawlPhase.new_releases


def forget_today() -> None:
    with SessionLocal() as session:
        session.delete(session.get_one(CrawlState, datetime.now(UTC).date()))
        session.commit()


def summary_count() -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(ReviewSummary)) or 0


def test_reviews_are_summarized_per_source() -> None:
    llm = FakeLLM()
    FakeMetacritic(["elden-ring"], []).crawl(llm, limit=1)

    assert len(llm.prompts) == 2
    assert llm.prompts[0].startswith("Game: ELDEN-RING\nCritic reviews:\n- (score 100)")
    with SessionLocal() as session:
        summaries = {s.source: s for s in session.scalars(select(ReviewSummary))}
        assert set(summaries) == {ReviewSource.critic, ReviewSource.user}
        assert (summaries[ReviewSource.user].likes, summaries[ReviewSource.user].dislikes) == (
            "Combat",
            "Camera",
        )
        assert len(summaries[ReviewSource.critic].reviews_hash) == 64


def test_summaries_refresh_only_when_reviews_change() -> None:
    fake = FakeMetacritic(["a"], [])
    fake.crawl(FakeLLM(), limit=1)

    forget_today()
    unchanged = FakeLLM()
    fake.crawl(unchanged, limit=1)
    assert unchanged.prompts == []

    forget_today()
    fake.reviews[ReviewSource.user]["data"]["items"][0]["quote"] = "Edited review"
    changed = FakeLLM()
    fake.crawl(changed, limit=1)
    assert len(changed.prompts) == 1
    assert "User reviews" in changed.prompts[0]
    assert summary_count() == 2


def test_llm_failure_keeps_the_game_and_logs_errors() -> None:
    run = FakeMetacritic(["a"], []).crawl(FakeLLM(fail=True), limit=1)

    assert (run.status, run.processed, run.errors) == (RunStatus.success, 1, 2)
    assert [e["url"] for e in run.error_log] == [
        "https://www.metacritic.com/game/a/critic-reviews/",
        "https://www.metacritic.com/game/a/user-reviews/",
    ]
    assert game_slugs() == ["a"]
    assert summary_count() == 0


def test_games_without_reviews_get_no_summary() -> None:
    fake = FakeMetacritic(["a"], [])
    for payload in fake.reviews.values():
        payload["data"]["items"] = []
    llm = FakeLLM()

    fake.crawl(llm, limit=1)

    assert llm.prompts == []
    assert summary_count() == 0


def test_games_get_genres_and_an_embedding_of_their_text() -> None:
    llm = FakeLLM()
    FakeMetacritic(["elden-ring"], []).crawl(llm, limit=1)

    assert len(llm.embedded) == 1
    assert "Genres: Action RPG" in llm.embedded[0]
    assert "Critics like: Combat" in llm.embedded[0]
    assert "Camera" not in llm.embedded[0]
    with SessionLocal() as session:
        game = session.scalars(select(Game)).one()
        assert game.genres == ["Action RPG"]
        assert game.embedding_model == "fake-embed"
        assert unpack(game.embedding or b"").tolist() == pytest.approx([0.6, 0.8])


def test_embeddings_recompute_only_when_their_text_changes() -> None:
    fake = FakeMetacritic(["a"], [])
    fake.crawl(FakeLLM(), limit=1)

    forget_today()
    unchanged = FakeLLM()
    fake.crawl(unchanged, limit=1)
    assert unchanged.embedded == []

    forget_today()
    fake.reviews[ReviewSource.user]["data"]["items"][0]["quote"] = "Edited review"
    resummarized = FakeLLM()
    resummarized.complete_json = lambda system, prompt: {"likes": ["Story"], "dislikes": []}
    fake.crawl(resummarized, limit=1)
    assert len(resummarized.embedded) == 1
    assert "Users like: Story" in resummarized.embedded[0]


def test_missing_embed_model_is_logged_once_and_skipped() -> None:
    llm = FakeLLM(fail_embed=True)
    run = FakeMetacritic(["a", "b"], []).crawl(llm, limit=2)

    assert (run.status, run.processed, run.errors) == (RunStatus.success, 2, 1)
    assert run.error_log[0]["source"] == "embedding"
    assert "rest of this run" in run.error_log[0]["message"]
    assert len(llm.embedded) == 1
    with SessionLocal() as session:
        assert session.scalars(select(Game.embedding_model)).all() == [None, None]


def test_only_one_run_can_hold_the_running_lock() -> None:
    first = claim_run()
    assert first is not None
    assert claim_run() is None
    assert FakeMetacritic(["a"], []).crawl(limit=1) is None
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(CrawlRun)) == 1

    run = FakeMetacritic(["a"], []).crawl(limit=1, run_id=first)
    assert run is not None
    assert (run.id, run.status, run.processed) == (first, RunStatus.success, 1)
    assert claim_run() is not None


def test_database_rejects_a_second_running_row() -> None:
    with SessionLocal() as session:
        session.add_all([CrawlRun(status=RunStatus.running), CrawlRun(status=RunStatus.running)])
        with pytest.raises(IntegrityError):
            session.commit()
