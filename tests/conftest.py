import os
import tempfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["SCHEDULER_ENABLED"] = "false"
# any test that forgets to inject a fake LLM fails fast instead of reaching a real one
os.environ["LLM_BASE_URL"] = "http://llm.invalid/v1"


@pytest.fixture(autouse=True)
def fresh_db() -> None:
    from alembic import command

    from app.db import alembic_config, engine
    from app.models import Base  # importing models registers every table on Base

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # marks the fresh schema as migrated, so app startup's upgrade is a no-op
    command.stamp(alembic_config(), "head")


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
