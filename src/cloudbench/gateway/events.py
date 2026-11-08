"""In-process pub/sub that feeds each client's Server-Sent Events stream.

Events go either to one principal (their lease, their runs) or to everyone
(workbench status, queue length). Each subscriber has a bounded queue; a
subscriber that falls too far behind is disconnected rather than allowed to
grow memory, and its browser reconnects and refetches state.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from cloudbench.gateway.util import utcnow

_QUEUE_SIZE = 256


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    data: dict[str, Any]

    def encode(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data, default=str)}\n\n"


@dataclass(eq=False)
class Subscription:
    principal_id: str | None
    queue: asyncio.Queue[Event | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_SIZE)
    )
    overflowed: bool = False


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._by_principal: dict[str, set[Subscription]] = defaultdict(set)

    def subscribe(self, principal_id: str | None) -> Subscription:
        sub = Subscription(principal_id)
        self._subs.add(sub)
        if principal_id is not None:
            self._by_principal[principal_id].add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)
        if sub.principal_id is not None:
            subs = self._by_principal.get(sub.principal_id)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    del self._by_principal[sub.principal_id]

    def is_connected(self, principal_id: str) -> bool:
        return bool(self._by_principal.get(principal_id))

    def publish(self, type: str, data: dict[str, Any], *, to: str | None = None) -> None:
        """Send an event to one principal (``to``) or, if ``to`` is None, to everyone."""
        event = Event(type, data)
        targets = self._by_principal.get(to, set()) if to is not None else self._subs
        for sub in list(targets):
            self._offer(sub, event)

    @staticmethod
    def _offer(sub: Subscription, event: Event) -> None:
        if sub.overflowed:
            return
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            sub.overflowed = True
            # Make room for the sentinel that ends the stream.
            while not sub.queue.empty():
                sub.queue.get_nowait()
            sub.queue.put_nowait(None)


class Presence:
    """Tracks when each principal was last active, for lease idle timeouts."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._last_seen: dict[str, float] = {}

    def touch(self, principal_id: str) -> None:
        self._last_seen[principal_id] = utcnow().timestamp()

    def idle_for(self, principal_id: str) -> float:
        """Seconds since the principal was last seen; zero while an event stream is open."""
        if self._bus.is_connected(principal_id):
            return 0.0
        last = self._last_seen.get(principal_id)
        if last is None:
            return float("inf")
        return max(0.0, utcnow().timestamp() - last)
