from __future__ import annotations

from pathlib import Path

from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    MachineProfileObservation,
)
from probe_station_gui.coordinates.model import CoordinateFrameRecord
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameLoadResult,
)
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.design.frame_registration import new_design_frame_draft
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.session import DesignSession
from probe_station_gui.settings.axis_calibration_config import default_axis_calibrations
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot
from probe_station_gui.stage.types import _Status


def _document(tmp_path: Path) -> DesignDocument:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"layout")
    return DesignDocument(
        path=source,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-3,
        user_unit=1e-6,
        polygons_by_layer={},
        visible_layers=frozenset({(1, 0)}),
        bounds=(0.0, 0.0, 1000.0, 1000.0),
    )


def _draft(document: DesignDocument) -> CoordinateFrameRecord:
    return new_design_frame_draft(document, existing_names=())


class _ExplodingSnapshot:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"stale snapshot was inspected through {name}")


class _MissingAxisPose:
    def require(self, axis: str) -> float:
        raise KeyError(axis)


class _ConversionFailureSnapshot:
    physical_machine_pose = _MissingAxisPose()


def _machine_snapshot(
    x_value: float,
    y_value: float,
    b_value: float,
    *,
    work_offset: tuple[float, ...] | None = None,
) -> MachineCoordinateSnapshot:
    machine = (x_value, y_value, 0.0, 0.0, b_value, 0.0)
    offset = work_offset or (0.0,) * len(machine)
    work_position = tuple(
        machine[index] - offset[index] for index in range(len(machine))
    )
    axis_index = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}
    mapper = StageAxisCalibrationMapper(
        calibrations=default_axis_calibrations(),
        position_reporting_mode="work",
        active_work_coordinate_system="G54",
        controller_coordinate_offsets={"G54": offset},
        axis_index=axis_index,
    )
    return MachineCoordinateSnapshot.from_status(
        _Status(
            state="Idle",
            synchronized_machine_position=machine,
            display_position=work_position,
            work_position=work_position,
            work_offset=offset,
            coordinate_system="G54",
        ),
        mapper,
        axis_index,
    )


def _loaded_coordinator(
    document: DesignDocument,
    record: CoordinateFrameRecord | None,
) -> tuple[CoordinateSystemCoordinator, CoordinateFrameRegistry, DesignSession]:
    registry = CoordinateFrameRegistry()
    session = DesignSession(document=document)
    coordinator = CoordinateSystemCoordinator(registry=registry, session=session)
    load = coordinator.start(MachineProfileObservation("profile-a")).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            load.intent_id,
            CoordinateFrameLoadResult(
                load.intent_id,
                CoordinateFrameDocument(
                    records=() if record is None else (record,)
                ),
            ),
        )
    )
    if record is not None:
        session.active_frame_id = record.frame_id
        session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    return coordinator, registry, session
