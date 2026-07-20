"""Compose measured axis curves with controller coordinate systems."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from probe_station_gui.settings.axis_calibration_config import AxisCalibrationSettings
from probe_station_gui.stage.axis_mapping import (
    AxisCalibrationCurve,
    controller_to_physical,
    curve_from_settings,
    physical_to_controller,
)


class CalibrationCoordinateUnavailable(ValueError):
    """Required controller coordinate-system state is not available."""


@dataclass(frozen=True)
class StageAxisCalibrationMapper:
    """Map all calibrated user coordinates without performing hardware I/O."""

    calibrations: Mapping[str, AxisCalibrationSettings | AxisCalibrationCurve]
    position_reporting_mode: str
    active_work_coordinate_system: str | None
    controller_coordinate_offsets: Mapping[str, Sequence[float]]
    axis_index: Mapping[str, int]
    _curves: Mapping[str, AxisCalibrationCurve] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_curves",
            {
                axis.upper(): curve
                for axis, settings in self.calibrations.items()
                if (
                    curve := (
                        settings
                        if isinstance(settings, AxisCalibrationCurve)
                        else curve_from_settings(settings)
                    )
                )
                is not None
            },
        )

    def has_calibration(self, axis: str) -> bool:
        return self._curve(axis) is not None

    def controller_to_physical(self, axis: str, value: float) -> float:
        curve = self._curve(axis)
        return float(value) if curve is None else controller_to_physical(curve, value)

    def physical_to_controller(self, axis: str, value: float) -> float:
        curve = self._curve(axis)
        return float(value) if curve is None else physical_to_controller(curve, value)

    def controller_domain(self, axis: str) -> tuple[float, float] | None:
        curve = self._curve(axis)
        return None if curve is None else (curve.controller[0], curve.controller[-1])

    def physical_domain(self, axis: str) -> tuple[float, float] | None:
        curve = self._curve(axis)
        return None if curve is None else (curve.physical[0], curve.physical[-1])

    def machine_controller_to_physical(self, axis: str, value: float) -> float:
        """Map a cached raw machine coordinate for the settings preview."""

        return self.controller_to_physical(axis, value)

    def configured_controller_to_physical(
        self,
        axis: str,
        value: float,
        status: object | None = None,
    ) -> float:
        curve = self._curve(axis)
        if curve is None:
            return float(value)
        origin = self._configured_origin(axis, status)
        machine_value = float(value) + origin
        return controller_to_physical(curve, machine_value) - controller_to_physical(
            curve,
            origin,
        )

    def physical_to_configured_controller(
        self,
        axis: str,
        value: float,
        status: object | None = None,
    ) -> float:
        curve = self._curve(axis)
        if curve is None:
            return float(value)
        origin = self._configured_origin(axis, status)
        physical_origin = controller_to_physical(curve, origin)
        machine_value = physical_to_controller(curve, float(value) + physical_origin)
        return machine_value - origin

    def controller_domain_for_configured_mode(
        self,
        axis: str,
        status: object | None = None,
    ) -> tuple[float, float] | None:
        domain = self.controller_domain(axis)
        if domain is None:
            return None
        origin = self._configured_origin(axis, status)
        return domain[0] - origin, domain[1] - origin

    def physical_domain_for_configured_mode(
        self,
        axis: str,
        status: object | None = None,
    ) -> tuple[float, float] | None:
        curve = self._curve(axis)
        if curve is None:
            return None
        origin = self._configured_origin(axis, status)
        physical_origin = controller_to_physical(curve, origin)
        return (
            curve.physical[0] - physical_origin,
            curve.physical[-1] - physical_origin,
        )

    def axis_work_offset_for_configured_mode(
        self,
        axis: str,
        status: object | None = None,
    ) -> float:
        if self.position_reporting_mode == "machine":
            return 0.0
        offset = self._work_offset(axis, status)
        return 0.0 if offset is None else offset

    def position_for_configured_mode(
        self,
        status: object | None,
    ) -> tuple[float, ...] | None:
        if status is None:
            return None
        position = (
            getattr(status, "position", None)
            if self.position_reporting_mode == "machine"
            else getattr(status, "work_position", None)
        )
        if position is None:
            return None
        return tuple(float(value) for value in position)

    def axis_value_for_configured_mode(
        self,
        status: object | None,
        axis: str,
    ) -> float | None:
        position = self.position_for_configured_mode(status)
        index = self.axis_index.get(str(axis).upper())
        if position is None or index is None or index >= len(position):
            return None
        return position[index]

    def axis_limits_for_configured_mode(
        self,
        axis: str,
        limits: tuple[float, float] | None,
        status: object | None,
    ) -> tuple[float, float] | None:
        if limits is None:
            return None
        if self.position_reporting_mode == "machine":
            return float(limits[0]), float(limits[1])
        offset = self._work_offset(axis, status)
        if offset is None:
            return None
        return float(limits[0]) - offset, float(limits[1]) - offset

    def _configured_origin(self, axis: str, status: object | None) -> float:
        if self.position_reporting_mode == "machine":
            return 0.0
        offset = self._work_offset(axis, status)
        if offset is None:
            raise CalibrationCoordinateUnavailable(
                f"{str(axis).upper()} work offset is unavailable."
            )
        return offset

    def _work_offset(self, axis: str, status: object | None) -> float | None:
        index = self.axis_index.get(str(axis).upper())
        if index is None:
            return None
        status_offset = None if status is None else getattr(status, "work_offset", None)
        coordinate_system = (
            None if status is None else getattr(status, "coordinate_system", None)
        ) or self.active_work_coordinate_system
        offset = status_offset
        if offset is None and coordinate_system:
            offset = self.controller_coordinate_offsets.get(coordinate_system)
        if offset is None or index >= len(offset):
            return None
        return float(offset[index])

    def _curve(self, axis: str) -> AxisCalibrationCurve | None:
        return self._curves.get(str(axis).upper())
