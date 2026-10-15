import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import SessionLocal
from app.models import CrawlRun, RunStatus

HTMX = {"HX-Request": "true"}


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Replaces the real crawl so Run now never touches the network; records claimed run ids."""
    run_ids: list[int] = []
    done = threading.Event()

    def fake_run_crawl(run_id: int) -> None:
        run_ids.append(run_id)
        done.set()

    monkeypatch.setattr(main, "run_crawl", fake_run_crawl)
    yield run_ids
    if run_ids:
        assert done.wait(1)


def add_run(status: RunStatus, errors: int = 0, processed: int = 0) -> int:
    with SessionLocal() as session:
        run = CrawlRun(status=status, processed=processed)
        for n in range(errors):
            run.record_error("game", f"https://example.test/{n}", f"error {n}")
        session.add(run)
        session.commit()
        return run.id


def test_run_now_returns_the_run_id_and_refuses_to_overlap(
    client: TestClient, started: list[int]
) -> None:
    response = client.post("/api/runs")
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    busy = client.post("/api/runs")
    assert busy.status_code == 409
    assert busy.json()["run_id"] == run_id
    assert started == [run_id]


def test_run_now_from_the_page(client: TestClient, started: list[int]) -> None:
    fragment = client.post("/api/runs", headers=HTMX).text
    assert f"Run #{started[0]} started." in fragment
    assert "running" in fragment
    assert "already in progress" in client.post("/api/runs", headers=HTMX).text

    with SessionLocal() as session:
        session.get_one(CrawlRun, started[0]).status = RunStatus.success
        session.commit()
    no_js = client.post("/api/runs", headers={"Accept": "text/html"}, follow_redirects=False)
    assert (no_js.status_code, no_js.headers["location"]) == (303, "/monitor")


def test_latest_run_as_json_and_as_fragment(client: TestClient) -> None:
    assert client.get("/api/runs/latest").status_code == 404
    assert "No runs yet" in client.get("/api/runs/latest", headers=HTMX).text

    add_run(RunStatus.success)
    run_id = add_run(RunStatus.failed, errors=8, processed=12)

    latest = client.get("/api/runs/latest").json()
    assert (latest["id"], latest["status"], latest["processed"], latest["errors"]) == (
        run_id,
        "failed",
        12,
        8,
    )
    assert len(latest["error_log"]) == 8

    fragment = client.get("/api/runs/latest", headers=HTMX).text
    assert f"Run #{run_id}" in fragment
    assert "<dd>12</dd>" in fragment and "<dd>8</dd>" in fragment
    assert "error 7" in fragment and "error 3" in fragment and "error 2" not in fragment
    assert fragment.index("error 7") < fragment.index("error 3")


def test_monitor_page_polls_the_latest_run(client: TestClient) -> None:
    add_run(RunStatus.success, processed=20)
    add_run(RunStatus.failed, errors=1)

    html = client.get("/monitor").text
    assert 'hx-get="/api/runs/latest"' in html
    assert 'hx-trigger="every 3s"' in html
    assert 'hx-post="/api/runs"' in html
    assert html.count("<tr>") == 3
    assert "scheduler off" in html


def test_run_now_button_is_disabled_while_a_run_is_going(client: TestClient) -> None:
    add_run(RunStatus.running)
    fragment = client.get("/api/runs/latest", headers=HTMX).text
    assert "disabled" in fragment and "Running…" in fragment


def test_admin_token_guards_run_now(
    client: TestClient, started: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.settings, "admin_token", "s3cret")

    assert client.post("/api/runs").status_code == 403
    assert client.post("/api/runs", headers={"X-Admin-Token": "wrong"}).status_code == 403
    non_ascii = {"X-Admin-Token": "ünïcode".encode()}
    assert client.post("/api/runs", headers=non_ascii).status_code == 403
    page = client.get("/monitor").text
    assert 'hx-post="/api/runs"' not in page
    assert 'action="/monitor/unlock"' in page

    wrong = client.post("/monitor/unlock", data={"token": "nope"}, follow_redirects=False)
    assert wrong.headers["location"] == "/monitor?unlock=failed"
    assert "didn't match" in client.get(wrong.headers["location"]).text
    assert started == []

    by_header = client.post("/api/runs", headers={"X-Admin-Token": "s3cret"})
    assert by_header.status_code == 202
    with SessionLocal() as session:
        session.get_one(CrawlRun, by_header.json()["run_id"]).status = RunStatus.success
        session.commit()

    unlocked = client.post("/monitor/unlock", data={"token": "s3cret"}, follow_redirects=False)
    assert unlocked.status_code == 303
    assert "httponly" in unlocked.headers["set-cookie"].lower()
    assert "samesite=strict" in unlocked.headers["set-cookie"].lower()
    unlocked_page = client.get("/monitor").text
    assert 'hx-post="/api/runs"' in unlocked_page
    assert 'action="/monitor/unlock"' not in unlocked_page
    assert client.post("/api/runs").status_code == 202


def test_monitor_shows_daily_llm_usage(client: TestClient) -> None:
    with SessionLocal() as session:
        session.add(CrawlRun(status=RunStatus.success, llm_calls=7))
        session.commit()

    assert "7 of 400 daily LLM calls used" in client.get("/monitor").text
    assert client.get("/api/runs/latest").json()["llm_calls"] == 7


def test_button_and_unlock_form_never_show_together(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 3s refresh renders both from one request, so a stale page corrects itself."""
    monkeypatch.setattr(main.settings, "admin_token", "s3cret")
    page = client.get("/monitor")
    assert page.headers["cache-control"] == "no-store"
    assert 'id="admin-unlock" hx-preserve' in page.text

    locked = client.get("/api/runs/latest", headers=HTMX).text
    assert 'action="/monitor/unlock"' in locked and 'hx-post="/api/runs"' not in locked

    client.post("/monitor/unlock", data={"token": "s3cret"}, follow_redirects=False)
    unlocked = client.get("/api/runs/latest", headers=HTMX).text
    assert 'hx-post="/api/runs"' in unlocked and 'action="/monitor/unlock"' not in unlocked
