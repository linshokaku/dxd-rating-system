import pytest

from dxd_rating.platform.config.common import DATABASE_URL_FORMAT_ERROR
from dxd_rating.platform.db.session import normalize_database_url_for_sqlalchemy


def test_normalize_database_url_for_sqlalchemy_converts_railway_url() -> None:
    assert normalize_database_url_for_sqlalchemy("postgresql://example") == (
        "postgresql+psycopg://example"
    )


def test_normalize_database_url_for_sqlalchemy_rejects_sqlalchemy_url() -> None:
    with pytest.raises(ValueError, match=DATABASE_URL_FORMAT_ERROR):
        normalize_database_url_for_sqlalchemy("postgresql+psycopg://example")


def test_normalize_database_url_for_sqlalchemy_rejects_other_scheme() -> None:
    with pytest.raises(ValueError, match=DATABASE_URL_FORMAT_ERROR):
        normalize_database_url_for_sqlalchemy("sqlite:///tmp/test.db")
