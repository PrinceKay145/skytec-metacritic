import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.crawler import fail_stale_runs, run_crawl
from app.db import Base, engine, get_session
from app.models import Game

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
SessionDep = Annotated[Session, Depends(get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # shortcut: create_all never alters tables, move to alembic once a deployed schema changes
    Base.metadata.create_all(engine)
    fail_stale_runs()
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_crawl,
        "interval",
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


@app.get("/health")
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def game_list(request: Request, session: SessionDep) -> HTMLResponse:
    games = session.scalars(select(Game).order_by(Game.first_seen.desc())).all()
    return templates.TemplateResponse(request, "index.html", {"games": games})
