from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn, Protocol, cast
from zoneinfo import ZoneInfo

import discord

from dxd_rating.contexts.ui.application import (
    ADMIN_OPERATIONS_NOTIFICATION_KIND_DAILY_WORKER_STARTED,
    ADMIN_OPERATIONS_NOTIFICATION_WORKER_NAME_DAILY_WORKER,
)
from dxd_rating.platform.db.models import OutboxEventType
from dxd_rating.platform.discord.copy.match import (
    MATCH_CREATED_NOTIFICATION_MESSAGE,
    build_match_admin_review_required_content,
    build_match_approval_requested_content,
    build_match_approval_started_content,
    build_match_created_content,
    build_match_finalized_auto_penalty_content,
    build_match_finalized_content,
    build_match_operation_thread_initial_content,
    build_match_operation_thread_name,
    build_match_operation_thread_parent_recruitment_content,
    build_match_operation_thread_routing_message,
    build_match_operation_thread_self_introduction_content,
    build_match_parent_assigned_content,
    build_match_report_opened_content,
    get_admin_review_reason_label,
    get_match_result_label,
    get_penalty_type_label,
)
from dxd_rating.platform.discord.copy.matchmaking import (
    PRESENCE_REMINDER_NOTIFICATION_MESSAGE,
    QUEUE_EXPIRED_NOTIFICATION_MESSAGE,
)
from dxd_rating.platform.discord.copy.system import (
    build_admin_operations_daily_worker_started_message,
    build_season_completed_message,
    build_season_top_rankings_message,
)
from dxd_rating.platform.discord.ui import (
    MatchmakingNewsMatchAnnouncementInteractionHandler,
    MatchmakingPresenceThreadInteractionHandler,
    MatchOperationThreadInteractionHandler,
    create_match_operation_thread_approval_view,
    create_match_operation_thread_initial_view,
    create_match_operation_thread_parent_recruitment_view,
    create_match_operation_thread_report_view,
    create_matchmaking_news_match_announcement_view,
    create_matchmaking_presence_thread_view,
)
from dxd_rating.platform.runtime.outbox import (
    NonRetryableOutboxPublishError,
    PendingOutboxEvent,
)
from dxd_rating.shared.constants import format_discord_user_mention, is_dummy_discord_user_id

JST = ZoneInfo("Asia/Tokyo")


class DiscordSendableChannel(Protocol):
    id: int

    async def send(
        self,
        content: str,
        *,
        allowed_mentions: discord.AllowedMentions,
        view: discord.ui.View | None = None,
    ) -> object: ...


class DiscordThreadParentChannel(DiscordSendableChannel, Protocol):
    async def create_thread(
        self,
        *,
        name: str,
        type: discord.ChannelType,
        invitable: bool,
        reason: str | None = None,
    ) -> object: ...


class DiscordPrivateThread(DiscordSendableChannel, Protocol):
    id: int
    name: str
    parent: object | None

    async def add_user(self, user: object) -> None: ...


class DiscordSendableUser(Protocol):
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

    def get_user(self, user_id: int) -> object | None: ...

    async def fetch_user(self, user_id: int) -> object: ...


@dataclass(frozen=True, slots=True)
class ChannelNotificationDestination:
    channel_id: int
    guild_id: int | None


@dataclass(frozen=True, slots=True)
class DirectMessageNotificationDestination:
    discord_user_id: int


NotificationDestination = ChannelNotificationDestination | DirectMessageNotificationDestination


@dataclass(frozen=True, slots=True)
class ResolvedNotification:
    destination: NotificationDestination
    content: str


@dataclass(frozen=True, slots=True)
class TeamRatingEntry:
    discord_user_id: int
    rating: float


@dataclass(frozen=True, slots=True)
class SeasonTopRankingEntry:
    rank: int
    display_name: str
    rating: float


@dataclass(frozen=True, slots=True)
class MatchOperationThreadContext:
    match_id: int
    parent_channel_id: int
    match_format: str
    queue_name: str
    team_a_discord_user_ids: tuple[int, ...]
    team_b_discord_user_ids: tuple[int, ...]
    team_a_rating_entries: tuple[TeamRatingEntry, ...] | None = None
    team_b_rating_entries: tuple[TeamRatingEntry, ...] | None = None

    @property
    def participant_discord_user_ids(self) -> tuple[int, ...]:
        return (*self.team_a_discord_user_ids, *self.team_b_discord_user_ids)


@dataclass(frozen=True, slots=True)
class MatchOperationThreadRoutingContext:
    match_id: int
    parent_channel_id: int
    team_a_discord_user_ids: tuple[int, ...]
    team_b_discord_user_ids: tuple[int, ...]

    @property
    def participant_discord_user_ids(self) -> tuple[int, ...]:
        return (*self.team_a_discord_user_ids, *self.team_b_discord_user_ids)


class DiscordOutboxEventPublisher:
    def __init__(
        self,
        client: DiscordChannelClient,
        *,
        admin_discord_user_ids: frozenset[int] = frozenset(),
        match_operation_thread_interaction_handler: (
            MatchOperationThreadInteractionHandler | None
        ) = None,
        matchmaking_news_match_announcement_interaction_handler: (
            MatchmakingNewsMatchAnnouncementInteractionHandler | None
        ) = None,
        matchmaking_presence_interaction_handler: (
            MatchmakingPresenceThreadInteractionHandler | None
        ) = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.admin_discord_user_ids = admin_discord_user_ids
        self.match_operation_thread_interaction_handler = match_operation_thread_interaction_handler
        self.matchmaking_news_match_announcement_interaction_handler = (
            matchmaking_news_match_announcement_interaction_handler
        )
        self.matchmaking_presence_interaction_handler = matchmaking_presence_interaction_handler
        self.logger = logger or logging.getLogger(__name__)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._allowed_mentions = discord.AllowedMentions(
            users=True,
            roles=False,
            everyone=False,
            replied_user=False,
        )
        self._match_operation_threads_by_match_id: dict[int, DiscordPrivateThread] = {}

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
        await self._maybe_create_match_operation_thread(event)
        match_operation_thread = await self._resolve_match_operation_thread_for_event(event)
        if match_operation_thread is not None:
            await match_operation_thread.send(
                self._render_content(event.event_type, event.payload),
                allowed_mentions=self._allowed_mentions,
                view=self._build_match_operation_thread_event_view(event),
            )
            return

        destination = await asyncio.to_thread(self._require_destination, event.payload)
        if isinstance(destination, DirectMessageNotificationDestination):
            await self._send_direct_message_notification(
                event=event,
                destination=destination,
                content=self._render_content(event.event_type, event.payload),
            )
            return

        await self._send_channel_notification(
            event=event,
            destination=destination,
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

        try:
            fetched_channel_object = await self.client.fetch_channel(channel_id)
        except discord.NotFound as exc:
            raise NonRetryableOutboxPublishError(
                f"Discord channel not found: {channel_id}"
            ) from exc

        fetched_channel = self._as_sendable_channel(fetched_channel_object)
        if fetched_channel is None:
            raise NonRetryableOutboxPublishError(
                f"Fetched Discord channel is not sendable: {channel_id}"
            )
        return fetched_channel

    async def _resolve_user(self, user_id: int) -> DiscordSendableUser:
        user = self._as_sendable_user(self.client.get_user(user_id))
        if user is not None:
            return user

        try:
            fetched_user_object = await self.client.fetch_user(user_id)
        except discord.NotFound as exc:
            raise NonRetryableOutboxPublishError(f"Discord user not found: {user_id}") from exc

        fetched_user = self._as_sendable_user(fetched_user_object)
        if fetched_user is None:
            raise NonRetryableOutboxPublishError(f"Fetched Discord user is not sendable: {user_id}")
        return fetched_user

    async def _send_channel_notification(
        self,
        *,
        event: PendingOutboxEvent,
        destination: ChannelNotificationDestination,
    ) -> None:
        channel = await self._resolve_channel(destination.channel_id)
        self._log_channel_guild_mismatch(
            destination=destination,
            channel=channel,
            event=event,
        )
        content = await self._render_channel_content(event=event, channel=channel)
        view = self._build_channel_view(event=event, channel=channel)
        try:
            await channel.send(
                content,
                allowed_mentions=self._allowed_mentions,
                view=view,
            )
        except discord.NotFound as exc:
            raise NonRetryableOutboxPublishError(
                f"Discord channel was deleted before send: {destination.channel_id}"
            ) from exc

    async def _send_direct_message_notification(
        self,
        *,
        event: PendingOutboxEvent,
        destination: DirectMessageNotificationDestination,
        content: str,
    ) -> None:
        try:
            user = await self._resolve_user(destination.discord_user_id)
            await user.send(
                content,
                allowed_mentions=self._allowed_mentions,
            )
        except discord.NotFound as exc:
            raise NonRetryableOutboxPublishError(
                f"Discord user was deleted before DM send: {destination.discord_user_id}"
            ) from exc
        except discord.Forbidden as exc:
            raise NonRetryableOutboxPublishError(
                f"Discord DM destination is forbidden: {destination.discord_user_id}"
            ) from exc

    async def _maybe_create_match_operation_thread(self, event: PendingOutboxEvent) -> None:
        context = self._build_match_operation_thread_context(event)
        if context is None:
            return

        try:
            thread, created = await self._resolve_or_create_match_operation_thread(
                match_id=context.match_id,
                parent_channel_id=context.parent_channel_id,
            )
        except Exception:
            self.logger.exception(
                "Failed to prepare match operation thread match_id=%s parent_channel_id=%s",
                context.match_id,
                context.parent_channel_id,
            )
            return

        if not created:
            return

        try:
            await self._invite_match_operation_thread_users(
                thread,
                context.participant_discord_user_ids,
            )
            await thread.send(
                self._render_match_operation_thread_initial_content(context),
                allowed_mentions=self._allowed_mentions,
                view=self._build_match_operation_thread_initial_view(context),
            )
            await thread.send(
                self._render_match_operation_thread_parent_recruitment_content(context),
                allowed_mentions=self._allowed_mentions,
                view=self._build_match_operation_thread_parent_recruitment_view(context),
            )
            await thread.send(
                self._render_match_operation_thread_self_introduction_content(context),
                allowed_mentions=self._allowed_mentions,
            )
        except Exception:
            self.logger.exception(
                "Failed to initialize match operation thread match_id=%s thread_id=%s",
                context.match_id,
                thread.id,
            )

    async def _resolve_match_operation_thread_for_event(
        self,
        event: PendingOutboxEvent,
    ) -> DiscordPrivateThread | None:
        if event.event_type not in {
            OutboxEventType.MATCH_PARENT_ASSIGNED,
            OutboxEventType.MATCH_REPORT_OPENED,
            OutboxEventType.MATCH_APPROVAL_REQUESTED,
            OutboxEventType.MATCH_FINALIZED,
            OutboxEventType.MATCH_ADMIN_REVIEW_REQUIRED,
        }:
            return None

        context = self._build_match_operation_thread_routing_context(event.payload)
        if context is None:
            return None

        thread, created = await self._resolve_or_create_match_operation_thread(
            match_id=context.match_id,
            parent_channel_id=context.parent_channel_id,
        )
        if created:
            await self._invite_match_operation_thread_users(
                thread,
                context.participant_discord_user_ids,
            )
        return thread

    def _build_match_operation_thread_context(
        self,
        event: PendingOutboxEvent,
    ) -> MatchOperationThreadContext | None:
        if event.event_type != OutboxEventType.MATCH_CREATED:
            return None

        if not self._require_payload_bool_with_default(
            event.payload,
            "create_match_operation_thread",
            False,
        ):
            return None

        parent_channel_id = self._require_payload_int(
            event.payload,
            "match_operation_thread_parent_channel_id",
        )
        match_id = self._require_payload_int(event.payload, "match_id")
        match_format = self._require_payload_str(event.payload, "match_format")
        queue_name = self._require_payload_str(event.payload, "queue_name")
        team_a_discord_user_ids = tuple(
            self._require_payload_int_list(event.payload, "team_a_discord_user_ids")
        )
        team_b_discord_user_ids = tuple(
            self._require_payload_int_list(event.payload, "team_b_discord_user_ids")
        )
        if not team_a_discord_user_ids or not team_b_discord_user_ids:
            self._raise_publish_error(
                "match_created payload team discord user ids must not be empty"
            )
        rating_entries = self._get_match_created_team_rating_entries(event.payload)
        team_a_rating_entries: tuple[TeamRatingEntry, ...] | None = None
        team_b_rating_entries: tuple[TeamRatingEntry, ...] | None = None
        if rating_entries is not None:
            team_a_rating_entries = tuple(rating_entries[0])
            team_b_rating_entries = tuple(rating_entries[1])

        return MatchOperationThreadContext(
            match_id=match_id,
            parent_channel_id=parent_channel_id,
            match_format=match_format,
            queue_name=queue_name,
            team_a_discord_user_ids=team_a_discord_user_ids,
            team_b_discord_user_ids=team_b_discord_user_ids,
            team_a_rating_entries=team_a_rating_entries,
            team_b_rating_entries=team_b_rating_entries,
        )

    def _build_match_operation_thread_routing_context(
        self,
        payload: dict[str, object],
    ) -> MatchOperationThreadRoutingContext | None:
        parent_channel_id = payload.get("match_operation_thread_parent_channel_id")
        if parent_channel_id is None:
            return None
        if not isinstance(parent_channel_id, int):
            self._raise_publish_error(
                "Outbox payload 'match_operation_thread_parent_channel_id' must be an int: "
                f"{parent_channel_id!r}"
            )

        match_id = self._require_payload_int(payload, "match_id")
        team_a_discord_user_ids = tuple(
            self._require_payload_int_list(payload, "team_a_discord_user_ids")
        )
        team_b_discord_user_ids = tuple(
            self._require_payload_int_list(payload, "team_b_discord_user_ids")
        )
        if not team_a_discord_user_ids or not team_b_discord_user_ids:
            self._raise_publish_error(
                "match operation thread payload team discord user ids must not be empty"
            )

        return MatchOperationThreadRoutingContext(
            match_id=match_id,
            parent_channel_id=parent_channel_id,
            team_a_discord_user_ids=team_a_discord_user_ids,
            team_b_discord_user_ids=team_b_discord_user_ids,
        )

    async def _resolve_or_create_match_operation_thread(
        self,
        *,
        match_id: int,
        parent_channel_id: int,
    ) -> tuple[DiscordPrivateThread, bool]:
        cached_thread = self._match_operation_threads_by_match_id.get(match_id)
        if cached_thread is not None:
            return cached_thread, False

        parent_channel = await self._resolve_channel(parent_channel_id)
        thread_parent = self._as_thread_parent_channel(parent_channel)
        if thread_parent is None:
            raise NonRetryableOutboxPublishError(
                f"Discord channel does not support private thread creation: {parent_channel_id}"
            )

        existing_thread = self._find_existing_match_operation_thread(
            parent_channel=thread_parent,
            match_id=match_id,
        )
        if existing_thread is not None:
            self._match_operation_threads_by_match_id[match_id] = existing_thread
            return existing_thread, False

        created_thread_object = await thread_parent.create_thread(
            name=self._build_match_operation_thread_name(match_id),
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason=f"Create match operation thread for match_id={match_id}",
        )
        thread = self._as_private_thread(created_thread_object)
        if thread is None:
            raise NonRetryableOutboxPublishError(
                f"Created Discord thread is not sendable: match_id={match_id}"
            )

        self._match_operation_threads_by_match_id[match_id] = thread
        return thread, True

    def _find_existing_match_operation_thread(
        self,
        *,
        parent_channel: DiscordThreadParentChannel,
        match_id: int,
    ) -> DiscordPrivateThread | None:
        thread_name = self._build_match_operation_thread_name(match_id)
        for candidate in self._iter_thread_candidates(parent_channel):
            thread = self._as_private_thread(candidate)
            if thread is None:
                continue
            if thread.parent is not parent_channel:
                continue
            if thread.name != thread_name:
                continue
            return thread
        return None

    def _iter_thread_candidates(
        self,
        parent_channel: DiscordThreadParentChannel,
    ) -> Iterable[object]:
        for attribute_name in ("created_threads", "threads"):
            candidates = getattr(parent_channel, attribute_name, None)
            if isinstance(candidates, list | tuple):
                yield from candidates

        guild = getattr(parent_channel, "guild", None)
        guild_threads = getattr(guild, "threads", None)
        if isinstance(guild_threads, list | tuple):
            yield from guild_threads

    async def _invite_match_operation_thread_users(
        self,
        thread: DiscordPrivateThread,
        participant_discord_user_ids: Iterable[int],
    ) -> None:
        invitee_ids = self._dedupe_discord_user_ids(
            [*participant_discord_user_ids, *sorted(self.admin_discord_user_ids)]
        )
        for discord_user_id in invitee_ids:
            if is_dummy_discord_user_id(discord_user_id):
                continue

            try:
                user = await self._resolve_user(discord_user_id)
                await thread.add_user(user)
            except Exception:
                self.logger.exception(
                    "Failed to add user to match operation thread thread_id=%s discord_user_id=%s",
                    thread.id,
                    discord_user_id,
                )

    def _render_match_operation_thread_initial_content(
        self,
        context: MatchOperationThreadContext,
    ) -> str:
        return build_match_operation_thread_initial_content(
            match_format=context.match_format,
            queue_name=context.queue_name,
            team_a_labels=self._build_match_created_team_labels(
                labels=[
                    format_discord_user_mention(discord_user_id)
                    for discord_user_id in context.team_a_discord_user_ids
                ],
                expected_discord_user_ids=context.team_a_discord_user_ids,
                rating_entries=context.team_a_rating_entries,
                payload_context="match_created payload for match operation thread",
            ),
            team_b_labels=self._build_match_created_team_labels(
                labels=[
                    format_discord_user_mention(discord_user_id)
                    for discord_user_id in context.team_b_discord_user_ids
                ],
                expected_discord_user_ids=context.team_b_discord_user_ids,
                rating_entries=context.team_b_rating_entries,
                payload_context="match_created payload for match operation thread",
            ),
            with_void_button=self.match_operation_thread_interaction_handler is not None,
        )

    def _build_match_operation_thread_initial_view(
        self,
        context: MatchOperationThreadContext,
    ) -> discord.ui.View | None:
        if self.match_operation_thread_interaction_handler is None:
            return None
        return create_match_operation_thread_initial_view(
            match_id=context.match_id,
            interaction_handler=self.match_operation_thread_interaction_handler,
        )

    def _build_match_operation_thread_parent_recruitment_view(
        self,
        context: MatchOperationThreadContext,
    ) -> discord.ui.View | None:
        if self.match_operation_thread_interaction_handler is None:
            return None
        return create_match_operation_thread_parent_recruitment_view(
            match_id=context.match_id,
            interaction_handler=self.match_operation_thread_interaction_handler,
        )

    def _render_match_operation_thread_parent_recruitment_content(
        self,
        context: MatchOperationThreadContext,
    ) -> str:
        del context
        return build_match_operation_thread_parent_recruitment_content()

    def _render_match_operation_thread_self_introduction_content(
        self,
        context: MatchOperationThreadContext,
    ) -> str:
        del context
        return build_match_operation_thread_self_introduction_content()

    def _build_match_operation_thread_name(self, match_id: int) -> str:
        return build_match_operation_thread_name(match_id)

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

        if event_type == OutboxEventType.MATCH_REPORT_OPENED:
            return self._render_match_report_opened_content(payload)

        if event_type == OutboxEventType.MATCH_APPROVAL_REQUESTED:
            return self._render_match_approval_requested_content(payload)

        if event_type == OutboxEventType.MATCH_FINALIZED:
            return self._render_match_finalized_content(payload)

        if event_type == OutboxEventType.MATCH_ADMIN_REVIEW_REQUIRED:
            return self._render_match_admin_review_required_content(payload)

        if event_type == OutboxEventType.SEASON_COMPLETED:
            return self._render_season_completed_content(payload)

        if event_type == OutboxEventType.SEASON_TOP_RANKINGS:
            return self._render_season_top_rankings_content(payload)

        if event_type == OutboxEventType.ADMIN_OPERATIONS_NOTIFICATION:
            return self._render_admin_operations_notification_content(payload)

        self._raise_publish_error(f"Unsupported outbox event type: {event_type}")

    def _render_presence_reminder_content(self, payload: dict[str, object]) -> str:
        return self._render_channel_targeted_text_notification(
            payload,
            PRESENCE_REMINDER_NOTIFICATION_MESSAGE,
        )

    def _render_queue_expired_content(self, payload: dict[str, object]) -> str:
        return self._render_channel_targeted_text_notification(
            payload,
            QUEUE_EXPIRED_NOTIFICATION_MESSAGE,
        )

    def _render_admin_operations_notification_content(self, payload: dict[str, object]) -> str:
        notification_kind = self._require_payload_str(payload, "notification_kind")
        if notification_kind != ADMIN_OPERATIONS_NOTIFICATION_KIND_DAILY_WORKER_STARTED:
            self._raise_publish_error(
                f"Unsupported admin operations notification kind: {notification_kind}"
            )

        worker_name = self._require_payload_str(payload, "worker_name")
        if worker_name != ADMIN_OPERATIONS_NOTIFICATION_WORKER_NAME_DAILY_WORKER:
            self._raise_publish_error(
                f"Unsupported admin operations notification worker_name: {worker_name}"
            )

        occurred_at = self._parse_admin_operations_notification_occurred_at(payload)
        localized_occurred_at = occurred_at.astimezone(JST)
        return build_admin_operations_daily_worker_started_message(
            f"{localized_occurred_at:%Y-%m-%d %H:%M JST}"
        )

    def _render_season_completed_content(self, payload: dict[str, object]) -> str:
        season_id = self._require_payload_int(payload, "season_id")
        season_name = self._require_payload_str(payload, "season_name")
        completed_at = self._parse_season_completed_at(payload)
        localized_completed_at = completed_at.astimezone(JST)
        return build_season_completed_message(
            season_id,
            season_name,
            f"{localized_completed_at:%Y-%m-%d %H:%M JST}",
        )

    def _render_season_top_rankings_content(self, payload: dict[str, object]) -> str:
        season_id = self._require_payload_int(payload, "season_id")
        season_name = self._require_payload_str(payload, "season_name")
        match_format = self._require_payload_str(payload, "match_format")
        self._parse_season_completed_at(payload)
        entries = self._get_season_top_ranking_entries(payload, "entries")
        ranking_lines = [f"{entry.rank} / {entry.display_name} / {entry.rating:.2f}" for entry in entries]
        item_range = None if not entries else f"{entries[0].rank}-{entries[-1].rank}"
        return build_season_top_rankings_message(
            season_id=season_id,
            season_name=season_name,
            match_format=match_format,
            item_range=item_range,
            ranking_lines=ranking_lines,
        )

    def _render_match_created_content(self, payload: dict[str, object]) -> str:
        self._require_payload_int(payload, "match_id")
        rating_entries = self._get_match_created_team_rating_entries(payload)
        team_a_player_display_names = payload.get("team_a_player_display_names")
        team_b_player_display_names = payload.get("team_b_player_display_names")
        if team_a_player_display_names is not None or team_b_player_display_names is not None:
            if team_a_player_display_names is None or team_b_player_display_names is None:
                self._raise_publish_error(
                    "match_created payload team player display names must either both be "
                    "present or both be omitted"
                )

            match_format = self._require_payload_str(payload, "match_format")
            queue_name = self._require_payload_str(payload, "queue_name")
            rendered_team_a_player_display_names = self._require_payload_str_list(
                payload,
                "team_a_player_display_names",
            )
            rendered_team_b_player_display_names = self._require_payload_str_list(
                payload,
                "team_b_player_display_names",
            )
            if not rendered_team_a_player_display_names or not rendered_team_b_player_display_names:
                self._raise_publish_error(
                    "match_created payload team player display names must not be empty"
                )
            team_a_labels = rendered_team_a_player_display_names
            team_b_labels = rendered_team_b_player_display_names
            if rating_entries is not None:
                team_a_discord_user_ids = self._require_payload_int_list(
                    payload,
                    "team_a_discord_user_ids",
                )
                team_b_discord_user_ids = self._require_payload_int_list(
                    payload,
                    "team_b_discord_user_ids",
                )
                team_a_labels = self._build_match_created_team_labels(
                    labels=rendered_team_a_player_display_names,
                    expected_discord_user_ids=team_a_discord_user_ids,
                    rating_entries=rating_entries[0],
                    payload_context="match_created payload",
                )
                team_b_labels = self._build_match_created_team_labels(
                    labels=rendered_team_b_player_display_names,
                    expected_discord_user_ids=team_b_discord_user_ids,
                    rating_entries=rating_entries[1],
                    payload_context="match_created payload",
                )

            return build_match_created_content(
                match_format=match_format,
                queue_name=queue_name,
                team_a_labels=team_a_labels,
                team_b_labels=team_b_labels,
                include_spectate_guide=(
                    self.matchmaking_news_match_announcement_interaction_handler is not None
                ),
            )

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
        team_a_labels = [
            format_discord_user_mention(discord_user_id)
            for discord_user_id in team_a_discord_user_ids
        ]
        team_b_labels = [
            format_discord_user_mention(discord_user_id)
            for discord_user_id in team_b_discord_user_ids
        ]
        if rating_entries is not None:
            team_a_labels = self._build_match_created_team_labels(
                labels=team_a_labels,
                expected_discord_user_ids=team_a_discord_user_ids,
                rating_entries=rating_entries[0],
                payload_context="match_created payload",
            )
            team_b_labels = self._build_match_created_team_labels(
                labels=team_b_labels,
                expected_discord_user_ids=team_b_discord_user_ids,
                rating_entries=rating_entries[1],
                payload_context="match_created payload",
            )

        return build_match_created_content(
            team_a_labels=team_a_labels,
            team_b_labels=team_b_labels,
        )

    async def _render_channel_content(
        self,
        *,
        event: PendingOutboxEvent,
        channel: DiscordSendableChannel,
    ) -> str:
        if (
            event.event_type == OutboxEventType.MATCH_CREATED
            and self._is_thread_like_channel(channel)
            and "mention_discord_user_id" in event.payload
            and "match_operation_thread_parent_channel_id" in event.payload
        ):
            return await self._render_presence_thread_match_created_content(event.payload)

        return self._render_content(event.event_type, event.payload)

    async def _render_presence_thread_match_created_content(
        self,
        payload: dict[str, object],
    ) -> str:
        mention_discord_user_id = self._require_payload_int(payload, "mention_discord_user_id")
        match_operation_thread = await self._resolve_match_operation_thread_from_payload(payload)
        return "\n".join(
            [
                f"{format_discord_user_mention(mention_discord_user_id)} "
                f"{MATCH_CREATED_NOTIFICATION_MESSAGE}",
                build_match_operation_thread_routing_message(f"<#{match_operation_thread.id}>"),
            ]
        )

    async def _resolve_match_operation_thread_from_payload(
        self,
        payload: dict[str, object],
    ) -> DiscordPrivateThread:
        context = self._build_match_operation_thread_routing_context(payload)
        if context is None:
            self._raise_publish_error(
                "match_created payload for presence thread must include "
                "'match_operation_thread_parent_channel_id'"
            )

        thread, _ = await self._resolve_or_create_match_operation_thread(
            match_id=context.match_id,
            parent_channel_id=context.parent_channel_id,
        )
        return thread

    def _render_channel_targeted_text_notification(
        self,
        payload: dict[str, object],
        message: str,
    ) -> str:
        destination = payload.get("destination")
        if not isinstance(destination, dict):
            self._raise_publish_error(
                f"Outbox payload 'destination' must be a dict[str, int | None]: {destination!r}"
            )

        if destination.get("kind", "channel") != "channel":
            return message

        mention_discord_user_id = self._require_payload_int(payload, "mention_discord_user_id")
        mention_text = format_discord_user_mention(mention_discord_user_id)
        return f"{mention_text} {message}"

    def _build_channel_view(
        self,
        *,
        event: PendingOutboxEvent,
        channel: DiscordSendableChannel,
    ) -> discord.ui.View | None:
        if (
            event.event_type == OutboxEventType.MATCH_CREATED
            and self.matchmaking_news_match_announcement_interaction_handler is not None
            and self._is_matchmaking_news_match_created_payload(event.payload)
        ):
            return create_matchmaking_news_match_announcement_view(
                self.matchmaking_news_match_announcement_interaction_handler,
                match_id=self._require_payload_int(event.payload, "match_id"),
            )

        if self.matchmaking_presence_interaction_handler is None:
            return None
        if event.event_type != OutboxEventType.PRESENCE_REMINDER:
            return None
        if not self._is_thread_like_channel(channel):
            return None
        return create_matchmaking_presence_thread_view(
            self.matchmaking_presence_interaction_handler
        )

    def _is_matchmaking_news_match_created_payload(self, payload: dict[str, object]) -> bool:
        return (
            "team_a_player_display_names" in payload
            and "team_b_player_display_names" in payload
            and "queue_name" in payload
        )

    def _is_thread_like_channel(self, channel: object) -> bool:
        return getattr(channel, "parent", None) is not None

    def _as_thread_parent_channel(self, channel: object) -> DiscordThreadParentChannel | None:
        if self._as_sendable_channel(channel) is None:
            return None
        if not callable(getattr(channel, "create_thread", None)):
            return None
        return cast(DiscordThreadParentChannel, channel)

    def _as_private_thread(self, channel: object) -> DiscordPrivateThread | None:
        if self._as_sendable_channel(channel) is None:
            return None
        if getattr(channel, "parent", None) is None:
            return None
        if not callable(getattr(channel, "add_user", None)):
            return None
        return cast(DiscordPrivateThread, channel)

    def _render_match_parent_assigned_content(self, payload: dict[str, object]) -> str:
        self._require_payload_int(payload, "match_id")
        parent_discord_user_id = self._require_payload_int(payload, "parent_discord_user_id")
        report_open_at = self._require_payload_str(payload, "report_open_at")
        report_deadline_at = self._require_payload_str(payload, "report_deadline_at")
        return build_match_parent_assigned_content(
            format_discord_user_mention(parent_discord_user_id),
            report_open_at,
            report_deadline_at,
        )

    def _render_match_report_opened_content(self, payload: dict[str, object]) -> str:
        self._require_payload_int(payload, "match_id")
        report_deadline_at = self._require_payload_str(payload, "report_deadline_at")
        return build_match_report_opened_content(report_deadline_at)

    def _build_match_operation_thread_event_view(
        self,
        event: PendingOutboxEvent,
    ) -> discord.ui.View | None:
        if self.match_operation_thread_interaction_handler is None:
            return None

        match_id = self._require_payload_int(event.payload, "match_id")
        if event.event_type == OutboxEventType.MATCH_REPORT_OPENED:
            return create_match_operation_thread_report_view(
                match_id=match_id,
                interaction_handler=self.match_operation_thread_interaction_handler,
            )

        if event.event_type != OutboxEventType.MATCH_APPROVAL_REQUESTED:
            return None

        phase_started = self._require_payload_bool_with_default(
            event.payload,
            "phase_started",
            False,
        )
        if phase_started:
            return None

        return create_match_operation_thread_approval_view(
            match_id=match_id,
            interaction_handler=self.match_operation_thread_interaction_handler,
        )

    def _render_match_approval_requested_content(self, payload: dict[str, object]) -> str:
        self._require_payload_int(payload, "match_id")
        phase_started = self._require_payload_bool_with_default(payload, "phase_started", False)
        provisional_result = self._require_payload_str(payload, "provisional_result")
        approval_deadline_at = self._require_payload_str(payload, "approval_deadline_at")
        if phase_started:
            return build_match_approval_started_content(
                self._format_match_result_label(provisional_result),
                approval_deadline_at,
            )

        approval_target_discord_user_ids = payload.get("approval_target_discord_user_ids")
        if approval_target_discord_user_ids is not None:
            approval_target_mentions = [
                format_discord_user_mention(discord_user_id)
                for discord_user_id in self._require_payload_int_list(
                    payload,
                    "approval_target_discord_user_ids",
                )
            ]
            if not approval_target_mentions:
                self._raise_publish_error(
                    "match_approval_requested payload approval targets must not be empty"
                )
            mention_text = " ".join(approval_target_mentions)
        else:
            mention_discord_user_id = self._require_payload_int(payload, "mention_discord_user_id")
            mention_text = format_discord_user_mention(mention_discord_user_id)
        return build_match_approval_requested_content(
            mention_text,
            self._format_match_result_label(provisional_result),
            approval_deadline_at,
        )

    def _render_match_finalized_content(self, payload: dict[str, object]) -> str:
        self._require_payload_int(payload, "match_id")
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
            return build_match_finalized_auto_penalty_content(
                mention_text,
                self._format_match_result_label(final_result),
                self._format_penalty_type_label(penalty_type),
                penalty_count,
            )

        finalized_by_admin = self._require_payload_bool(payload, "finalized_by_admin")
        return build_match_finalized_content(
            self._format_match_result_label(final_result),
            finalized_by_admin=finalized_by_admin,
            rating_lines=self._render_team_rating_lines(payload),
        )

    def _render_match_admin_review_required_content(self, payload: dict[str, object]) -> str:
        self._require_payload_int(payload, "match_id")
        final_result = self._require_payload_str(payload, "final_result")
        reasons = self._require_payload_str_list(payload, "admin_review_reasons")
        admin_discord_user_ids = self._require_payload_int_list(payload, "admin_discord_user_ids")
        mention_prefix = " ".join(
            format_discord_user_mention(discord_user_id)
            for discord_user_id in admin_discord_user_ids
        )
        return build_match_admin_review_required_content(
            mention_prefix,
            self._format_match_result_label(final_result),
            [self._format_admin_review_reason_label(reason) for reason in reasons],
        )

    def _log_channel_guild_mismatch(
        self,
        *,
        destination: ChannelNotificationDestination,
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

    def _dedupe_discord_user_ids(self, discord_user_ids: Iterable[int]) -> list[int]:
        deduped_user_ids: list[int] = []
        seen_user_ids: set[int] = set()
        for discord_user_id in discord_user_ids:
            if discord_user_id in seen_user_ids:
                continue
            seen_user_ids.add(discord_user_id)
            deduped_user_ids.append(discord_user_id)
        return deduped_user_ids

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

    def _get_match_created_team_rating_entries(
        self,
        payload: dict[str, object],
    ) -> tuple[list[TeamRatingEntry], list[TeamRatingEntry]] | None:
        team_a_rating_entries = self._get_optional_team_rating_entries(
            payload,
            "team_a_rating_entries",
        )
        team_b_rating_entries = self._get_optional_team_rating_entries(
            payload,
            "team_b_rating_entries",
        )
        if team_a_rating_entries is None and team_b_rating_entries is None:
            return None
        if not team_a_rating_entries or not team_b_rating_entries:
            self._raise_publish_error(
                "match_created payload team rating entries must either both be present "
                "or both be omitted"
            )
        return team_a_rating_entries, team_b_rating_entries

    def _build_match_created_team_labels(
        self,
        *,
        labels: Sequence[str],
        expected_discord_user_ids: Sequence[int],
        rating_entries: Sequence[TeamRatingEntry] | None,
        payload_context: str,
    ) -> list[str]:
        if rating_entries is None:
            return list(labels)
        if len(labels) != len(expected_discord_user_ids):
            self._raise_publish_error(
                f"{payload_context} team labels and discord user ids must have the same length"
            )
        if len(rating_entries) != len(expected_discord_user_ids):
            self._raise_publish_error(
                f"{payload_context} team rating entries must match team discord user ids"
            )

        ratings_by_discord_user_id: dict[int, float] = {}
        for entry in rating_entries:
            if entry.discord_user_id in ratings_by_discord_user_id:
                self._raise_publish_error(
                    f"{payload_context} team rating entries must not contain duplicate users"
                )
            ratings_by_discord_user_id[entry.discord_user_id] = entry.rating

        formatted_labels: list[str] = []
        for label, discord_user_id in zip(labels, expected_discord_user_ids, strict=True):
            rating = ratings_by_discord_user_id.get(discord_user_id)
            if rating is None:
                self._raise_publish_error(
                    f"{payload_context} team rating entries must match team discord user ids"
                )
            formatted_labels.append(f"{label}: {round(rating)}")

        return formatted_labels

    def _get_season_top_ranking_entries(
        self,
        payload: dict[str, object],
        key: str,
    ) -> list[SeasonTopRankingEntry]:
        value = payload.get(key)
        if not isinstance(value, list):
            self._raise_publish_error(
                f"Outbox payload '{key}' must be a list[dict[str, int | str | float]]: {value!r}"
            )

        entries: list[SeasonTopRankingEntry] = []
        for item in value:
            if not isinstance(item, dict):
                self._raise_publish_error(
                    "Outbox payload "
                    f"'{key}' must be a list[dict[str, int | str | float]]: {value!r}"
                )
            rank = item.get("rank")
            display_name = item.get("display_name")
            rating = item.get("rating")
            if not isinstance(rank, int) or isinstance(rank, bool):
                self._raise_publish_error(f"Outbox payload '{key}.rank' must be an int: {rank!r}")
            if not isinstance(display_name, str):
                self._raise_publish_error(
                    f"Outbox payload '{key}.display_name' must be a str: {display_name!r}"
                )
            if not isinstance(rating, int | float) or isinstance(rating, bool):
                self._raise_publish_error(
                    f"Outbox payload '{key}.rating' must be an int | float: {rating!r}"
                )
            entries.append(
                SeasonTopRankingEntry(
                    rank=rank,
                    display_name=display_name,
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

    def _parse_admin_operations_notification_occurred_at(
        self,
        payload: dict[str, object],
    ) -> datetime:
        occurred_at = self._require_payload_str(payload, "occurred_at")
        try:
            parsed = datetime.fromisoformat(occurred_at)
        except ValueError:
            self._raise_publish_error(
                f"Outbox payload 'occurred_at' must be an ISO 8601 datetime: {occurred_at!r}"
            )

        if parsed.tzinfo is None or parsed.utcoffset() is None:
            self._raise_publish_error(
                f"Outbox payload 'occurred_at' must be timezone-aware UTC: {occurred_at!r}"
            )
        if parsed.utcoffset() != timedelta(0):
            self._raise_publish_error(f"Outbox payload 'occurred_at' must be UTC: {occurred_at!r}")
        return parsed

    def _parse_season_completed_at(
        self,
        payload: dict[str, object],
    ) -> datetime:
        completed_at = self._require_payload_str(payload, "completed_at")
        try:
            parsed = datetime.fromisoformat(completed_at)
        except ValueError:
            self._raise_publish_error(
                f"Outbox payload 'completed_at' must be an ISO 8601 datetime: {completed_at!r}"
            )

        if parsed.tzinfo is None or parsed.utcoffset() is None:
            self._raise_publish_error(
                f"Outbox payload 'completed_at' must be timezone-aware UTC: {completed_at!r}"
            )
        if parsed.utcoffset() != timedelta(0):
            self._raise_publish_error(
                f"Outbox payload 'completed_at' must be UTC: {completed_at!r}"
            )
        return parsed

    def _require_destination(self, payload: dict[str, object]) -> NotificationDestination:
        value = payload.get("destination")
        if not isinstance(value, dict):
            self._raise_publish_error(
                f"Outbox payload 'destination' must be a dict[str, int | None]: {value!r}"
            )

        kind = value.get("kind", "channel")
        if kind == "dm":
            discord_user_id = value.get("discord_user_id")
            if not isinstance(discord_user_id, int):
                self._raise_publish_error(
                    "Outbox payload destination.discord_user_id must be an int: "
                    f"{discord_user_id!r}"
                )
            return DirectMessageNotificationDestination(discord_user_id=discord_user_id)

        if kind != "channel":
            self._raise_publish_error(
                f"Outbox payload destination.kind must be 'channel' or 'dm': {kind!r}"
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

        return ChannelNotificationDestination(
            channel_id=channel_id,
            guild_id=cast(int | None, guild_id),
        )

    def _format_match_result_label(self, value: str) -> str:
        return get_match_result_label(value)

    def _format_admin_review_reason_label(self, value: str) -> str:
        return get_admin_review_reason_label(value)

    def _format_penalty_type_label(self, value: str) -> str:
        return get_penalty_type_label(value)

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

    def _as_sendable_user(self, user: object | None) -> DiscordSendableUser | None:
        if user is None:
            return None

        send = getattr(user, "send", None)
        user_id = getattr(user, "id", None)
        if not callable(send) or not isinstance(user_id, int):
            return None

        return cast(DiscordSendableUser, user)

    def _raise_publish_error(self, message: str) -> NoReturn:
        self.logger.error(message)
        raise ValueError(message)
