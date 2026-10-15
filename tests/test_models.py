from app.db import Base, SessionLocal, engine
from app.models import ERROR_LOG_SIZE, ERROR_MESSAGE_CHARS, CrawlRun


def test_record_error_keeps_count_and_last_entries() -> None:
    Base.metadata.create_all(engine)
    run = CrawlRun()
    for n in range(ERROR_LOG_SIZE + 5):
        run.record_error("product", f"https://example.test/{n}", f"boom {n}" + "x" * 1000)

    with SessionLocal() as session:
        session.add(run)
        session.commit()
        saved = session.get(CrawlRun, run.id)
        assert saved is not None
        assert saved.errors == ERROR_LOG_SIZE + 5
        assert len(saved.error_log) == ERROR_LOG_SIZE
        assert saved.error_log[0]["url"] == "https://example.test/5"
        assert saved.error_log[-1]["message"].startswith(f"boom {ERROR_LOG_SIZE + 4}")
        assert len(saved.error_log[-1]["message"]) == ERROR_MESSAGE_CHARS
