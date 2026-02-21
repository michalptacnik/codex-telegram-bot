from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    event_type: str
    payload: str
    created_at: datetime


Subscriber = Callable[[RunEvent], None]


class EventBus:
    def __init__(self):
        self._subscribers: List[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def publish(self, run_id: str, event_type: str, payload: str = "") -> RunEvent:
        event = RunEvent(
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            created_at=datetime.now(timezone.utc),
        )
        for subscriber in self._subscribers:
            subscriber(event)
        return event

