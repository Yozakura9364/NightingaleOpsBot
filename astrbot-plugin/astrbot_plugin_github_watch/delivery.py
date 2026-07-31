from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol


class DeliveryStore(Protocol):
    def is_delivered(self, *, event_key: str, target_origin: str) -> bool: ...

    def mark_delivered(self, *, event_key: str, target_origin: str) -> bool: ...


async def deliver_once(
    *,
    store: DeliveryStore,
    event_key: str,
    target_origin: str,
    send: Callable[[], Awaitable[None]],
) -> bool:
    if store.is_delivered(event_key=event_key, target_origin=target_origin):
        return False
    await send()
    store.mark_delivered(event_key=event_key, target_origin=target_origin)
    return True
