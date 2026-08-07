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
from .persistence import (
    CoordinateFrameDocument,
    CoordinateFrameStoreWorker,
    FilesystemCoordinateFrameBackend,
    FrameLoadDiagnostic,
)
from .transforms import BFrameTransform, rotate_xy
from .lifecycle import (
    MACHINE_FRAME_ID,
    CoordinateFrameLifecycle,
    DesignFrameUsabilitySnapshot,
    DesignUsabilityContext,
    FrameSelectionContext,
    FrameSelectionDecision,
)

__all__ = [
    "STAGE_AXES",
    "VISIBLE_STAGE_AXES",
    "AxisReadiness",
    "BFrameTransform",
    "CoordinateFrameLifecycle",
    "CoordinateFrameDocument",
    "CoordinateFrameRecord",
    "CoordinateFrameRegistry",
    "CoordinateFrameStoreWorker",
    "DesignFrameUsabilitySnapshot",
    "DesignUsabilityContext",
    "FilesystemCoordinateFrameBackend",
    "FrameKind",
    "FrameLoadDiagnostic",
    "FrameSelectionContext",
    "FrameSelectionDecision",
    "FrameVersionConflict",
    "PhysicalMachinePose",
    "MACHINE_FRAME_ID",
    "ReadinessStatus",
    "RegistrySnapshot",
    "invalidate_axes",
    "normalize_axis_values",
    "rotate_xy",
]
