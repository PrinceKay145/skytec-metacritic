import hashlib
import heapq
import math
import operator
from array import array
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, PlatformScore

SIMILAR_LIMIT = 6
DEVELOPER_BOOST = 0.05
PLATFORM_BOOST = 0.03
MAX_DESCRIPTION_CHARS = 2000


def embedding_text(game: Game) -> str:
    parts = [game.title]
    if game.genres:
        parts.append("Genres: " + ", ".join(game.genres))
    if game.description:
        parts.append(game.description[:MAX_DESCRIPTION_CHARS])
    # likes describe what a game is; dislikes ("bugs", "short") would pull unrelated games together
    for summary in sorted(game.summaries, key=lambda s: s.source):
        if summary.likes:
            parts.append(
                f"{summary.source.capitalize()}s like: " + "; ".join(summary.likes.splitlines())
            )
    return "\n".join(parts)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def pack(vector: list[float]) -> bytes:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return array("f", [x / norm for x in vector]).tobytes()


def unpack(blob: bytes) -> array:
    vector = array("f")
    vector.frombytes(blob)
    return vector


def similar_games(
    session: Session, game: Game, model: str, limit: int = SIMILAR_LIMIT
) -> list[Game]:
    """Closest games by cosine similarity, nudged up for a shared developer or platforms."""
    if game.embedding is None or game.embedding_model != model:
        return []
    target = unpack(game.embedding)
    developers = _names(game.developer)
    platforms = {score.platform for score in game.scores}
    platforms_by_game: dict[int, set[str]] = defaultdict(set)
    for game_id, platform in session.execute(select(PlatformScore.game_id, PlatformScore.platform)):
        platforms_by_game[game_id].add(platform)

    # shortcut: brute force over every stored vector per card view, fine up to a few thousand
    # games; past that, precompute neighbours at crawl time or use pgvector
    candidates = session.execute(
        select(Game.id, Game.embedding, Game.developer).where(
            Game.id != game.id, Game.embedding_model == model, Game.embedding.is_not(None)
        )
    )
    scored: list[tuple[float, int]] = []
    for other_id, blob, developer in candidates:
        vector = unpack(blob)
        if len(vector) != len(target):
            continue
        score = sum(map(operator.mul, target, vector))
        if developers & _names(developer):
            score += DEVELOPER_BOOST
        others = platforms_by_game[other_id]
        if platforms | others:
            score += PLATFORM_BOOST * len(platforms & others) / len(platforms | others)
        scored.append((score, other_id))

    best = [other_id for _, other_id in heapq.nlargest(limit, scored)]
    games = {other.id: other for other in session.scalars(select(Game).where(Game.id.in_(best)))}
    return [games[other_id] for other_id in best]


def _names(developer: str | None) -> set[str]:
    return {name.strip().lower() for name in (developer or "").split(",") if name.strip()}
