from logging.config import fileConfig

from alembic import context

from app.db import engine
from app.models import Base

config = context.config
# the app runs migrations on startup and keeps its own logging setup
if config.config_file_name and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name)

if context.is_offline_mode():
    raise SystemExit("Offline SQL generation isn't set up; run against a database.")

with engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        # SQLite can't ALTER most things, so alembic rebuilds the table instead
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()
