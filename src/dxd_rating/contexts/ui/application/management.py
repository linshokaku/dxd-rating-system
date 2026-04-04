from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.platform.db.models import ManagedUiChannel, ManagedUiType
from dxd_rating.platform.db.session import session_scope

REGISTER_PANEL_RECOMMENDED_CHANNEL_NAME = "レート戦はこちらから"
MATCHMAKING_CHANNEL_RECOMMENDED_CHANNEL_NAME = "レート戦マッチング"
MATCHMAKING_NEWS_CHANNEL_RECOMMENDED_CHANNEL_NAME = "レート戦マッチ速報"
INFO_CHANNEL_RECOMMENDED_CHANNEL_NAME = "レート戦情報"
SYSTEM_ANNOUNCEMENTS_CHANNEL_RECOMMENDED_CHANNEL_NAME = "レート戦アナウンス"
ADMIN_CONTACT_CHANNEL_RECOMMENDED_CHANNEL_NAME = "運営連絡・フィードバック"
REGISTERED_PLAYER_ROLE_NAME = "レート戦参加者"


@dataclass(frozen=True, slots=True)
class ManagedUiDefinition:
    ui_type: ManagedUiType
    recommended_channel_name: str
    singleton: bool
    requires_registered_player_role: bool
    installs_persistent_view: bool


MANAGED_UI_DEFINITIONS = {
    ManagedUiType.REGISTER_PANEL: ManagedUiDefinition(
        ui_type=ManagedUiType.REGISTER_PANEL,
        recommended_channel_name=REGISTER_PANEL_RECOMMENDED_CHANNEL_NAME,
        singleton=True,
        requires_registered_player_role=False,
        installs_persistent_view=True,
    ),
    ManagedUiType.MATCHMAKING_CHANNEL: ManagedUiDefinition(
        ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
        recommended_channel_name=MATCHMAKING_CHANNEL_RECOMMENDED_CHANNEL_NAME,
        singleton=True,
        requires_registered_player_role=True,
        installs_persistent_view=True,
    ),
    ManagedUiType.MATCHMAKING_NEWS_CHANNEL: ManagedUiDefinition(
        ui_type=ManagedUiType.MATCHMAKING_NEWS_CHANNEL,
        recommended_channel_name=MATCHMAKING_NEWS_CHANNEL_RECOMMENDED_CHANNEL_NAME,
        singleton=True,
        requires_registered_player_role=True,
        installs_persistent_view=False,
    ),
    ManagedUiType.INFO_CHANNEL: ManagedUiDefinition(
        ui_type=ManagedUiType.INFO_CHANNEL,
        recommended_channel_name=INFO_CHANNEL_RECOMMENDED_CHANNEL_NAME,
        singleton=True,
        requires_registered_player_role=True,
        installs_persistent_view=True,
    ),
    ManagedUiType.SYSTEM_ANNOUNCEMENTS_CHANNEL: ManagedUiDefinition(
        ui_type=ManagedUiType.SYSTEM_ANNOUNCEMENTS_CHANNEL,
        recommended_channel_name=SYSTEM_ANNOUNCEMENTS_CHANNEL_RECOMMENDED_CHANNEL_NAME,
        singleton=True,
        requires_registered_player_role=True,
        installs_persistent_view=False,
    ),
    ManagedUiType.ADMIN_CONTACT_CHANNEL: ManagedUiDefinition(
        ui_type=ManagedUiType.ADMIN_CONTACT_CHANNEL,
        recommended_channel_name=ADMIN_CONTACT_CHANNEL_RECOMMENDED_CHANNEL_NAME,
        singleton=True,
        requires_registered_player_role=False,
        installs_persistent_view=False,
    ),
}
REQUIRED_MANAGED_UI_TYPES = (
    ManagedUiType.REGISTER_PANEL,
    ManagedUiType.MATCHMAKING_CHANNEL,
    ManagedUiType.MATCHMAKING_NEWS_CHANNEL,
    ManagedUiType.INFO_CHANNEL,
    ManagedUiType.SYSTEM_ANNOUNCEMENTS_CHANNEL,
    ManagedUiType.ADMIN_CONTACT_CHANNEL,
)


def get_managed_ui_definition(ui_type: ManagedUiType) -> ManagedUiDefinition:
    return MANAGED_UI_DEFINITIONS[ui_type]


def get_required_managed_ui_definitions() -> tuple[ManagedUiDefinition, ...]:
    return tuple(MANAGED_UI_DEFINITIONS[ui_type] for ui_type in REQUIRED_MANAGED_UI_TYPES)


class ManagedUiService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def list_managed_ui_channels(self) -> list[ManagedUiChannel]:
        with session_scope(self.session_factory) as session:
            return _list_managed_ui_channels(session)

    def get_managed_ui_channel_by_type(self, ui_type: ManagedUiType) -> ManagedUiChannel | None:
        with session_scope(self.session_factory) as session:
            return _get_managed_ui_channel_by_type(session, ui_type=ui_type)

    def create_managed_ui_channel(
        self,
        *,
        ui_type: ManagedUiType,
        channel_id: int,
        message_id: int,
        created_by_discord_user_id: int,
    ) -> ManagedUiChannel:
        with session_scope(self.session_factory) as session:
            return _create_managed_ui_channel(
                session,
                ui_type=ui_type,
                channel_id=channel_id,
                message_id=message_id,
                created_by_discord_user_id=created_by_discord_user_id,
            )

    def delete_managed_ui_channel_by_channel_id(self, channel_id: int) -> bool:
        return self.delete_managed_ui_channels_by_channel_ids([channel_id]) > 0

    def delete_managed_ui_channels_by_channel_ids(self, channel_ids: list[int]) -> int:
        if not channel_ids:
            return 0

        with session_scope(self.session_factory) as session:
            return _delete_managed_ui_channels_by_channel_ids(
                session,
                channel_ids=channel_ids,
            )


def _list_managed_ui_channels(session: Session) -> list[ManagedUiChannel]:
    return list(session.scalars(select(ManagedUiChannel).order_by(ManagedUiChannel.id.asc())).all())


def _get_managed_ui_channel_by_type(
    session: Session,
    *,
    ui_type: ManagedUiType,
) -> ManagedUiChannel | None:
    return session.scalar(
        select(ManagedUiChannel)
        .where(ManagedUiChannel.ui_type == ui_type)
        .order_by(ManagedUiChannel.id.asc())
    )


def _create_managed_ui_channel(
    session: Session,
    *,
    ui_type: ManagedUiType,
    channel_id: int,
    message_id: int,
    created_by_discord_user_id: int,
) -> ManagedUiChannel:
    managed_ui_channel = ManagedUiChannel(
        ui_type=ui_type,
        channel_id=channel_id,
        message_id=message_id,
        created_by_discord_user_id=created_by_discord_user_id,
    )
    session.add(managed_ui_channel)
    session.flush()
    return managed_ui_channel


def _delete_managed_ui_channels_by_channel_ids(
    session: Session,
    *,
    channel_ids: list[int],
) -> int:
    deleted_channel_ids = session.scalars(
        delete(ManagedUiChannel)
        .where(ManagedUiChannel.channel_id.in_(channel_ids))
        .returning(ManagedUiChannel.channel_id)
    ).all()
    return len(deleted_channel_ids)
