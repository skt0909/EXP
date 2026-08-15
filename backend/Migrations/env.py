import os
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No SQLAlchemy ORM models exist anywhere in this project -- every module
# uses raw SQL / SQLAlchemy Core (see Game_logic/db_utils.py and friends).
# Autogenerate has no target to diff against, so it isn't used here; every
# migration (including the baseline) is hand-authored. See Migrations/README.md.
target_metadata = None


def _database_url() -> str:
    """Same .env-discovery pattern as every db_utils.py in this project:
    walk up from this file looking for a .env, then read DATABASE_URL.
    DATABASE_URL in the environment (if already set) takes precedence,
    so a temporary override for a verification/test database just works.
    """
    if not os.getenv("DATABASE_URL"):
        search_dirs = [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents)
        for d in search_dirs:
            candidate = d / ".env"
            if candidate.is_file():
                load_dotenv(candidate)
                break
        else:
            load_dotenv()

    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set (checked environment and .env files).")
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema="public",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema="public",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
