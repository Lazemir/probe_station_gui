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
from .lifecycle import (
    MACHINE_FRAME_ID,
    CoordinateFrameLifecycle,
    DesignFrameUsabilitySnapshot,
    DesignUsabilityContext,
    FrameSelectionContext,
    FrameSelectionDecision,
)
from .coordinator import CoordinateSystemCoordinator
from .coordinator_model import (
    CoordinateAdapterCompletion,
    CoordinateNotice,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    DesignSessionCheckpoint,
    DesignSessionFrameLink,
    FrameRecordsPublication,
    LoadCoordinateFramesIntent,
    MachineProfileObservation,
    SaveCoordinateFramesIntent,
)

_PERSISTENCE_EXPORTS = frozenset(
    {
        "CoordinateFrameDocument",
        "CoordinateFrameStoreWorker",
        "FilesystemCoordinateFrameBackend",
        "FrameLoadDiagnostic",
    }
)


def __getattr__(name: str) -> object:
    if name not in _PERSISTENCE_EXPORTS:
        raise AttributeError(name)
    from . import persistence

    value = getattr(persistence, name)
    globals()[name] = value
    return value

__all__ = [
    "STAGE_AXES",
    "VISIBLE_STAGE_AXES",
    "AxisReadiness",
    "BFrameTransform",
    "CoordinateFrameLifecycle",
    "CoordinateAdapterCompletion",
    "CoordinateNotice",
    "CoordinateSystemCoordinator",
    "CoordinateSystemSnapshot",
    "CoordinateTransition",
    "CoordinateFrameDocument",
    "CoordinateFrameRecord",
    "CoordinateFrameRegistry",
    "CoordinateFrameStoreWorker",
    "DesignFrameUsabilitySnapshot",
    "DesignSessionCheckpoint",
    "DesignSessionFrameLink",
    "DesignUsabilityContext",
    "FilesystemCoordinateFrameBackend",
    "FrameKind",
    "FrameRecordsPublication",
    "FrameLoadDiagnostic",
    "FrameSelectionContext",
    "FrameSelectionDecision",
    "FrameVersionConflict",
    "PhysicalMachinePose",
    "MACHINE_FRAME_ID",
    "LoadCoordinateFramesIntent",
    "MachineProfileObservation",
    "ReadinessStatus",
    "RegistrySnapshot",
    "SaveCoordinateFramesIntent",
    "invalidate_axes",
    "normalize_axis_values",
    "rotate_xy",
]
