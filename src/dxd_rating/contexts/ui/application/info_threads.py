from __future__ import annotations

from enum import StrEnum

from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.platform.db.models import PlayerInfoThreadBinding
from dxd_rating.platform.db.session import session_scope


class InfoThreadCommandName(StrEnum):
    LEADERBOARD = "leaderboard"
    LEADERBOARD_SEASON = "leaderboard_season"
    PLAYER_INFO = "player_info"
    PLAYER_INFO_SEASON = "player_info_season"


class InfoThreadBindingService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def get_latest_thread_channel_id(self, player_id: int) -> int | None:
        with session_scope(self.session_factory) as session:
            binding = session.get(PlayerInfoThreadBinding, player_id)
            if binding is None:
                return None
            return binding.thread_channel_id

    def upsert_latest_thread_channel_id(
        self,
        *,
        player_id: int,
        thread_channel_id: int,
    ) -> PlayerInfoThreadBinding:
        with session_scope(self.session_factory) as session:
            binding = session.get(PlayerInfoThreadBinding, player_id)
            if binding is None:
                binding = PlayerInfoThreadBinding(
                    player_id=player_id,
                    thread_channel_id=thread_channel_id,
                )
                session.add(binding)
            else:
                binding.thread_channel_id = thread_channel_id

            session.flush()
            return binding
