from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn, Protocol, cast

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bot.constants import is_dummy_discord_user_id
from bot.db.session import session_scope
from bot.models import (
    MatchParticipant,
    MatchParticipantTeam,
    MatchQueueEntry,
    MatchResultType,
    OutboxEventType,
)
from bot.runtime.outbox import PendingOutboxEvent
from bot.services import (
    MATCH_CREATED_NOTIFICATION_MESSAGE,
    PRESENCE_REMINDER_NOTIFICATION_MESSAGE,
    QUEUE_EXPIRED_NOTIFICATION_MESSAGE,
)


class DiscordSendableChannel(Protocol):
    id: int

    async def send(
        self,
        content: str,
        *,
        allowed_mentions: discord.AllowedMentions,
    ) -> object: ...


class DiscordChannelClient(Protocol):
    def get_channel(self, channel_id: int) -> object | None: ...

    async def fetch_channel(self, channel_id: int) -> object: ...


@dataclass(frozen=True, slots=True)
class NotificationDestination:
    channel_id: int
    guild_id: int | None


@dataclass(frozen=True, slots=True)
class ResolvedNotification:
    destination: NotificationDestination
    content: str


class DiscordOutboxEventPublisher:
    def __init__(
        self,
        client: DiscordChannelClient,
        session_factory: sessionmaker[Session],
        *,
        super_admin_user_ids: frozenset[int] = frozenset(),
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
        self.super_admin_user_ids = super_admin_user_ids
        self.logger = logger or logging.getLogger(__name__)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._allowed_mentions = discord.AllowedMentions(
            users=True,
            roles=False,
            everyone=False,
            replied_user=False,
        )

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("DiscordOutboxEventPublisher loop is already bound")
        self._loop = loop

    def publish(self, event: PendingOutboxEvent) -> None:
        if self._is_running_on_bound_loop():
            raise RuntimeError(
                "DiscordOutboxEventPublisher.publish must not run on the bound event loop"
            )

        future = asyncio.run_coroutine_threadsafe(self._publish_event(event), self._require_loop())
        future.result()

    async def _publish_event(self, event: PendingOutboxEvent) -> None:
        notifications = await asyncio.to_thread(self._resolve_notifications, event)

        for notification in notifications:
            channel = await self._resolve_channel(notification.destination.channel_id)
            self._log_channel_guild_mismatch(
                destination=notification.destination,
                channel=channel,
                event=event,
            )
            await channel.send(
                notification.content,
                allowed_mentions=self._allowed_mentions,
            )

    def _resolve_notifications(self, event: PendingOutboxEvent) -> tuple[ResolvedNotification, ...]:
        if event.event_type in (
            OutboxEventType.PRESENCE_REMINDER,
            OutboxEventType.QUEUE_EXPIRED,
        ):
            queue_entry_id = self._require_payload_int(event.payload, "queue_entry_id")
            with session_scope(self.session_factory) as session:
                entry = session.get(MatchQueueEntry, queue_entry_id)
                if entry is None:
                    self._raise_publish_error(
                        f"Queue entry not found for outbox event id={event.id}: {queue_entry_id}"
                    )
                destination, mention_discord_user_id = self._build_destination(
                    entry=entry,
                    event=event,
                )
                message_body = self._message_body_for_event_type(event.event_type)
                return (
                    ResolvedNotification(
                        destination=destination,
                        content=self._render_prefixed_message(
                            mention_discord_user_id=mention_discord_user_id,
                            message_body=message_body,
                        ),
                    ),
                )

        if event.event_type == OutboxEventType.MATCH_CREATED:
            notification_kind = event.payload.get("notification_kind")
            if isinstance(notification_kind, str):
                return self._resolve_match_progress_notifications(event)
            return self._resolve_match_created_notifications(event)

        self._raise_publish_error(f"Unsupported outbox event type: {event.event_type}")

    async def _resolve_channel(self, channel_id: int) -> DiscordSendableChannel:
        channel = self._as_sendable_channel(self.client.get_channel(channel_id))
        if channel is not None:
            return channel

        fetched_channel = self._as_sendable_channel(await self.client.fetch_channel(channel_id))
        if fetched_channel is None:
            self._raise_publish_error(f"Fetched Discord channel is not sendable: {channel_id}")
        return fetched_channel

    def _build_destination(
        self,
        *,
        entry: MatchQueueEntry,
        event: PendingOutboxEvent,
    ) -> tuple[NotificationDestination, int]:
        if entry.notification_channel_id is None:
            self._raise_publish_error(
                "notification_channel_id is missing for outbox "
                f"event id={event.id} queue_entry_id={entry.id}"
            )
        if entry.notification_mention_discord_user_id is None:
            self._raise_publish_error(
                "notification_mention_discord_user_id is missing for outbox "
                f"event id={event.id} queue_entry_id={entry.id}"
            )

        return (
            NotificationDestination(
                channel_id=entry.notification_channel_id,
                guild_id=entry.notification_guild_id,
            ),
            entry.notification_mention_discord_user_id,
        )

    def _message_body_for_event_type(self, event_type: OutboxEventType) -> str:
        if event_type == OutboxEventType.PRESENCE_REMINDER:
            return PRESENCE_REMINDER_NOTIFICATION_MESSAGE
        if event_type == OutboxEventType.QUEUE_EXPIRED:
            return QUEUE_EXPIRED_NOTIFICATION_MESSAGE
        if event_type == OutboxEventType.MATCH_CREATED:
            return MATCH_CREATED_NOTIFICATION_MESSAGE
        self._raise_publish_error(f"Unsupported outbox event type: {event_type}")

    def _render_prefixed_message(self, *, mention_discord_user_id: int, message_body: str) -> str:
        if is_dummy_discord_user_id(mention_discord_user_id):
            return f"<dummy_{mention_discord_user_id}> {message_body}"
        return f"<@{mention_discord_user_id}> {message_body}"

    def _render_match_created_message(
        self,
        *,
        match_id: int,
        team_a_display_labels: list[str],
        team_b_display_labels: list[str],
    ) -> str:
        indented_team_a_display_labels = [f"    {label}" for label in team_a_display_labels]
        indented_team_b_display_labels = [f"    {label}" for label in team_b_display_labels]
        return "\n".join(
            [
                MATCH_CREATED_NOTIFICATION_MESSAGE,
                f"Match ID: {match_id}",
                "5分以内に /match_parent で親に立候補してください。",
                "Team A",
                *indented_team_a_display_labels,
                "Team B",
                *indented_team_b_display_labels,
            ]
        )

    def _build_display_user_ids_by_player_id(
        self,
        *,
        entries: Sequence[MatchQueueEntry],
        event: PendingOutboxEvent,
    ) -> dict[int, int]:
        display_user_ids_by_player_id: dict[int, int] = {}
        for entry in entries:
            display_user_id = entry.notification_mention_discord_user_id
            if display_user_id is None:
                self._raise_publish_error(
                    "notification_mention_discord_user_id is missing for match_created "
                    f"event id={event.id} queue_entry_id={entry.id}"
                )
            display_user_ids_by_player_id[entry.player_id] = display_user_id
        return display_user_ids_by_player_id

    def _build_team_display_labels(
        self,
        *,
        player_ids: list[int],
        display_user_ids_by_player_id: dict[int, int],
        event: PendingOutboxEvent,
    ) -> list[str]:
        display_labels: list[str] = []
        for player_id in player_ids:
            display_user_id = display_user_ids_by_player_id.get(player_id)
            if display_user_id is None:
                self._raise_publish_error(
                    "Player is missing from match_created payload resolution "
                    f"event id={event.id} player_id={player_id}"
                )
            display_labels.append(self._format_participant_label(display_user_id))
        return display_labels

    def _resolve_match_created_notifications(
        self,
        event: PendingOutboxEvent,
    ) -> tuple[ResolvedNotification, ...]:
        queue_entry_ids = self._require_payload_int_list(event.payload, "queue_entry_ids")
        if not queue_entry_ids:
            self._raise_publish_error(
                f"queue_entry_ids is empty for match_created outbox event id={event.id}"
            )

        with session_scope(self.session_factory) as session:
            entries = session.scalars(
                select(MatchQueueEntry)
                .where(MatchQueueEntry.id.in_(queue_entry_ids))
                .order_by(MatchQueueEntry.id)
            ).all()

        entries_by_id = {entry.id: entry for entry in entries}
        missing_queue_entry_ids = [
            queue_entry_id
            for queue_entry_id in queue_entry_ids
            if queue_entry_id not in entries_by_id
        ]
        if missing_queue_entry_ids:
            self._raise_publish_error(
                "Queue entries not found for match_created outbox "
                f"event id={event.id}: {missing_queue_entry_ids}"
            )

        match_id = self._require_payload_int(event.payload, "match_id")
        teams = self._require_payload_teams(event.payload)
        display_user_ids_by_player_id = self._build_display_user_ids_by_player_id(
            entries=entries,
            event=event,
        )
        message_body = self._render_match_created_message(
            match_id=match_id,
            team_a_display_labels=self._build_team_display_labels(
                player_ids=teams[MatchParticipantTeam.TEAM_A.value],
                display_user_ids_by_player_id=display_user_ids_by_player_id,
                event=event,
            ),
            team_b_display_labels=self._build_team_display_labels(
                player_ids=teams[MatchParticipantTeam.TEAM_B.value],
                display_user_ids_by_player_id=display_user_ids_by_player_id,
                event=event,
            ),
        )

        unique_notifications: dict[int, ResolvedNotification] = {}
        for queue_entry_id in queue_entry_ids:
            entry = entries_by_id[queue_entry_id]
            destination, _mention_discord_user_id = self._build_destination(
                entry=entry,
                event=event,
            )
            unique_notifications.setdefault(
                destination.channel_id,
                ResolvedNotification(destination=destination, content=message_body),
            )

        return tuple(unique_notifications.values())

    def _resolve_match_progress_notifications(
        self,
        event: PendingOutboxEvent,
    ) -> tuple[ResolvedNotification, ...]:
        match_id = self._require_payload_int(event.payload, "match_id")
        with session_scope(self.session_factory) as session:
            participants = session.scalars(
                select(MatchParticipant)
                .where(MatchParticipant.match_id == match_id)
                .order_by(MatchParticipant.team, MatchParticipant.slot, MatchParticipant.id)
            ).all()

            if not participants:
                self._raise_publish_error(
                    f"Match participants not found for outbox event id={event.id}: {match_id}"
                )

            destinations_by_channel_id: dict[int, NotificationDestination] = {}
            display_user_ids_by_player_id: dict[int, int] = {}

            for participant in participants:
                entry = participant.queue_entry
                if entry is None:
                    self._raise_publish_error(
                        "queue_entry is missing for match notification "
                        f"event id={event.id} participant_id={participant.id}"
                    )
                destination, mention_discord_user_id = self._build_destination(
                    entry=entry,
                    event=event,
                )
                destinations_by_channel_id.setdefault(destination.channel_id, destination)
                display_user_ids_by_player_id[participant.player_id] = mention_discord_user_id

        message_body = self._render_match_progress_message(
            event=event,
            display_user_ids_by_player_id=display_user_ids_by_player_id,
        )
        return tuple(
            ResolvedNotification(destination=destination, content=message_body)
            for destination in destinations_by_channel_id.values()
        )

    def _render_match_progress_message(
        self,
        *,
        event: PendingOutboxEvent,
        display_user_ids_by_player_id: dict[int, int],
    ) -> str:
        match_id = self._require_payload_int(event.payload, "match_id")
        notification_kind = self._require_payload_str(event.payload, "notification_kind")

        if notification_kind == "match_parent_decided":
            parent_player_id = self._require_payload_int(event.payload, "parent_player_id")
            report_open_at = self._require_payload_str(event.payload, "report_open_at")
            report_deadline_at = self._require_payload_str(event.payload, "report_deadline_at")
            parent_label = self._label_for_player_id(
                player_id=parent_player_id,
                display_user_ids_by_player_id=display_user_ids_by_player_id,
                event=event,
            )
            return "\n".join(
                [
                    f"試合 {match_id} の親が決定しました。",
                    f"親: {parent_label}",
                    f"勝敗報告開始: {report_open_at}",
                    f"勝敗報告締切: {report_deadline_at}",
                ]
            )

        if notification_kind == "match_approval_started":
            approval_deadline_at = self._require_payload_str(event.payload, "approval_deadline_at")
            provisional_result = self._require_match_result_type(
                event.payload,
                "provisional_result",
            )
            approval_target_player_ids = self._require_payload_int_list(
                event.payload,
                "approval_target_player_ids",
            )
            result_line = self._render_match_result_label(provisional_result)
            target_labels = [
                self._label_for_player_id(
                    player_id=player_id,
                    display_user_ids_by_player_id=display_user_ids_by_player_id,
                    event=event,
                )
                for player_id in approval_target_player_ids
            ]
            lines = [
                f"試合 {match_id} の仮決定結果: {result_line}",
                f"承認期限: {approval_deadline_at}",
            ]
            if target_labels:
                lines.append(f"承認対象: {', '.join(target_labels)}")
                lines.append("承認できない場合は証拠を提示して admin へ連絡してください。")
            else:
                lines.append("承認対象はいません。")
            return "\n".join(lines)

        if notification_kind == "match_finalized":
            final_result = self._require_match_result_type(event.payload, "final_result")
            finalized_at = self._require_payload_str(event.payload, "finalized_at")
            return "\n".join(
                [
                    f"試合 {match_id} の結果が確定しました。",
                    f"結果: {self._render_match_result_label(final_result)}",
                    f"確定時刻: {finalized_at}",
                ]
            )

        if notification_kind == "match_admin_review_required":
            final_result = self._require_match_result_type(event.payload, "final_result")
            reasons = self._require_payload_str_list(event.payload, "reasons")
            admin_prefix = self._render_admin_mentions()
            lines = []
            if admin_prefix:
                lines.append(admin_prefix)
            lines.extend(
                [
                    f"試合 {match_id} は admin 確認が必要です。",
                    f"現在結果: {self._render_match_result_label(final_result)}",
                    f"理由: {', '.join(reasons) if reasons else 'manual_check_required'}",
                ]
            )
            return "\n".join(lines)

        self._raise_publish_error(f"Unsupported outbox event type: {event.event_type}")

    def _label_for_player_id(
        self,
        *,
        player_id: int,
        display_user_ids_by_player_id: dict[int, int],
        event: PendingOutboxEvent,
    ) -> str:
        display_user_id = display_user_ids_by_player_id.get(player_id)
        if display_user_id is None:
            self._raise_publish_error(
                "Player is missing from match payload resolution "
                f"event id={event.id} player_id={player_id}"
            )
        return self._format_participant_label(display_user_id)

    def _render_match_result_label(self, match_result: MatchResultType) -> str:
        if match_result == MatchResultType.TEAM_A_WIN:
            return "Team A の勝ち"
        if match_result == MatchResultType.TEAM_B_WIN:
            return "Team B の勝ち"
        if match_result == MatchResultType.DRAW:
            return "引き分け"
        return "無効試合"

    def _render_admin_mentions(self) -> str:
        if not self.super_admin_user_ids:
            return ""
        return " ".join(
            self._format_participant_label(user_id) for user_id in self.super_admin_user_ids
        )

    def _format_participant_label(self, discord_user_id: int) -> str:
        if is_dummy_discord_user_id(discord_user_id):
            return f"<dummy_{discord_user_id}>"
        return f"<@{discord_user_id}>"

    def _require_payload_teams(self, payload: dict[str, object]) -> dict[str, list[int]]:
        value = payload.get("teams")
        if not isinstance(value, dict):
            self._raise_publish_error(
                f"Outbox payload 'teams' must be a dict[str, list[int]]: {value!r}"
            )

        teams = {
            MatchParticipantTeam.TEAM_A.value: self._require_team_player_ids(
                value,
                MatchParticipantTeam.TEAM_A.value,
            ),
            MatchParticipantTeam.TEAM_B.value: self._require_team_player_ids(
                value,
                MatchParticipantTeam.TEAM_B.value,
            ),
        }
        return teams

    def _require_team_player_ids(
        self,
        teams: dict[object, object],
        team_name: str,
    ) -> list[int]:
        value = teams.get(team_name)
        if not isinstance(value, list) or any(not isinstance(item, int) for item in value):
            self._raise_publish_error(
                f"Outbox payload teams['{team_name}'] must be a list[int]: {value!r}"
            )
        return cast(list[int], value)

    def _log_channel_guild_mismatch(
        self,
        *,
        destination: NotificationDestination,
        channel: DiscordSendableChannel,
        event: PendingOutboxEvent,
    ) -> None:
        if destination.guild_id is None:
            return

        channel_guild = getattr(channel, "guild", None)
        actual_guild_id = getattr(channel_guild, "id", None)
        if actual_guild_id is None or actual_guild_id == destination.guild_id:
            return

        self.logger.warning(
            "Discord notification channel guild mismatch event_id=%s channel_id=%s "
            "expected_guild_id=%s actual_guild_id=%s",
            event.id,
            destination.channel_id,
            destination.guild_id,
            actual_guild_id,
        )

    def _require_payload_int(self, payload: dict[str, object], key: str) -> int:
        value = payload.get(key)
        if not isinstance(value, int):
            self._raise_publish_error(f"Outbox payload '{key}' must be an int: {value!r}")
        return value

    def _require_payload_int_list(self, payload: dict[str, object], key: str) -> list[int]:
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, int) for item in value):
            self._raise_publish_error(f"Outbox payload '{key}' must be a list[int]: {value!r}")
        return cast(list[int], value)

    def _require_payload_str(self, payload: dict[str, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            self._raise_publish_error(f"Outbox payload '{key}' must be a str: {value!r}")
        return value

    def _require_payload_str_list(self, payload: dict[str, object], key: str) -> list[str]:
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            self._raise_publish_error(f"Outbox payload '{key}' must be a list[str]: {value!r}")
        return cast(list[str], value)

    def _require_match_result_type(self, payload: dict[str, object], key: str) -> MatchResultType:
        value = self._require_payload_str(payload, key)
        try:
            return MatchResultType(value)
        except ValueError as exc:
            self._raise_publish_error(
                f"Outbox payload '{key}' must be a valid match result type: {value!r}"
            )
            raise RuntimeError("unreachable") from exc

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("DiscordOutboxEventPublisher loop is not bound")
        return self._loop

    def _is_running_on_bound_loop(self) -> bool:
        if self._loop is None:
            return False

        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _as_sendable_channel(self, channel: object | None) -> DiscordSendableChannel | None:
        if channel is None:
            return None

        send = getattr(channel, "send", None)
        channel_id = getattr(channel, "id", None)
        if not callable(send) or not isinstance(channel_id, int):
            return None

        return cast(DiscordSendableChannel, channel)

    def _raise_publish_error(self, message: str) -> NoReturn:
        self.logger.error(message)
        raise ValueError(message)
