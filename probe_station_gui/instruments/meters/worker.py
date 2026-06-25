"""Worker queue primitives for measurement instrument controllers."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable


@dataclass
class MeterWorkerCall:
    func: Callable[[], object]
    done: threading.Event | None = None
    result: object = None
    error: BaseException | None = None


def meter_worker_poll_timeout(
    *,
    live_polling_enabled: bool,
    stop_polling: bool,
    has_session: bool,
    poll_interval_ms: int,
) -> float | None:
    if not live_polling_enabled or stop_polling or not has_session:
        return None
    return max(0.05, poll_interval_ms / 1000.0)
