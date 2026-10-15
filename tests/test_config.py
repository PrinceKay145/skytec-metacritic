import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@db:5432/app", "postgresql+psycopg://u:p@db:5432/app"),
        (
            "postgresql://u:p@db/app?sslmode=require",
            "postgresql+psycopg://u:p@db/app?sslmode=require",
        ),
        ("postgresql+psycopg://u:p@db/app", "postgresql+psycopg://u:p@db/app"),
        ("sqlite:///./metacritic.db", "sqlite:///./metacritic.db"),
    ],
)
def test_database_url_gets_the_psycopg_driver(given: str, expected: str) -> None:
    assert Settings(database_url=given).database_url == expected
