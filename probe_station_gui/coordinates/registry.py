from __future__ import annotations

from dataclasses import dataclass, replace
import threading
from typing import Iterable

from .model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
    VISIBLE_STAGE_AXES,
)


@dataclass(frozen=True)
class RegistrySnapshot:
    generation: int
    records: tuple[CoordinateFrameRecord, ...]


class FrameVersionConflict(RuntimeError):
    def __init__(self, frame_id: str) -> None:
        self.frame_id = frame_id
        super().__init__(f"Frame {frame_id} has changed.")


class CoordinateFrameRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._records: dict[str, CoordinateFrameRecord] = {}

    def add(self, record: CoordinateFrameRecord) -> CoordinateFrameRecord:
        with self._lock:
            if record.frame_id in self._records:
                raise ValueError(f"Frame {record.frame_id} already exists.")
            self._records[record.frame_id] = record
            self._generation += 1
            return record

    def get(self, frame_id: str) -> CoordinateFrameRecord | None:
        with self._lock:
            return self._records.get(str(frame_id))

    def reset(self, records: Iterable[CoordinateFrameRecord]) -> RegistrySnapshot:
        """Atomically install one loaded durable frame document."""

        loaded: dict[str, CoordinateFrameRecord] = {}
        for record in records:
            if record.frame_id in loaded:
                raise ValueError(f"Frame {record.frame_id} appears more than once.")
            loaded[record.frame_id] = record
        with self._lock:
            self._records = loaded
            self._generation += 1
        return self.snapshot()

    def replace(
        self,
        record: CoordinateFrameRecord,
        *,
        expected_version: int,
    ) -> CoordinateFrameRecord:
        with self._lock:
            current = self._records.get(record.frame_id)
            if current is None:
                raise KeyError(record.frame_id)
            if current.version != expected_version:
                raise FrameVersionConflict(record.frame_id)
            updated = replace(record, version=current.version + 1)
            self._records[record.frame_id] = updated
            self._generation += 1
            return updated

    def snapshot(self) -> RegistrySnapshot:
        with self._lock:
            records = tuple(
                sorted(
                    self._records.values(),
                    key=lambda record: (
                        {
                            FrameKind.MACHINE: 0,
                            FrameKind.DESIGN: 1,
                            FrameKind.CUSTOM: 2,
                        }[record.kind],
                        record.name,
                    ),
                )
            )
            return RegistrySnapshot(self._generation, records)


def invalidate_axes(
    record: CoordinateFrameRecord,
    axes: Iterable[str],
    reason: str,
) -> CoordinateFrameRecord:
    changed_axes = {str(axis).strip().upper() for axis in axes}
    unsupported = changed_axes - set(VISIBLE_STAGE_AXES)
    if unsupported:
        raise ValueError(f"Unsupported readiness axes: {sorted(unsupported)!r}.")
    if changed_axes & {"X", "Y", "B"}:
        stale_axes = {"X", "Y", "B", "Z", "A"}
    elif "Z" in changed_axes:
        stale_axes = {"Z", "A"}
    elif "A" in changed_axes:
        stale_axes = {"A"}
    else:
        return record
    readiness = dict(record.readiness)
    for axis in stale_axes:
        readiness[axis] = AxisReadiness(ReadinessStatus.STALE, reason)
    return replace(record, readiness=readiness)
