"""Immutable, provenance-checked Machine-coordinate snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper


class MachineCoordinateSnapshotUnavailable(ValueError):
    """A status frame cannot establish one synchronized coordinate snapshot."""


@dataclass(frozen=True)
class MachineCoordinateSnapshot:
    """One status generation plus the universal calibration used to interpret it."""

    raw_machine_position: tuple[float, ...]
    configured_position: tuple[float, ...]
    work_offset: tuple[float, ...]
    coordinate_system: str | None
    position_reporting_mode: str
    mapper: StageAxisCalibrationMapper
    axis_index: Mapping[str, int]
    physical_machine_pose: PhysicalMachinePose

    def __post_init__(self) -> None:
        object.__setattr__(self, "axis_index", MappingProxyType(dict(self.axis_index)))

    @classmethod
    def from_status(
        cls,
        status: object | None,
        mapper: StageAxisCalibrationMapper,
        axis_index: Mapping[str, int],
    ) -> "MachineCoordinateSnapshot":
        if status is None:
            raise MachineCoordinateSnapshotUnavailable(
                "A synchronized controller status is unavailable."
            )
        state = str(getattr(status, "state", "")).strip().lower()
        if state != "idle":
            raise MachineCoordinateSnapshotUnavailable(
                "Wait for the stage to stop before capturing a reference."
            )
        synchronized = getattr(status, "synchronized_machine_position", None)
        if synchronized is None:
            raise MachineCoordinateSnapshotUnavailable(
                "Synchronized Machine coordinates are unavailable."
            )
        try:
            raw_machine = tuple(float(value) for value in synchronized)
        except (TypeError, ValueError) as exc:
            raise MachineCoordinateSnapshotUnavailable(
                "Synchronized Machine coordinates are invalid."
            ) from exc
        if not raw_machine or not all(math.isfinite(value) for value in raw_machine):
            raise MachineCoordinateSnapshotUnavailable(
                "Synchronized Machine coordinates are invalid."
            )

        mode = str(mapper.position_reporting_mode).strip().lower()
        if mode == "machine":
            configured = raw_machine
            work_offset = tuple(0.0 for _ in raw_machine)
            coordinate_system = None
        else:
            work_position = getattr(status, "work_position", None)
            same_status_offset = getattr(status, "work_offset", None)
            coordinate_system = getattr(status, "coordinate_system", None)
            if work_position is None or same_status_offset is None or not coordinate_system:
                raise MachineCoordinateSnapshotUnavailable(
                    "Same-generation work-coordinate provenance is unavailable."
                )
            try:
                configured = tuple(float(value) for value in work_position)
                work_offset = tuple(float(value) for value in same_status_offset)
            except (TypeError, ValueError) as exc:
                raise MachineCoordinateSnapshotUnavailable(
                    "Same-generation work-coordinate provenance is invalid."
                ) from exc
            if (
                len(configured) != len(raw_machine)
                or len(work_offset) != len(raw_machine)
                or not all(math.isfinite(value) for value in configured + work_offset)
                or any(
                    not math.isclose(
                        configured[index] + work_offset[index],
                        raw_machine[index],
                        rel_tol=0.0,
                        abs_tol=1e-6,
                    )
                    for index in range(len(raw_machine))
                )
            ):
                raise MachineCoordinateSnapshotUnavailable(
                    "Work and Machine coordinates are not from one status generation."
                )

        physical: dict[str, float] = {}
        try:
            for raw_axis, index in axis_index.items():
                axis = str(raw_axis).strip().upper()
                if 0 <= int(index) < len(raw_machine):
                    physical[axis] = float(
                        mapper.controller_to_physical(axis, raw_machine[int(index)])
                    )
        except (TypeError, ValueError) as exc:
            raise MachineCoordinateSnapshotUnavailable(
                "Machine coordinates are outside the universal calibration domain."
            ) from exc
        return cls(
            raw_machine_position=raw_machine,
            configured_position=configured,
            work_offset=work_offset,
            coordinate_system=(None if coordinate_system is None else str(coordinate_system)),
            position_reporting_mode=mode,
            mapper=mapper,
            axis_index=axis_index,
            physical_machine_pose=PhysicalMachinePose.from_mapping(physical),
        )

    def configured_controller_to_physical_machine(
        self,
        axis: str,
        value: float,
    ) -> float:
        normalized, index = self._axis(axis)
        raw_machine = float(value)
        if self.position_reporting_mode != "machine":
            raw_machine += self.work_offset[index]
        return float(self.mapper.controller_to_physical(normalized, raw_machine))

    def physical_machine_to_configured_controller(
        self,
        axis: str,
        value: float,
    ) -> float:
        normalized, index = self._axis(axis)
        raw_machine = float(self.mapper.physical_to_controller(normalized, float(value)))
        if self.position_reporting_mode != "machine":
            raw_machine -= self.work_offset[index]
        return raw_machine

    def _axis(self, axis: str) -> tuple[str, int]:
        normalized = str(axis).strip().upper()
        index = self.axis_index.get(normalized)
        if index is None or index < 0 or index >= len(self.raw_machine_position):
            raise MachineCoordinateSnapshotUnavailable(
                f"Physical Machine {normalized} is unavailable."
            )
        return normalized, index


__all__ = [
    "MachineCoordinateSnapshot",
    "MachineCoordinateSnapshotUnavailable",
]
