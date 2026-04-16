from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.platform.config.common import RAILWAY_DATABASE_URL_PREFIX, validate_database_url

SQLALCHEMY_DATABASE_URL_PREFIX = "postgresql+psycopg://"


def normalize_database_url_for_sqlalchemy(database_url: str) -> str:
    validated_database_url = validate_database_url(database_url)
    return SQLALCHEMY_DATABASE_URL_PREFIX + validated_database_url.removeprefix(
        RAILWAY_DATABASE_URL_PREFIX
    )


def create_db_engine(database_url: str) -> Engine:
    return create_engine(
        normalize_database_url_for_sqlalchemy(database_url),
        pool_pre_ping=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
