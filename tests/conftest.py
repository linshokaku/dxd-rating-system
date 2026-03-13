import os
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine
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
    transaction = connection.begin()
    session_factory = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
    session = session_factory()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
