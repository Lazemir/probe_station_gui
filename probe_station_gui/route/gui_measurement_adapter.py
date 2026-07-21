"""Typed GUI wiring for route point events and meter startup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from probe_station_gui.instruments.meters.lcr import LCRMeterError
from probe_station_gui.route.measurement_records import (
    RouteContactHeightRecord,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RoutePhotoRecord,
)
from probe_station_gui.route.meter_config import RouteMeterConfiguration
from probe_station_gui.route.point_execution import PointPhotoSettings
from probe_station_gui.route.point_execution_adapters import RouteMeasurementEvents
from probe_station_gui.route.runtime_presenter import RouteRuntimePresentationSink


class RoutePhotoCapture(Protocol):
    def __call__(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        settings: PointPhotoSettings,
        focus_result: object | None = None,
    ) -> str | Path: ...


class RoutePhotoAutofocus(Protocol):
    def __call__(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        settings: PointPhotoSettings,
    ) -> object | None: ...


class RouteContactHeightWriter(Protocol):
    def __call__(
        self,
        record: RouteContactHeightRecord,
        position: int,
        total: int,
        *,
        csv_path: str | Path,
    ) -> None: ...


class RouteMeterSetupPort(Protocol):
    def is_connected(self) -> bool: ...

    def apply_route_meter_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None: ...

    def apply_route_meter_runtime_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None: ...


@dataclass(frozen=True)
class GuiRouteEventBindings:
    """Build callbacks that consume one immutable point-settings snapshot."""

    status: Callable[[str], None]
    progress: Callable[[int, int, int], None]
    record: Callable[[RouteMeasurementRecord, int, int], None]
    capture_photo: RoutePhotoCapture
    autofocus: RoutePhotoAutofocus
    photo_record: Callable[[RoutePhotoRecord, int, int], None]
    contact_height: RouteContactHeightWriter
    contact_photo: Callable[
        [RouteMeasurementPoint, RouteMeasurementRecord, int, int, bool], None
    ]
    pre_contact_photo: Callable[[RouteMeasurementPoint, int, int], None]
    result: Callable[[RouteMeasurementRecord, int, int, bool], None]
    waiting: Callable[[bool], None]

    def events(self) -> RouteMeasurementEvents:
        return RouteMeasurementEvents(
            status=self.status,
            progress=self.progress,
            record=self.record,
            point_photo=self._capture_photo,
            point_photo_focus=self._autofocus,
            photo_record=self.photo_record,
            point_contact_height=self._contact_height,
            contact_photo=self.contact_photo,
            pre_contact_photo=self.pre_contact_photo,
            result=self.result,
            waiting=self.waiting,
        )

    def _capture_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        focus_result: object | None,
        settings: PointPhotoSettings,
    ) -> str | Path:
        return self.capture_photo(
            point,
            position,
            total,
            settings=settings,
            focus_result=focus_result,
        )

    def _autofocus(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        settings: PointPhotoSettings,
    ) -> object | None:
        return self.autofocus(
            point,
            position,
            total,
            settings=settings,
        )

    def _contact_height(
        self,
        record: RouteContactHeightRecord,
        position: int,
        total: int,
        settings: PointPhotoSettings,
    ) -> None:
        self.contact_height(
            record,
            position,
            total,
            csv_path=settings.csv_path,
        )


def setup_gui_route_meter(
    *,
    controller: RouteMeterSetupPort,
    configuration: RouteMeterConfiguration,
    measure_enabled: bool,
    show_status: Callable[[str, int], None],
    presenter: RouteRuntimePresentationSink,
) -> object | None:
    """Configure the meter or reject GUI route startup as one operation."""

    if not measure_enabled:
        return object()
    try:
        if controller.is_connected():
            controller.apply_route_meter_configuration(configuration)
        else:
            controller.apply_route_meter_runtime_configuration(configuration)
    except LCRMeterError as exc:
        message = f"Route measurement instrument setup failed: {exc}"
        show_status(message, 8000)
        presenter.set_running(False)
        presenter.set_status(message)
        return None
    return controller


__all__ = [
    "GuiRouteEventBindings",
    "RouteContactHeightWriter",
    "RouteMeterSetupPort",
    "RoutePhotoAutofocus",
    "RoutePhotoCapture",
    "setup_gui_route_meter",
]
