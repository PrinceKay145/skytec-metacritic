import datetime as dt
from datetime import UTC, datetime
from enum import StrEnum, auto

from sqlalchemy import JSON, ForeignKey, LargeBinary, Text, UniqueConstraint
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ERROR_LOG_SIZE = 20
ERROR_MESSAGE_CHARS = 300


def utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewSource(StrEnum):
    critic = auto()
    user = auto()


class CrawlPhase(StrEnum):
    new_releases = auto()
    browse = auto()


class RunStatus(StrEnum):
    running = auto()
    success = auto()
    failed = auto()


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str]
    cover_url: Mapped[str | None]
    developer: Mapped[str | None]
    description: Mapped[str | None] = mapped_column(Text)
    video_url: Mapped[str | None]
    genres: Mapped[list[str]] = mapped_column(JSON, default=list)
    # normalized float32 vector; model and input hash decide when it needs recomputing
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    embedding_model: Mapped[str | None]
    embedding_hash: Mapped[str | None]
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    scores: Mapped[list["PlatformScore"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    summaries: Mapped[list["ReviewSummary"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )


class PlatformScore(Base):
    __tablename__ = "platform_scores"
    __table_args__ = (UniqueConstraint("game_id", "platform"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    platform: Mapped[str]
    metascore: Mapped[int | None]
    userscore: Mapped[float | None]

    game: Mapped[Game] = relationship(back_populates="scores")


class ReviewSummary(Base):
    __tablename__ = "review_summaries"
    __table_args__ = (UniqueConstraint("game_id", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    source: Mapped[ReviewSource]
    likes: Mapped[str] = mapped_column(Text)
    dislikes: Mapped[str] = mapped_column(Text)
    reviews_hash: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    game: Mapped[Game] = relationship(back_populates="summaries")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    status: Mapped[RunStatus] = mapped_column(default=RunStatus.running)
    processed: Mapped[int] = mapped_column(default=0)
    errors: Mapped[int] = mapped_column(default=0)
    error_log: Mapped[list[dict[str, str]]] = mapped_column(
        MutableList.as_mutable(JSON), default=list
    )

    def record_error(self, source: str, url: str, message: str) -> None:
        # column defaults are only applied on INSERT, so a fresh run still has None here
        self.errors = (self.errors or 0) + 1
        entry = {"source": source, "url": url, "message": message[:ERROR_MESSAGE_CHARS]}
        self.error_log = [*(self.error_log or []), entry][-ERROR_LOG_SIZE:]


class CrawlState(Base):
    __tablename__ = "crawl_state"

    date: Mapped[dt.date] = mapped_column(primary_key=True)
    phase: Mapped[CrawlPhase] = mapped_column(default=CrawlPhase.new_releases)
    next_page: Mapped[int] = mapped_column(default=1)
    processed_slugs: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
