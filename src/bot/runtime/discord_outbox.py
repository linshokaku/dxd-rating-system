from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import NoReturn, Protocol, cast

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bot.db.session import session_scope
from bot.models import MatchQueueEntry, OutboxEventType
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
    mention_discord_user_id: int


class DiscordOutboxEventPublisher:
    def __init__(
        self,
        client: DiscordChannelClient,
        session_factory: sessionmaker[Session],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
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
        destinations = await asyncio.to_thread(self._resolve_destinations, event)
        message_body = self._message_body_for_event_type(event.event_type)

        for destination in destinations:
            channel = await self._resolve_channel(destination.channel_id)
            self._log_channel_guild_mismatch(destination=destination, channel=channel, event=event)
            await channel.send(
                self._render_message(
                    mention_discord_user_id=destination.mention_discord_user_id,
                    message_body=message_body,
                ),
                allowed_mentions=self._allowed_mentions,
            )

    def _resolve_destinations(
        self, event: PendingOutboxEvent
    ) -> tuple[NotificationDestination, ...]:
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
                return (self._build_destination(entry=entry, event=event),)

        if event.event_type == OutboxEventType.MATCH_CREATED:
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

            unique_destinations: dict[tuple[int, int], NotificationDestination] = {}
            for queue_entry_id in queue_entry_ids:
                entry = entries_by_id[queue_entry_id]
                destination = self._build_destination(entry=entry, event=event)
                unique_destinations.setdefault(
                    (destination.channel_id, destination.mention_discord_user_id),
                    destination,
                )

            return tuple(unique_destinations.values())

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
    ) -> NotificationDestination:
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

        return NotificationDestination(
            channel_id=entry.notification_channel_id,
            guild_id=entry.notification_guild_id,
            mention_discord_user_id=entry.notification_mention_discord_user_id,
        )

    def _message_body_for_event_type(self, event_type: OutboxEventType) -> str:
        if event_type == OutboxEventType.PRESENCE_REMINDER:
            return PRESENCE_REMINDER_NOTIFICATION_MESSAGE
        if event_type == OutboxEventType.QUEUE_EXPIRED:
            return QUEUE_EXPIRED_NOTIFICATION_MESSAGE
        if event_type == OutboxEventType.MATCH_CREATED:
            return MATCH_CREATED_NOTIFICATION_MESSAGE
        self._raise_publish_error(f"Unsupported outbox event type: {event_type}")

    def _render_message(self, *, mention_discord_user_id: int, message_body: str) -> str:
        return f"<@{mention_discord_user_id}> {message_body}"

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
