from sqlalchemy import Engine

import bot.models  # noqa: F401
from bot.models import Base


def create_tables(engine: Engine) -> None:
    Base.metadata.create_all(bind=engine)
