from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol, cast

from dxd_rating.platform.runtime.match_runtime import MatchRuntime, MatchRuntimeSyncResult
from dxd_rating.platform.runtime.outbox import (
    NoopOutboxDispatcher,
    OutboxDispatcher,
    OutboxStartupResult,
)


class RuntimeLifecycle(Protocol):
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None: ...

    async def start(self) -> object: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BotRuntimeStartResult:
    match_runtime: MatchRuntimeSyncResult
    outbox: OutboxStartupResult


class BotRuntime:
    def __init__(
        self,
        *,
        match_runtime: MatchRuntime,
        outbox_dispatcher: OutboxDispatcher | NoopOutboxDispatcher,
        logger: logging.Logger | None = None,
    ) -> None:
        self.match_runtime = match_runtime
        self.outbox_dispatcher = outbox_dispatcher
        self.logger = logger or logging.getLogger(__name__)
        self._started = False
        self._closed = False
        self._state_lock = asyncio.Lock()

    async def start(self) -> BotRuntimeStartResult:
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("BotRuntime is already closed")
            if self._started:
                raise RuntimeError("BotRuntime is already started")

            loop = asyncio.get_running_loop()
            components = self._components()
            for _, component in components:
                component.bind_loop(loop)

            tasks = [
                asyncio.create_task(component.start(), name=f"bot-runtime-start-{name}")
                for name, component in components
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            started_components: list[RuntimeLifecycle] = []
            match_runtime_started = False
            failures: list[Exception] = []
            named_results: dict[str, object] = {}

            for (name, component), result in zip(components, results, strict=True):
                if isinstance(result, Exception):
                    failures.append(result)
                    continue

                started_components.append(component)
                named_results[name] = result
                if name == "match_runtime":
                    match_runtime_started = True

            if failures:
                await self._stop_components(started_components)
                if match_runtime_started:
                    self._closed = True
                raise failures[0]

            self._started = True
            return BotRuntimeStartResult(
                match_runtime=cast(MatchRuntimeSyncResult, named_results["match_runtime"]),
                outbox=cast(OutboxStartupResult, named_results["outbox"]),
            )

    async def stop(self) -> None:
        async with self._state_lock:
            self._closed = True
            self._started = False
            components = [component for _, component in self._components()]

        stop_results = await asyncio.gather(
            *(component.stop() for component in components),
            return_exceptions=True,
        )
        failures = [result for result in stop_results if isinstance(result, Exception)]
        if not failures:
            return

        for failure in failures[1:]:
            self.logger.exception("Secondary BotRuntime shutdown failure", exc_info=failure)
        raise failures[0]

    async def _stop_components(self, components: list[RuntimeLifecycle]) -> None:
        if not components:
            return

        stop_results = await asyncio.gather(
            *(component.stop() for component in components),
            return_exceptions=True,
        )
        for failure in stop_results:
            if isinstance(failure, Exception):
                self.logger.exception(
                    "Failed to roll back BotRuntime child start",
                    exc_info=failure,
                )

    def _components(
        self,
    ) -> tuple[tuple[str, RuntimeLifecycle], tuple[str, RuntimeLifecycle]]:
        return (
            ("match_runtime", self.match_runtime),
            ("outbox", self.outbox_dispatcher),
        )
