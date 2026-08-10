"""Qt-free completion values emitted by coordinate-frame persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model import CoordinateFrameRecord

if TYPE_CHECKING:
    from .persistence import CoordinateFrameDocument


@dataclass(frozen=True)
class CoordinateFrameLoadResult:
    request_id: int
    document: CoordinateFrameDocument
    runtime_records: tuple[CoordinateFrameRecord, ...] | None = None
    provenance_diagnostics: tuple[object, ...] = ()


@dataclass(frozen=True)
class CoordinateFrameStoreSuccess:
    request_id: int
    operation: str


@dataclass(frozen=True)
class CoordinateFrameStoreFailure:
    request_id: int
    operation: str
    message: str


__all__ = [
    "CoordinateFrameLoadResult",
    "CoordinateFrameStoreFailure",
    "CoordinateFrameStoreSuccess",
]
