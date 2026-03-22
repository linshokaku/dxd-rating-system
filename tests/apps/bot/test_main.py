from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.apps.bot.main import initialize_seasons
from dxd_rating.platform.db.models import Season


def test_initialize_seasons_creates_active_and_upcoming_seasons(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    initialize_seasons(session_factory)
    session.expire_all()
    seasons = session.scalars(select(Season).order_by(Season.start_at, Season.id)).all()

    assert len(seasons) == 2
    assert seasons[0].end_at == seasons[1].start_at
