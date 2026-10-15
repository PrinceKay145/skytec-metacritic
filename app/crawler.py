import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.llm import LLM, OpenAICompatibleLLM
from app.metacritic import API_URL, GameDetails, MetacriticClient
from app.models import (
    CrawlPhase,
    CrawlRun,
    CrawlState,
    Game,
    PlatformScore,
    ReviewSource,
    ReviewSummary,
    RunStatus,
    utcnow,
)
from app.similar import embedding_text, pack, text_hash
from app.summaries import reviews_hash, summarize

BROWSE_URL = "https://www.metacritic.com/browse/game/all/all/all-time/new/"

log = logging.getLogger(__name__)


def claim_run() -> int | None:
    """Inserts this run's "running" row, or returns None while another run holds it."""
    with SessionLocal() as session:
        run = CrawlRun(status=RunStatus.running)
        session.add(run)
        try:
            session.commit()
        except IntegrityError:
            return None
        return run.id


def run_crawl(
    client: MetacriticClient | None = None,
    llm: LLM | None = None,
    limit: int = settings.games_per_run,
    max_pages: int = settings.max_pages_per_run,
    run_id: int | None = None,
) -> CrawlRun | None:
    """Crawls under an already claimed run_id, or claims one; skips if another run is going."""
    if run_id is None:
        run_id = claim_run()
        if run_id is None:
            log.info("crawl skipped: another run is in progress")
            return None
    with SessionLocal() as session, client or MetacriticClient() as metacritic:
        run = session.get_one(CrawlRun, run_id)
        log.info("crawl run %s started", run.id)
        try:
            _crawl(session, metacritic, llm or OpenAICompatibleLLM(), run, limit, max_pages)
            run.status = RunStatus.success
        except Exception as exc:
            log.exception("crawl run %s failed", run.id)
            session.rollback()
            run.record_error("run", "", _describe(exc))
            run.status = RunStatus.failed
        run.finished_at = utcnow()
        session.commit()
        log.info(
            "crawl run %s %s: %s games, %s errors", run.id, run.status, run.processed, run.errors
        )
        return run


def fail_stale_runs() -> int:
    """Close runs left "running" by a process that died; call before the scheduler starts."""
    with SessionLocal() as session:
        stale = session.scalars(select(CrawlRun).where(CrawlRun.status == RunStatus.running)).all()
        for run in stale:
            log.warning("crawl run %s was interrupted by a restart", run.id)
            run.record_error("run", "", "process restarted mid-run")
            run.status = RunStatus.failed
            run.finished_at = utcnow()
        session.commit()
        return len(stale)


def _crawl(
    session: Session,
    metacritic: MetacriticClient,
    llm: LLM,
    run: CrawlRun,
    limit: int,
    max_pages: int,
) -> None:
    today = datetime.now(UTC).date()
    state = session.get(CrawlState, today)
    if state is None:
        state = CrawlState(date=today, phase=CrawlPhase.new_releases, next_page=1)
        session.add(state)
        session.commit()

    slugs, phase, next_page = _pick_slugs(metacritic, run, state, limit, max_pages)
    embeddings_ok = True
    for slug in slugs:
        try:
            game = _upsert(session, metacritic, run, metacritic.game(slug))
            _summarize(metacritic, llm, run, game)
            if embeddings_ok:
                embeddings_ok = _embed(llm, run, game)
            run.processed += 1
        except Exception as exc:
            log.warning("skipping game %s: %s", slug, exc)
            session.rollback()
            run.record_error(
                "game", _url(exc, f"{API_URL}/games/metacritic/{slug}/web"), _describe(exc)
            )
        state.processed_slugs.append(slug)
        session.commit()
    # the page pointer moves only after the games are saved, so a crash re-reads the same page
    state.phase, state.next_page = phase, next_page


def _pick_slugs(
    metacritic: MetacriticClient, run: CrawlRun, state: CrawlState, limit: int, max_pages: int
) -> tuple[list[str], CrawlPhase, int]:
    seen = set(state.processed_slugs)
    picked: list[str] = []
    phase, page = state.phase, state.next_page
    for _ in range(max_pages):
        browsing = phase == CrawlPhase.browse
        if browsing:
            listed = metacritic.browse_page(page)
            if not listed:
                return picked, phase, page
        else:
            listed = metacritic.new_releases()
            phase = CrawlPhase.browse
        fresh = [slug for slug in dict.fromkeys(listed) if slug not in seen]
        taken = fresh[: limit - len(picked)]
        picked += taken
        seen.update(taken)
        # browse order is unstable within a date, so leave a page only when nothing new is left
        if browsing and len(taken) == len(fresh):
            page += 1
        if len(picked) == limit:
            return picked, phase, page
    run.record_error(
        "browse",
        f"{BROWSE_URL}?page={page}",
        f"page cap of {max_pages} reached with {len(picked)}/{limit} new games",
    )
    return picked, phase, page


def _upsert(
    session: Session, metacritic: MetacriticClient, run: CrawlRun, details: GameDetails
) -> Game:
    game = session.scalar(select(Game).where(Game.slug == details.slug)) or Game(slug=details.slug)
    game.title = details.title
    game.description = details.description
    game.developer = details.developer
    game.cover_url = details.cover_url
    game.video_url = details.video_url
    game.genres = details.genres
    session.add(game)

    scores = {score.platform: score for score in game.scores}
    for platform in details.platforms:
        try:
            metascore = metacritic.score(details.slug, platform.slug, ReviewSource.critic)
            userscore = metacritic.score(details.slug, platform.slug, ReviewSource.user)
        except Exception as exc:
            log.warning("skipping %s scores for %s: %s", platform.slug, details.slug, exc)
            fallback = f"https://www.metacritic.com/game/{details.slug}/"
            run.record_error("score", _url(exc, fallback), _describe(exc))
            continue
        score = scores.get(platform.name)
        if score is None:
            score = PlatformScore(platform=platform.name)
            game.scores.append(score)
        score.metascore = None if metascore is None else int(metascore)
        score.userscore = userscore
    return game


def _summarize(metacritic: MetacriticClient, llm: LLM, run: CrawlRun, game: Game) -> None:
    summaries = {summary.source: summary for summary in game.summaries}
    for source in ReviewSource:
        current = summaries.get(source)
        try:
            reviews = metacritic.reviews(game.slug, source)
            digest = reviews_hash(reviews)
            if not reviews or (current and current.reviews_hash == digest):
                continue
            summary = summarize(llm, game.title, source, reviews)
        except Exception as exc:
            log.warning("skipping %s review summary for %s: %s", source, game.slug, exc)
            fallback = f"https://www.metacritic.com/game/{game.slug}/{source}-reviews/"
            run.record_error("summary", _url(exc, fallback), _describe(exc))
            continue
        if current is None:
            current = ReviewSummary(source=source)
            game.summaries.append(current)
        current.likes = "\n".join(summary.likes)
        current.dislikes = "\n".join(summary.dislikes)
        current.reviews_hash = digest


def _embed(llm: LLM, run: CrawlRun, game: Game) -> bool:
    """Returns False when the embed model fails, so the rest of the run skips embeddings."""
    text = embedding_text(game)
    digest = text_hash(text)
    if game.embedding_model == llm.embed_model and game.embedding_hash == digest:
        return True
    try:
        [vector] = llm.embed([text])
    except Exception as exc:
        log.warning("embeddings unavailable, skipping them for this run: %s", exc)
        message = f"{_describe(exc)} (embeddings skipped for the rest of this run)"
        run.record_error("embedding", _url(exc, ""), message)
        return False
    game.embedding = pack(vector)
    game.embedding_model = llm.embed_model
    game.embedding_hash = digest
    return True


def _url(exc: Exception, fallback: str) -> str:
    if isinstance(exc, httpx.RequestError | httpx.HTTPStatusError):
        return str(exc.request.url)
    return fallback


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


if __name__ == "__main__":
    from app.db import Base, engine

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    Base.metadata.create_all(engine)
    run_crawl()
