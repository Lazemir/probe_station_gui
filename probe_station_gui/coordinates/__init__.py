"""Software coordinate primitives and transforms."""

from .model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
    STAGE_AXES,
    VISIBLE_STAGE_AXES,
    PhysicalMachinePose,
    normalize_axis_values,
)
from .registry import (
    CoordinateFrameRegistry,
    FrameVersionConflict,
    RegistrySnapshot,
    invalidate_axes,
)
from .transforms import BFrameTransform, rotate_xy

__all__ = [
    "STAGE_AXES",
    "VISIBLE_STAGE_AXES",
    "AxisReadiness",
    "BFrameTransform",
    "CoordinateFrameRecord",
    "CoordinateFrameRegistry",
    "FrameKind",
    "FrameVersionConflict",
    "PhysicalMachinePose",
    "ReadinessStatus",
    "RegistrySnapshot",
    "invalidate_axes",
    "normalize_axis_values",
    "rotate_xy",
]
