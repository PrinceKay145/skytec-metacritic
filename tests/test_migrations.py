from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from app.db import alembic_config, engine
from app.models import Base


def test_migrations_build_the_same_schema_as_the_models() -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    command.upgrade(alembic_config(), "head")

    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_downgrade_removes_everything() -> None:
    command.downgrade(alembic_config(), "base")
    with engine.connect() as connection:
        tables = connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
        assert [name for (name,) in tables] == ["alembic_version"]
    command.upgrade(alembic_config(), "head")
