"""Alembic environment — uses the app's SQLCipher-aware engine.

Building the engine through ``app.services.db.create_db_engine`` ensures the
``PRAGMA key`` is issued first, so migrations work against encrypted databases.
"""

from logging.config import fileConfig

from alembic import context

from app.config import settings
from app.models.base import Base
import app.models  # noqa: F401  (import side effect: registers all models)
from app.services.db import create_db_engine

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite: emulate ALTER via batch table rebuilds
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_db_engine(settings)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # required for SQLite column changes
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
