import subprocess
import sys

import probe_station_gui.coordinates as coordinates
from probe_station_gui.coordinates.model import (
    STAGE_AXES,
    VISIBLE_STAGE_AXES,
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    PhysicalMachinePose,
    ReadinessStatus,
    normalize_axis_values,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameStoreWorker,
    FilesystemCoordinateFrameBackend,
    FrameLoadDiagnostic,
)
from probe_station_gui.coordinates.registry import (
    CoordinateFrameRegistry,
    FrameVersionConflict,
    RegistrySnapshot,
    invalidate_axes,
)
from probe_station_gui.coordinates.transforms import BFrameTransform, rotate_xy
import probe_station_gui.settings.software_coordinates as software_coordinates
from probe_station_gui.settings.software_coordinates import (
    SOFTWARE_COORDINATE_SETTINGS_VERSION,
    CustomFrameSettings,
    RotationPivotSettings,
    SoftwareCoordinateSettings,
    parse_software_coordinate_settings,
)


def test_coordinates_package_exports_stable_core_and_store_types() -> None:
    assert set(coordinates.__all__) == {
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
    }
    assert coordinates.STAGE_AXES is STAGE_AXES
    assert coordinates.VISIBLE_STAGE_AXES is VISIBLE_STAGE_AXES
    assert coordinates.AxisReadiness is AxisReadiness
    assert coordinates.BFrameTransform is BFrameTransform
    assert coordinates.CoordinateFrameDocument is CoordinateFrameDocument
    assert coordinates.CoordinateFrameRecord is CoordinateFrameRecord
    assert coordinates.CoordinateFrameRegistry is CoordinateFrameRegistry
    assert coordinates.CoordinateFrameStoreWorker is CoordinateFrameStoreWorker
    assert coordinates.FilesystemCoordinateFrameBackend is FilesystemCoordinateFrameBackend
    assert coordinates.FrameKind is FrameKind
    assert coordinates.FrameLoadDiagnostic is FrameLoadDiagnostic
    assert coordinates.FrameVersionConflict is FrameVersionConflict
    assert coordinates.PhysicalMachinePose is PhysicalMachinePose
    assert coordinates.ReadinessStatus is ReadinessStatus
    assert coordinates.RegistrySnapshot is RegistrySnapshot
    assert coordinates.invalidate_axes is invalidate_axes
    assert coordinates.normalize_axis_values is normalize_axis_values
    assert coordinates.rotate_xy is rotate_xy


def test_software_coordinate_settings_types_remain_importable() -> None:
    assert set(software_coordinates.__all__) == {
        "SOFTWARE_COORDINATE_SETTINGS_VERSION",
        "CustomFrameSettings",
        "RotationPivotSettings",
        "SoftwareCoordinateSettings",
        "parse_software_coordinate_settings",
    }
    assert SOFTWARE_COORDINATE_SETTINGS_VERSION == 1
    assert CustomFrameSettings.__module__ == software_coordinates.__name__
    assert RotationPivotSettings.__module__ == software_coordinates.__name__
    assert SoftwareCoordinateSettings.__module__ == software_coordinates.__name__
    assert parse_software_coordinate_settings.__module__ == software_coordinates.__name__


def test_design_and_coordinates_packages_are_import_order_independent() -> None:
    for script in (
        "import probe_station_gui.design.session; import probe_station_gui.coordinates",
        "import probe_station_gui.coordinates; import probe_station_gui.design.session",
    ):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
