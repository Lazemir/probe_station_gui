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
