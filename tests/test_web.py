import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import SessionLocal
from app.models import Game, PlatformScore, ReviewSource, ReviewSummary
from app.similar import pack


def add_game(slug: str, title: str, scores: dict[str, tuple[int | None, float | None]]) -> None:
    with SessionLocal() as session:
        game = Game(slug=slug, title=title)
        for platform, (metascore, userscore) in scores.items():
            game.scores.append(
                PlatformScore(platform=platform, metascore=metascore, userscore=userscore)
            )
        session.add(game)
        session.commit()


def titles_in_order(html: str) -> list[str]:
    found = [(html.index(t), t) for t in ("Alpha Quest", "Beta 100% Run", "Gamma") if t in html]
    return [title for _, title in sorted(found)]


@pytest.fixture
def three_games() -> None:
    add_game("alpha", "Alpha Quest", {"PC": (80, 6.0)})
    add_game("beta", "Beta 100% Run", {"PlayStation 5": (90, None), "PC": (70, 9.5)})
    add_game("gamma", "Gamma", {"PlayStation 5": (None, 7.0)})


@pytest.mark.usefixtures("three_games")
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, ["Beta 100% Run", "Alpha Quest", "Gamma"]),
        ({"sort": "userscore"}, ["Beta 100% Run", "Gamma", "Alpha Quest"]),
        ({"sort": "title"}, ["Alpha Quest", "Beta 100% Run", "Gamma"]),
        ({"platform": "PC"}, ["Alpha Quest", "Beta 100% Run"]),
        ({"platform": "PlayStation 5", "sort": "userscore"}, ["Gamma", "Beta 100% Run"]),
        ({"q": "  BETA "}, ["Beta 100% Run"]),
        ({"q": "%"}, ["Beta 100% Run"]),
    ],
)
def test_list_search_filter_and_sort(
    client: TestClient, params: dict[str, str], expected: list[str]
) -> None:
    html = client.get("/", params=params).text
    assert titles_in_order(html) == expected
    assert f"{len(expected)} game" in html


@pytest.mark.usefixtures("three_games")
def test_list_shows_platform_options_and_rejects_unknown_sort(client: TestClient) -> None:
    html = client.get("/").text
    assert "<option>PC</option>" in html
    assert "<option>PlayStation 5</option>" in html
    assert client.get("/", params={"sort": "price"}).status_code == 422


@pytest.mark.usefixtures("three_games")
def test_htmx_requests_get_partials_and_load_more(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "PAGE_SIZE", 2)
    htmx = {"HX-Request": "true"}

    first = client.get("/", params={"sort": "title"}, headers=htmx).text
    assert "<html" not in first
    assert titles_in_order(first) == ["Alpha Quest", "Beta 100% Run"]
    assert "page=2" in first

    second = client.get("/", params={"sort": "title", "page": 2}, headers=htmx).text
    assert titles_in_order(second) == ["Gamma"]
    assert "Load more" not in second
    assert "3 games" not in second

    restored = client.get("/", headers={**htmx, "HX-History-Restore-Request": "true"}).text
    assert "<html" in restored


def test_game_card_shows_everything(client: TestClient) -> None:
    with SessionLocal() as session:
        game = Game(
            slug="hades-ii",
            title="Hades II",
            developer="Supergiant Games",
            description="Battle beyond the Underworld.",
            cover_url="https://www.metacritic.com/a/img/catalog/provider/x.jpg",
            video_url="https://cdn.jwplayer.com/players/abc.html",
        )
        game.scores += [
            PlatformScore(platform="PC", metascore=95, userscore=8.9),
            PlatformScore(platform="Nintendo Switch 2", metascore=None, userscore=None),
        ]
        game.summaries.append(
            ReviewSummary(
                source=ReviewSource.critic,
                likes="Tight combat\nGorgeous art",
                dislikes="Slow start",
                reviews_hash="h",
            )
        )
        session.add(game)
        session.commit()

    html = client.get("/games/hades-ii").text

    for text in (
        "Hades II",
        "Developer: Supergiant Games",
        "Battle beyond the Underworld.",
        'src="https://www.metacritic.com/a/img/catalog/provider/x.jpg"',
        'href="https://cdn.jwplayer.com/players/abc.html"',
        'href="https://www.metacritic.com/game/hades-ii/"',
        "<li>Tight combat</li><li>Gorgeous art</li>",
        "<li>Slow start</li>",
        "No user reviews yet.",
    ):
        assert text in html
    assert html.index(">PC<") < html.index(">Nintendo Switch 2<")
    assert ">95<" in html and ">8.9<" in html and ">tbd<" in html


def test_unknown_pages_get_an_html_404_and_api_paths_keep_json(client: TestClient) -> None:
    for path in ("/games/nope", "/nope"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")
        assert 'href="/"' in response.text
    assert "Game not found" in client.get("/games/nope").text

    api = client.get("/api/nope")
    assert api.status_code == 404
    assert api.json() == {"detail": "Not Found"}


def test_game_card_lists_similar_games_or_hides_the_block(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.settings, "embed_model", "embed-model")
    with SessionLocal() as session:
        for slug, vector in (("hades-ii", [1, 0]), ("hades", [0.9, 0.1]), ("tetris", [0, 1])):
            game = Game(slug=slug, title=slug.title(), embedding=pack(vector))
            game.embedding_model = "embed-model"
            session.add(game)
        session.add(Game(slug="lonely", title="Lonely"))
        session.commit()

    html = client.get("/games/hades-ii").text
    block = html[html.index("Similar games") :]
    assert block.index('href="/games/hades"') < block.index('href="/games/tetris"')
    assert "Similar games" not in client.get("/games/lonely").text


def test_long_titles_and_missing_data_still_render(client: TestClient) -> None:
    long_title = "Supercalifragilistic" * 15
    add_game("long", long_title, {})

    listing = client.get("/").text
    assert long_title in listing
    assert '<div class="cover" aria-hidden="true"></div>' in listing
    assert ">tbd<" in listing

    card = client.get("/games/long").text
    assert "Developer unknown" in card
    assert "No platforms yet" in card
    assert "No video" in card
    assert "No critic reviews yet." in card and "No user reviews yet." in card
    assert "Similar games" not in card
    assert "No games match these filters." in client.get("/", params={"q": "zzz"}).text
