from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import Base, engine, get_session
from app.models import Game

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
SessionDep = Annotated[Session, Depends(get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # shortcut: create_all never alters tables, move to alembic once a deployed schema changes
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Skytec Metacritic", lifespan=lifespan)


@app.get("/health")
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def game_list(request: Request, session: SessionDep) -> HTMLResponse:
    games = session.scalars(select(Game).order_by(Game.first_seen.desc())).all()
    return templates.TemplateResponse(request, "index.html", {"games": games})
