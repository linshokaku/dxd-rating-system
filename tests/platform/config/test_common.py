import pytest
from pydantic import ValidationError

from dxd_rating.platform.config.common import DATABASE_URL_FORMAT_ERROR, DatabaseSettings


def test_database_settings_accepts_railway_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")

    settings = DatabaseSettings()

    assert settings.database_url == "postgresql://example"


def test_database_settings_rejects_sqlalchemy_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example")

    with pytest.raises(ValidationError) as exc_info:
        DatabaseSettings()

    assert DATABASE_URL_FORMAT_ERROR in str(exc_info.value)


def test_database_settings_rejects_other_database_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp/test.db")

    with pytest.raises(ValidationError) as exc_info:
        DatabaseSettings()

    assert DATABASE_URL_FORMAT_ERROR in str(exc_info.value)
