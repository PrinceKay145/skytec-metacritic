import os
import tempfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["SCHEDULER_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def fresh_db() -> None:
    from app.db import engine
    from app.models import Base  # importing models registers every table on Base

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
