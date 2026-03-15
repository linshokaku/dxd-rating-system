import os
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from bot.config import Settings


def get_database_url() -> str:
    if "DATABASE_URL" in os.environ:
        return os.environ["DATABASE_URL"]
    return Settings().database_url


@pytest.fixture(scope="session")
def engine() -> Generator[Engine]:
    database_url = get_database_url()
    engine = create_engine(database_url, pool_pre_ping=True)

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Generator[Session]:
    connection: Connection = engine.connect()
    session_factory = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
    session = session_factory()

    try:
        yield session
    finally:
        session.close()
        truncate_all_tables(connection)
        connection.close()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def truncate_all_tables(connection: Connection) -> None:
    table_names = (
        connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        .scalars()
        .all()
    )
    table_names = [table_name for table_name in table_names if table_name != "alembic_version"]
    if not table_names:
        return

    quoted_table_names = ", ".join(f'"{table_name}"' for table_name in table_names)
    connection.execute(text(f"TRUNCATE TABLE {quoted_table_names} RESTART IDENTITY CASCADE"))
    connection.commit()
