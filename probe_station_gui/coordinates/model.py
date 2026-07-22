from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from .transforms import BFrameTransform


STAGE_AXES = ("X", "Y", "Z", "A", "B", "C")
VISIBLE_STAGE_AXES = ("X", "Y", "Z", "A", "B")


class FrameKind(str, Enum):
    MACHINE = "machine"
    DESIGN = "design"
    CUSTOM = "custom"


class ReadinessStatus(str, Enum):
    READY = "ready"
    MISSING = "missing"
    BLOCKED = "blocked"
    STALE = "stale"


@dataclass(frozen=True)
class AxisReadiness:
    status: ReadinessStatus
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.status is ReadinessStatus.READY


def _normalize_readiness(
    readiness: Mapping[str, AxisReadiness],
) -> Mapping[str, AxisReadiness]:
    normalized = {str(axis).strip().upper(): state for axis, state in readiness.items()}
    if set(normalized) != set(VISIBLE_STAGE_AXES):
        raise ValueError(
            "Frame readiness must contain exactly X, Y, Z, A, and B axes."
        )
    if not all(isinstance(state, AxisReadiness) for state in normalized.values()):
        raise ValueError("Frame readiness values must be AxisReadiness instances.")
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class CoordinateFrameRecord:
    frame_id: str
    kind: FrameKind
    name: str
    version: int
    transform: BFrameTransform | None
    readiness: Mapping[str, AxisReadiness]
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        try:
            UUID(self.frame_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Frame ID must be a UUID: {self.frame_id!r}.") from exc
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Frame name must not be empty.")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 0:
            raise ValueError("Frame version must be a non-negative integer.")
        if self.transform is not None and not isinstance(self.transform, BFrameTransform):
            raise ValueError("Frame transform must be a BFrameTransform or None.")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("Frame metadata must be a mapping.")
        object.__setattr__(self, "kind", FrameKind(self.kind))
        object.__setattr__(self, "readiness", _normalize_readiness(self.readiness))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def create_design(
        cls,
        *,
        frame_id: str,
        name: str,
        transform: BFrameTransform,
        readiness: Mapping[str, AxisReadiness],
        metadata: Mapping[str, object],
    ) -> CoordinateFrameRecord:
        if not isinstance(transform, BFrameTransform):
            raise ValueError("Design frame transform must be a BFrameTransform.")
        return cls(
            frame_id=frame_id,
            kind=FrameKind.DESIGN,
            name=name,
            version=0,
            transform=transform,
            readiness=readiness,
            metadata=metadata,
        )

    def with_name(self, name: str) -> CoordinateFrameRecord:
        return replace(self, name=name)

    def with_authority_block(
        self,
        axes: set[str],
        reason: str,
    ) -> CoordinateFrameRecord:
        blocked_axes = {str(axis).strip().upper() for axis in axes}
        unsupported = blocked_axes - set(VISIBLE_STAGE_AXES)
        if unsupported:
            raise ValueError(f"Unsupported readiness axes: {sorted(unsupported)!r}.")
        readiness = dict(self.readiness)
        for axis in blocked_axes:
            readiness[axis] = AxisReadiness(ReadinessStatus.BLOCKED, reason)
        return replace(self, readiness=readiness)


def normalize_axis_values(values: Mapping[str, float]) -> Mapping[str, float]:
    normalized: dict[str, float] = {}
    for raw_axis, raw_value in values.items():
        axis = str(raw_axis).strip().upper()
        if axis not in STAGE_AXES:
            raise ValueError(f"Unsupported stage axis {raw_axis!r}.")
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(f"{axis} coordinate must be finite.")
        normalized[axis] = value
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class PhysicalMachinePose:
    values: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", normalize_axis_values(self.values))

    @classmethod
    def from_mapping(cls, values: Mapping[str, float]) -> PhysicalMachinePose:
        return cls(values)

    def require(self, axis: str) -> float:
        normalized = str(axis).strip().upper()
        try:
            return self.values[normalized]
        except KeyError as exc:
            raise ValueError(f"Physical Machine {normalized} is unavailable.") from exc

    def to_dict(self) -> dict[str, float]:
        return dict(self.values)
