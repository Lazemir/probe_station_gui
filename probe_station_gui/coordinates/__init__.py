"""Software coordinate primitives and transforms."""

from importlib import import_module

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
    FrameVersionConflict,
    invalidate_axes,
)
from .transforms import BFrameTransform, rotate_xy
from .lifecycle import MACHINE_FRAME_ID
from .coordinator_model import (
    CoordinateAdapterCompletion,
    CoordinateAuthorityObservation,
    CoordinateMotionLease,
    CoordinateMotionProjection,
    CoordinateMotionRequest,
    CoordinateNotice,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    CoordinateSystemSelection,
    CustomSystemsRequest,
    DesignCoordinateLease,
    LoadCoordinateFramesIntent,
    MachineProfileObservation,
    SaveCoordinateFramesIntent,
)

_LAZY_EXPORT_MODULES = {
    "CoordinateSystemCoordinator": "coordinator",
    "CoordinateFrameDocument": "persistence",
    "CoordinateFrameStoreWorker": "persistence",
    "FilesystemCoordinateFrameBackend": "persistence",
    "FrameLoadDiagnostic": "persistence",
}


def __getattr__(name: str) -> object:
    module_name = _LAZY_EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "STAGE_AXES",
    "VISIBLE_STAGE_AXES",
    "AxisReadiness",
    "BFrameTransform",
    "CoordinateAdapterCompletion",
    "CoordinateAuthorityObservation",
    "CoordinateMotionLease",
    "CoordinateMotionProjection",
    "CoordinateMotionRequest",
    "CoordinateNotice",
    "CoordinateSystemCoordinator",
    "CoordinateSystemSnapshot",
    "CoordinateTransition",
    "CoordinateFrameDocument",
    "CoordinateFrameRecord",
    "CoordinateFrameStoreWorker",
    "CoordinateSystemSelection",
    "CustomSystemsRequest",
    "DesignCoordinateLease",
    "FilesystemCoordinateFrameBackend",
    "FrameKind",
    "FrameLoadDiagnostic",
    "FrameVersionConflict",
    "PhysicalMachinePose",
    "MACHINE_FRAME_ID",
    "LoadCoordinateFramesIntent",
    "MachineProfileObservation",
    "ReadinessStatus",
    "SaveCoordinateFramesIntent",
    "invalidate_axes",
    "normalize_axis_values",
    "rotate_xy",
]
