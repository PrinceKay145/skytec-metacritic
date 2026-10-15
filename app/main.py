import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.crawler import claim_run, fail_stale_runs, run_crawl
from app.db import Base, engine, get_session
from app.models import CrawlRun, CrawlState, Game, PlatformScore, ReviewSource, RunStatus
from app.similar import similar_games

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
SessionDep = Annotated[Session, Depends(get_session)]

PAGE_SIZE = 48
Sort = Literal["metascore", "userscore", "newest", "title"]
SORT_LABELS: dict[Sort, str] = {
    "metascore": "Metascore",
    "userscore": "User score",
    "newest": "Recently added",
    "title": "Title",
}
RECENT_RUNS = 15


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # shortcut: create_all never alters tables, move to alembic once a deployed schema changes
    Base.metadata.create_all(engine)
    fail_stale_runs()
    scheduler = BackgroundScheduler()
    app.state.scheduler = scheduler
    scheduler.add_job(
        run_crawl,
        "interval",
        id="crawl",
        minutes=settings.crawl_interval_minutes,
        next_run_time=datetime.now(UTC),
        max_instances=1,
        coalesce=True,
    )
    if settings.scheduler_enabled:
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Skytec Metacritic", lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def not_found_page(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code == 404 and not request.url.path.startswith("/api/"):
        return templates.TemplateResponse(
            request, "404.html", {"detail": exc.detail}, status_code=404
        )
    return await http_exception_handler(request, exc)


@app.get("/health")
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def game_list(
    request: Request,
    session: SessionDep,
    q: str = "",
    platform: str = "",
    sort: Sort = "metascore",
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    q = q.strip()
    # with a platform selected, ratings are that platform's; otherwise the best across platforms
    scores = select(
        PlatformScore.game_id,
        func.max(PlatformScore.metascore).label("metascore"),
        func.max(PlatformScore.userscore).label("userscore"),
    ).group_by(PlatformScore.game_id)
    if platform:
        scores = scores.where(PlatformScore.platform == platform)
    ratings = scores.subquery()
    query = select(Game, ratings.c.metascore, ratings.c.userscore).join(
        ratings, ratings.c.game_id == Game.id, isouter=not platform
    )
    if q:
        query = query.where(Game.title.icontains(q, autoescape=True))
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    order = {
        "metascore": ratings.c.metascore.desc().nulls_last(),
        "userscore": ratings.c.userscore.desc().nulls_last(),
        "newest": Game.first_seen.desc(),
        "title": Game.title,
    }[sort]
    rows = session.execute(
        query.order_by(order, Game.title)
        .options(selectinload(Game.scores))
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    ).all()
    context = {
        "rows": rows,
        "total": total,
        "page": page,
        "has_more": page * PAGE_SIZE < total,
        "q": q,
        "platform": platform,
        "sort": sort,
        "sorts": SORT_LABELS,
    }
    if request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request"):
        template = "_cards.html" if page > 1 else "_results.html"
    else:
        template = "index.html"
        context["platforms"] = session.scalars(
            select(PlatformScore.platform).distinct().order_by(PlatformScore.platform)
        ).all()
    return templates.TemplateResponse(request, template, context)


@app.get("/games/{slug}", response_class=HTMLResponse)
def game_card(request: Request, slug: str, session: SessionDep) -> HTMLResponse:
    game = session.scalar(
        select(Game)
        .where(Game.slug == slug)
        .options(selectinload(Game.scores), selectinload(Game.summaries))
    )
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    scores = sorted(
        game.scores, key=lambda s: (s.metascore is None, -(s.metascore or 0), s.platform)
    )
    context = {
        "game": game,
        "scores": scores,
        "summaries": {summary.source: summary for summary in game.summaries},
        "sources": list(ReviewSource),
        "similar": similar_games(session, game, settings.embed_model),
    }
    return templates.TemplateResponse(request, "game.html", context)


@app.get("/monitor", response_class=HTMLResponse)
def monitor(request: Request, session: SessionDep) -> HTMLResponse:
    today = session.get(CrawlState, datetime.now(UTC).date())
    context = {
        **_latest_context(request, session),
        "runs": session.scalars(
            select(CrawlRun).order_by(CrawlRun.id.desc()).limit(RECENT_RUNS)
        ).all(),
        "games_total": session.scalar(select(func.count()).select_from(Game)),
        "games_today": len(today.processed_slugs) if today else 0,
    }
    return templates.TemplateResponse(request, "monitor.html", context)


@app.get("/api/runs/latest")
def latest_run(request: Request, session: SessionDep) -> Response:
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "_latest_run.html", _latest_context(request, session)
        )
    run = _latest_run(session)
    if run is None:
        raise HTTPException(status_code=404, detail="No runs yet")
    return JSONResponse(jsonable_encoder(_run_json(run)))


@app.post("/api/runs")
def start_run(request: Request, session: SessionDep) -> Response:
    """Claims the run and returns its id at once; the crawl itself runs in a background thread."""
    run_id = claim_run()
    if run_id is not None:
        threading.Thread(
            target=run_crawl, kwargs={"run_id": run_id}, name=f"crawl-{run_id}", daemon=True
        ).start()
    if request.headers.get("HX-Request"):
        note = f"Run #{run_id} started." if run_id else "A run is already in progress."
        context = {**_latest_context(request, session), "note": note}
        return templates.TemplateResponse(request, "_latest_run.html", context)
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/monitor", status_code=303)
    if run_id is None:
        running = session.scalar(select(CrawlRun.id).where(CrawlRun.status == RunStatus.running))
        detail = "A crawl is already running"
        return JSONResponse({"detail": detail, "run_id": running}, status_code=409)
    return JSONResponse({"run_id": run_id}, status_code=202)


def _latest_run(session: Session) -> CrawlRun | None:
    return session.scalar(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(1))


def _latest_context(request: Request, session: Session) -> dict[str, Any]:
    scheduler: BackgroundScheduler = request.app.state.scheduler
    job = scheduler.get_job("crawl") if scheduler.running else None
    next_run = job.next_run_time.astimezone(UTC) if job and job.next_run_time else None
    return {"run": _latest_run(session), "next_run": next_run, "note": None}


def _run_json(run: CrawlRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "processed": run.processed,
        "errors": run.errors,
        "error_log": run.error_log,
    }
