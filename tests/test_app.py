from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import CrawlRun, Game, PlatformScore, RunStatus


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_game_list(client: TestClient) -> None:
    assert "No games yet." in client.get("/").text

    with SessionLocal() as session:
        game = Game(slug="hades-ii", title="Hades II", developer="Supergiant Games")
        game.scores.append(PlatformScore(platform="ps5", metascore=94, userscore=8.7))
        session.add(game)
        session.commit()

    page = client.get("/").text
    assert "Hades II" in page
    assert "Supergiant Games" in page


def test_startup_marks_interrupted_runs_failed() -> None:
    from app.main import app

    with SessionLocal() as session:
        stale, done = CrawlRun(status=RunStatus.running), CrawlRun(status=RunStatus.success)
        session.add_all([stale, done])
        session.commit()

    with TestClient(app):
        pass

    with SessionLocal() as session:
        stale = session.get_one(CrawlRun, stale.id)
        assert stale.status == RunStatus.failed
        assert stale.finished_at is not None
        assert stale.error_log == [
            {"source": "run", "url": "", "message": "process restarted mid-run"}
        ]
        assert session.get_one(CrawlRun, done.id).status == RunStatus.success
