from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bot.db.session import session_scope
from bot.models import OutboxEvent, OutboxEventType

DEFAULT_OUTBOX_BATCH_SIZE = 100
DEFAULT_OUTBOX_POLL_INTERVAL = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class PendingOutboxEvent:
    id: int
    event_type: OutboxEventType
    dedupe_key: str
    payload: dict[str, object]
    created_at: datetime


class OutboxEventPublisher(Protocol):
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None: ...

    def publish(self, event: PendingOutboxEvent) -> None: ...


class NoopOutboxEventPublisher:
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        del loop

    def publish(self, event: PendingOutboxEvent) -> None:
        del event


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publisher: OutboxEventPublisher,
        *,
        batch_size: int = DEFAULT_OUTBOX_BATCH_SIZE,
        poll_interval: timedelta = DEFAULT_OUTBOX_POLL_INTERVAL,
        logger: logging.Logger | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.publisher = publisher
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.logger = logger or logging.getLogger(__name__)
        self._dispatch_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.publisher.bind_loop(loop)

    async def start(self) -> tuple[int, ...]:
        async with self._state_lock:
            if self._dispatch_task is not None:
                return tuple()

            published_event_ids = await self.dispatch_once()
            self._dispatch_task = asyncio.create_task(
                self._run_dispatch_loop(),
                name="matching-queue-outbox-dispatcher",
            )
            return published_event_ids

    async def stop(self) -> None:
        async with self._state_lock:
            dispatch_task = self._dispatch_task
            self._dispatch_task = None

        if dispatch_task is None:
            return

        dispatch_task.cancel()
        await asyncio.gather(dispatch_task, return_exceptions=True)

    async def dispatch_once(self) -> tuple[int, ...]:
        published_event_ids = await asyncio.to_thread(self._dispatch_once_sync)
        if published_event_ids:
            self.logger.info(
                "Published outbox events count=%s event_ids=%s",
                len(published_event_ids),
                published_event_ids,
            )
        return published_event_ids

    async def _run_dispatch_loop(self) -> None:
        while True:
            try:
                await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Failed to dispatch outbox events")

            try:
                await asyncio.sleep(self.poll_interval.total_seconds())
            except asyncio.CancelledError:
                raise

    def _dispatch_once_sync(self) -> tuple[int, ...]:
        published_event_ids: list[int] = []

        while len(published_event_ids) < self.batch_size:
            event_id = self._dispatch_single_event_sync()
            if event_id is None:
                break
            published_event_ids.append(event_id)

        return tuple(published_event_ids)

    def _dispatch_single_event_sync(self) -> int | None:
        with session_scope(self.session_factory) as session:
            event = session.scalar(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return None

            self.publisher.publish(
                PendingOutboxEvent(
                    id=event.id,
                    event_type=event.event_type,
                    dedupe_key=event.dedupe_key,
                    payload=event.payload,
                    created_at=event.created_at,
                )
            )
            event.published_at = self._get_database_now(session)
            return event.id

    def _get_database_now(self, session: Session) -> datetime:
        return session.execute(select(func.now())).scalar_one()
