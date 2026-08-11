"""Immutable commands, publications, and worker events for Design snapping."""

from __future__ import annotations

from dataclasses import dataclass

from .klayout_types import SnapFailure, SnapRequest, SnapResponse
from .model import Point2D, SnapResult
from .plot_interaction import SnapClickIntent


SourceKey = tuple[object, str | None]


@dataclass(frozen=True)
class AttachWorker:
    worker_token: int
    source_key: SourceKey


@dataclass(frozen=True)
class ReplaceWorker:
    worker_token: int
    source_key: SourceKey
    timeout_s: float = 0.0


@dataclass(frozen=True)
class DetachWorker:
    worker_token: int
    timeout_s: float = 0.0


@dataclass(frozen=True)
class SubmitHover:
    worker_token: int
    request: SnapRequest


@dataclass(frozen=True)
class SubmitClick:
    worker_token: int
    request: SnapRequest


@dataclass(frozen=True)
class CancelHover:
    worker_token: int


@dataclass(frozen=True)
class CancelPending:
    worker_token: int


SnapCommand = (
    AttachWorker
    | CancelHover
    | CancelPending
    | DetachWorker
    | ReplaceWorker
    | SubmitClick
    | SubmitHover
)


@dataclass(frozen=True)
class HoverPublication:
    result: SnapResult | None
    shift: bool
    control: bool
    raw_point: Point2D | None
    elapsed_ms: float | None
    generation: int | None = None


@dataclass(frozen=True)
class ClickPublication:
    intent: SnapClickIntent
    result: SnapResult
    elapsed_ms: float | None = None


SnapPublication = HoverPublication | ClickPublication


@dataclass(frozen=True)
class SnapNotice:
    kind: str
    message: str


@dataclass(frozen=True)
class SnapTransition:
    commands: tuple[SnapCommand, ...] = ()
    publications: tuple[SnapPublication, ...] = ()
    notices: tuple[SnapNotice, ...] = ()


@dataclass(frozen=True)
class WorkerResponseEvent:
    worker_token: int
    response: SnapResponse


@dataclass(frozen=True)
class WorkerFailureEvent:
    worker_token: int
    failure: SnapFailure


@dataclass(frozen=True)
class WorkerLifecycleFailedEvent:
    worker_token: int
    message: str


SnapWorkerEvent = WorkerResponseEvent | WorkerFailureEvent | WorkerLifecycleFailedEvent


__all__ = [
    "AttachWorker",
    "CancelHover",
    "CancelPending",
    "ClickPublication",
    "DetachWorker",
    "HoverPublication",
    "ReplaceWorker",
    "SnapCommand",
    "SnapNotice",
    "SnapPublication",
    "SnapTransition",
    "SnapWorkerEvent",
    "SourceKey",
    "SubmitClick",
    "SubmitHover",
    "WorkerFailureEvent",
    "WorkerLifecycleFailedEvent",
    "WorkerResponseEvent",
]
