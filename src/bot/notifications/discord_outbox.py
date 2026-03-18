from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import NoReturn, Protocol, cast

import discord

from bot.constants import format_discord_user_mention
from bot.models import OutboxEventType
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
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
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
        notification = await asyncio.to_thread(self._resolve_notification, event)
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

    def _resolve_notification(self, event: PendingOutboxEvent) -> ResolvedNotification:
        return ResolvedNotification(
            destination=self._require_destination(event.payload),
            content=self._render_content(event.event_type, event.payload),
        )

    async def _resolve_channel(self, channel_id: int) -> DiscordSendableChannel:
        channel = self._as_sendable_channel(self.client.get_channel(channel_id))
        if channel is not None:
            return channel

        fetched_channel = self._as_sendable_channel(await self.client.fetch_channel(channel_id))
        if fetched_channel is None:
            self._raise_publish_error(f"Fetched Discord channel is not sendable: {channel_id}")
        return fetched_channel

    def _render_content(
        self,
        event_type: OutboxEventType,
        payload: dict[str, object],
    ) -> str:
        if event_type == OutboxEventType.PRESENCE_REMINDER:
            return self._render_presence_reminder_content(payload)

        if event_type == OutboxEventType.QUEUE_EXPIRED:
            return self._render_queue_expired_content(payload)

        if event_type == OutboxEventType.MATCH_CREATED:
            return self._render_match_created_content(payload)

        self._raise_publish_error(f"Unsupported outbox event type: {event_type}")

    def _render_presence_reminder_content(self, payload: dict[str, object]) -> str:
        mention_discord_user_id = self._require_payload_int(payload, "mention_discord_user_id")
        mention_text = format_discord_user_mention(mention_discord_user_id)
        return f"{mention_text} {PRESENCE_REMINDER_NOTIFICATION_MESSAGE}"

    def _render_queue_expired_content(self, payload: dict[str, object]) -> str:
        mention_discord_user_id = self._require_payload_int(payload, "mention_discord_user_id")
        mention_text = format_discord_user_mention(mention_discord_user_id)
        return f"{mention_text} {QUEUE_EXPIRED_NOTIFICATION_MESSAGE}"

    def _render_match_created_content(self, payload: dict[str, object]) -> str:
        team_a_discord_user_ids = self._require_payload_int_list(
            payload,
            "team_a_discord_user_ids",
        )
        team_b_discord_user_ids = self._require_payload_int_list(
            payload,
            "team_b_discord_user_ids",
        )
        if not team_a_discord_user_ids or not team_b_discord_user_ids:
            self._raise_publish_error(
                "match_created payload team discord user ids must not be empty"
            )

        team_a_display_labels = [
            format_discord_user_mention(discord_user_id)
            for discord_user_id in team_a_discord_user_ids
        ]
        team_b_display_labels = [
            format_discord_user_mention(discord_user_id)
            for discord_user_id in team_b_discord_user_ids
        ]
        indented_team_a_display_labels = [f"    {label}" for label in team_a_display_labels]
        indented_team_b_display_labels = [f"    {label}" for label in team_b_display_labels]
        return "\n".join(
            [
                MATCH_CREATED_NOTIFICATION_MESSAGE,
                "Team A",
                *indented_team_a_display_labels,
                "Team B",
                *indented_team_b_display_labels,
            ]
        )

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

    def _require_destination(self, payload: dict[str, object]) -> NotificationDestination:
        value = payload.get("destination")
        if not isinstance(value, dict):
            self._raise_publish_error(
                f"Outbox payload 'destination' must be a dict[str, int | None]: {value!r}"
            )

        channel_id = value.get("channel_id")
        guild_id = value.get("guild_id")
        if not isinstance(channel_id, int):
            self._raise_publish_error(
                f"Outbox payload destination.channel_id must be an int: {channel_id!r}"
            )
        if guild_id is not None and not isinstance(guild_id, int):
            self._raise_publish_error(
                f"Outbox payload destination.guild_id must be an int | None: {guild_id!r}"
            )

        return NotificationDestination(
            channel_id=channel_id,
            guild_id=cast(int | None, guild_id),
        )

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
