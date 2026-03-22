from sqlalchemy import Engine

import dxd_rating.platform.db.models  # noqa: F401
from dxd_rating.platform.db.models import Base


def create_tables(engine: Engine) -> None:
    Base.metadata.create_all(bind=engine)
