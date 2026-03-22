from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import NoReturn, Protocol, cast

import discord

from dxd_rating.platform.db.models import OutboxEventType
from dxd_rating.platform.runtime.outbox import PendingOutboxEvent
from dxd_rating.services import (
    MATCH_ADMIN_REVIEW_REQUIRED_NOTIFICATION_MESSAGE,
    MATCH_APPROVAL_REQUESTED_NOTIFICATION_MESSAGE,
    MATCH_APPROVAL_STARTED_NOTIFICATION_MESSAGE,
    MATCH_AUTO_PENALTY_APPLIED_NOTIFICATION_MESSAGE,
    MATCH_CREATED_NOTIFICATION_MESSAGE,
    MATCH_FINALIZED_NOTIFICATION_MESSAGE,
    MATCH_PARENT_ASSIGNED_NOTIFICATION_MESSAGE,
    PRESENCE_REMINDER_NOTIFICATION_MESSAGE,
    QUEUE_EXPIRED_NOTIFICATION_MESSAGE,
)
from dxd_rating.shared.constants import format_discord_user_mention


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


@dataclass(frozen=True, slots=True)
class TeamRatingEntry:
    discord_user_id: int
    rating: float


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

        if event_type == OutboxEventType.MATCH_PARENT_ASSIGNED:
            return self._render_match_parent_assigned_content(payload)

        if event_type == OutboxEventType.MATCH_APPROVAL_REQUESTED:
            return self._render_match_approval_requested_content(payload)

        if event_type == OutboxEventType.MATCH_FINALIZED:
            return self._render_match_finalized_content(payload)

        if event_type == OutboxEventType.MATCH_ADMIN_REVIEW_REQUIRED:
            return self._render_match_admin_review_required_content(payload)

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
        match_id = self._require_payload_int(payload, "match_id")
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
                f"{MATCH_CREATED_NOTIFICATION_MESSAGE} match_id={match_id}",
                "Team A",
                *indented_team_a_display_labels,
                "Team B",
                *indented_team_b_display_labels,
            ]
        )

    def _render_match_parent_assigned_content(self, payload: dict[str, object]) -> str:
        match_id = self._require_payload_int(payload, "match_id")
        parent_discord_user_id = self._require_payload_int(payload, "parent_discord_user_id")
        report_open_at = self._require_payload_str(payload, "report_open_at")
        report_deadline_at = self._require_payload_str(payload, "report_deadline_at")
        return "\n".join(
            [
                f"{MATCH_PARENT_ASSIGNED_NOTIFICATION_MESSAGE} match_id={match_id}",
                f"親: {format_discord_user_mention(parent_discord_user_id)}",
                f"勝敗報告開始: {report_open_at}",
                f"勝敗報告締切: {report_deadline_at}",
            ]
        )

    def _render_match_approval_requested_content(self, payload: dict[str, object]) -> str:
        match_id = self._require_payload_int(payload, "match_id")
        phase_started = self._require_payload_bool_with_default(payload, "phase_started", False)
        provisional_result = self._require_payload_str(payload, "provisional_result")
        approval_deadline_at = self._require_payload_str(payload, "approval_deadline_at")
        if phase_started:
            return "\n".join(
                [
                    f"{MATCH_APPROVAL_STARTED_NOTIFICATION_MESSAGE} match_id={match_id}",
                    f"仮決定結果: {self._format_match_result_label(provisional_result)}",
                    f"承認締切: {approval_deadline_at}",
                ]
            )

        mention_discord_user_id = self._require_payload_int(payload, "mention_discord_user_id")
        mention_text = format_discord_user_mention(mention_discord_user_id)
        headline = (
            f"{mention_text} {MATCH_APPROVAL_REQUESTED_NOTIFICATION_MESSAGE} match_id={match_id}"
        )
        return "\n".join(
            [
                headline,
                f"仮決定結果: {self._format_match_result_label(provisional_result)}",
                f"承認締切: {approval_deadline_at}",
                "承認できない場合は証拠を提示したうえで admin へ連絡してください。",
            ]
        )

    def _render_match_finalized_content(self, payload: dict[str, object]) -> str:
        match_id = self._require_payload_int(payload, "match_id")
        auto_penalty_applied = self._require_payload_bool_with_default(
            payload,
            "auto_penalty_applied",
            False,
        )
        final_result = self._require_payload_str(payload, "final_result")
        if auto_penalty_applied:
            mention_discord_user_id = self._require_payload_int(payload, "mention_discord_user_id")
            penalty_type = self._require_payload_str(payload, "penalty_type")
            penalty_count = self._require_payload_int(payload, "penalty_count")
            mention_text = format_discord_user_mention(mention_discord_user_id)
            return "\n".join(
                [
                    f"{mention_text} {MATCH_AUTO_PENALTY_APPLIED_NOTIFICATION_MESSAGE} "
                    f"match_id={match_id}",
                    f"結果: {self._format_match_result_label(final_result)}",
                    f"ペナルティ: {self._format_penalty_type_label(penalty_type)}",
                    f"現在の累積: {penalty_count}",
                ]
            )

        finalized_by_admin = self._require_payload_bool(payload, "finalized_by_admin")
        lines = [
            f"{MATCH_FINALIZED_NOTIFICATION_MESSAGE} match_id={match_id}",
            f"結果: {self._format_match_result_label(final_result)}",
        ]
        if finalized_by_admin:
            lines.append("admin により結果が確定または更新されました。")
        else:
            rating_lines = self._render_team_rating_lines(payload)
            if rating_lines:
                lines.extend(["更新後レート", *rating_lines])
        return "\n".join(lines)

    def _render_match_admin_review_required_content(self, payload: dict[str, object]) -> str:
        match_id = self._require_payload_int(payload, "match_id")
        final_result = self._require_payload_str(payload, "final_result")
        reasons = self._require_payload_str_list(payload, "admin_review_reasons")
        admin_discord_user_ids = self._require_payload_int_list(payload, "admin_discord_user_ids")
        mention_prefix = " ".join(
            format_discord_user_mention(discord_user_id)
            for discord_user_id in admin_discord_user_ids
        )
        body = [
            f"{MATCH_ADMIN_REVIEW_REQUIRED_NOTIFICATION_MESSAGE} match_id={match_id}",
            f"結果: {self._format_match_result_label(final_result)}",
        ]
        if reasons:
            body.append(
                "理由: "
                + ", ".join(self._format_admin_review_reason_label(reason) for reason in reasons)
            )
        if mention_prefix:
            return "\n".join([mention_prefix, *body])
        return "\n".join(body)

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

    def _require_payload_bool(self, payload: dict[str, object], key: str) -> bool:
        value = payload.get(key)
        if not isinstance(value, bool):
            self._raise_publish_error(f"Outbox payload '{key}' must be a bool: {value!r}")
        return value

    def _require_payload_bool_with_default(
        self,
        payload: dict[str, object],
        key: str,
        default: bool,
    ) -> bool:
        value = payload.get(key, default)
        if not isinstance(value, bool):
            self._raise_publish_error(f"Outbox payload '{key}' must be a bool: {value!r}")
        return value

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

    def _require_payload_int_list(self, payload: dict[str, object], key: str) -> list[int]:
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, int) for item in value):
            self._raise_publish_error(f"Outbox payload '{key}' must be a list[int]: {value!r}")
        return cast(list[int], value)

    def _get_optional_team_rating_entries(
        self,
        payload: dict[str, object],
        key: str,
    ) -> list[TeamRatingEntry] | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, list):
            self._raise_publish_error(
                f"Outbox payload '{key}' must be a list[dict[str, int | float]]: {value!r}"
            )

        entries: list[TeamRatingEntry] = []
        for item in value:
            if not isinstance(item, dict):
                self._raise_publish_error(
                    f"Outbox payload '{key}' must be a list[dict[str, int | float]]: {value!r}"
                )
            discord_user_id = item.get("discord_user_id")
            rating = item.get("rating")
            if not isinstance(discord_user_id, int) or isinstance(discord_user_id, bool):
                self._raise_publish_error(
                    f"Outbox payload '{key}.discord_user_id' must be an int: {discord_user_id!r}"
                )
            if not isinstance(rating, int | float) or isinstance(rating, bool):
                self._raise_publish_error(
                    f"Outbox payload '{key}.rating' must be an int | float: {rating!r}"
                )
            entries.append(
                TeamRatingEntry(
                    discord_user_id=discord_user_id,
                    rating=float(rating),
                )
            )
        return entries

    def _render_team_rating_lines(self, payload: dict[str, object]) -> list[str]:
        team_a_rating_entries = self._get_optional_team_rating_entries(
            payload,
            "team_a_rating_entries",
        )
        team_b_rating_entries = self._get_optional_team_rating_entries(
            payload,
            "team_b_rating_entries",
        )
        if team_a_rating_entries is None and team_b_rating_entries is None:
            return []
        if not team_a_rating_entries or not team_b_rating_entries:
            self._raise_publish_error(
                "match_finalized payload team rating entries must either both be present "
                "or both be omitted"
            )

        return [
            "Team A",
            *[
                f"    {format_discord_user_mention(entry.discord_user_id)}: {round(entry.rating)}"
                for entry in team_a_rating_entries
            ],
            "Team B",
            *[
                f"    {format_discord_user_mention(entry.discord_user_id)}: {round(entry.rating)}"
                for entry in team_b_rating_entries
            ],
        ]

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

    def _format_match_result_label(self, value: str) -> str:
        labels = {
            "team_a_win": "チーム A の勝ち",
            "team_b_win": "チーム B の勝ち",
            "draw": "引き分け",
            "void": "無効試合",
        }
        return labels.get(value, value)

    def _format_admin_review_reason_label(self, value: str) -> str:
        labels = {
            "low_report_count": "勝敗報告を行ったプレイヤーが 2 人以下です",
            "single_team_reports": "勝敗報告が片方のチームに偏っています",
            "unresolved_tie": "同票が解消できませんでした",
        }
        return labels.get(value, value)

    def _format_penalty_type_label(self, value: str) -> str:
        labels = {
            "incorrect_report": "誤報告",
            "no_report": "未報告",
            "room_setup_delay": "部屋立て遅延",
            "match_mistake": "試合進行ミス",
            "late": "遅刻",
            "disconnect": "切断",
        }
        return labels.get(value, value)

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
