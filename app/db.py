from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    # FastAPI and the scheduler use sessions from worker threads
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def alembic_config() -> Config:
    config = Config(Path(__file__).parent.parent / "alembic.ini")
    config.attributes["configure_logging"] = False
    return config


def migrate() -> None:
    command.upgrade(alembic_config(), "head")
