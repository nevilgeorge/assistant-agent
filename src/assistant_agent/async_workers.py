"""Bounded workers for synchronous SDKs used by the async web application."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import ParamSpec, TypeVar

from anyio import CapacityLimiter, to_thread

P = ParamSpec("P")
T = TypeVar("T")


class AsyncWorker:
    """Keep capacity reserved until a call finishes, including during cancellation."""

    def __init__(self, capacity: int = 4) -> None:
        self.limiter = CapacityLimiter(capacity)

    async def run(self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        return await to_thread.run_sync(
            partial(function, *args, **kwargs), limiter=self.limiter, abandon_on_cancel=False
        )
