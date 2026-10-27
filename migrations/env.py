import sys
from pathlib import Path

# Ensure backend directory is in sys.path
_backend_dir = str(Path(__file__).resolve().parents[1] / "backend")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context
from app.core.config import settings
from app.models.entities import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    url = f"sqlite:///{settings.db_file.as_posix()}"
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine

    from app.core.db import get_engine

    cfg_url = config.get_main_option("sqlalchemy.url")
    if cfg_url and cfg_url != "sqlite:///./data/dpm.db":
        connectable = create_engine(cfg_url, poolclass=pool.NullPool)
    else:
        connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()



if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
