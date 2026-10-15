from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Game, PlatformScore


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
