"""Production bindings for :mod:`probe_station_gui.route.contact_lifecycle`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from probe_station_gui.route.contact_lifecycle import (
    CallbackContactAutofocusAdapter,
    CallbackContactPhotoAdapter,
    ContactTask,
    RouteContactEventPort,
    RouteContactFlow,
    RouteContactInterruptPort,
    RouteContactMessage,
    RouteContactMeterPort,
    RouteContactQualityPort,
    RouteContactRequest,
    RouteContactStagePort,
)
from probe_station_gui.route.contact_quality import RouteMeasurementSample
from probe_station_gui.route.measurement_records import (
    Point2D,
    RouteContactSeekResult,
    RouteContactPlacementResult,
    RouteExternalContactPreparation,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)


class ExternalContactStageController(Protocol):
    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None,
    ) -> None: ...

    def run_external_move_to_xy(self, x: float, y: float) -> None: ...


@dataclass(frozen=True)
class ContactStageBindings:
    controller: ExternalContactStageController
    needle_feedrate: Callable[[], float | None]
    begin: Callable[[], None]
    finish: Callable[[], None]
    wait_for_background_tasks: Callable[[], None]
    lower_needles: Callable[[], None]
    contact_xy: Callable[[RouteMeasurementPoint], Point2D]
    photo_xy: Callable[[RouteMeasurementPoint], Point2D]
    settle_contact: Callable[[], bool]
    settle_photo: Callable[[], bool]


@dataclass(frozen=True)
class ContactMeterBindings:
    initial_count: Callable[[], int]
    prepare: Callable[[int], ContactTask | None]
    measure: Callable[
        [int, int, ContactTask | None], list[RouteMeasurementSample] | None
    ]
    close_output: Callable[[], None]


@dataclass(frozen=True)
class ContactQualityBindings:
    reset_seek: Callable[[], None]
    current_seek: Callable[[], RouteContactSeekResult | None]
    auto_seek_enabled: Callable[[], bool]
    set_auto_seek_enabled: Callable[[bool], None]
    record: Callable[
        [RouteMeasurementPoint, list[RouteMeasurementSample]], RouteMeasurementRecord
    ]
    placement_success: Callable[[RouteMeasurementRecord], bool]
    placement_message: Callable[[RouteContactMessage], str]


@dataclass(frozen=True)
class ContactControlBindings:
    clear_interrupt: Callable[[], None]
    interrupted: Callable[[], bool]
    status: Callable[[str], None]
    pre_contact_photo: Callable[[RouteMeasurementPoint, int, int], None]
    contact_photo: Callable[
        [RouteMeasurementPoint, RouteMeasurementRecord, int, int, bool], None
    ]


class _StagePort(RouteContactStagePort):
    def __init__(self, bindings: ContactStageBindings) -> None:
        self._bindings = bindings

    def begin(self) -> None:
        self._bindings.begin()

    def finish(self) -> None:
        self._bindings.finish()

    def wait_for_background_tasks(self) -> None:
        self._bindings.wait_for_background_tasks()

    def raise_needles(self) -> None:
        self._needles("raise")

    def lift_needles(self) -> None:
        self._needles("lift")

    def lower_needles(self) -> None:
        self._bindings.lower_needles()

    def move_to_contact(self, point: RouteMeasurementPoint) -> None:
        self._move_to(self._bindings.contact_xy(point))

    def move_to_photo(self, point: RouteMeasurementPoint) -> Point2D:
        photo_xy = self._bindings.photo_xy(point)
        self._move_to(photo_xy)
        return photo_xy

    def photo_matches_contact(
        self,
        point: RouteMeasurementPoint,
        photo_xy: Point2D,
    ) -> bool:
        contact_xy = self._bindings.contact_xy(point)
        return (
            abs(float(photo_xy[0]) - float(contact_xy[0])) <= 1e-9
            and abs(float(photo_xy[1]) - float(contact_xy[1])) <= 1e-9
        )

    def settle_contact(self) -> bool:
        return bool(self._bindings.settle_contact())

    def settle_photo(self) -> bool:
        return bool(self._bindings.settle_photo())

    def _needles(self, action: str) -> None:
        self._bindings.controller.run_external_needles_action(
            action,
            self._bindings.needle_feedrate(),
        )

    def _move_to(self, xy: Point2D) -> None:
        self._bindings.controller.run_external_move_to_xy(*xy)


class _MeterPort(RouteContactMeterPort):
    def __init__(self, bindings: ContactMeterBindings) -> None:
        self._bindings = bindings

    def initial_count(self) -> int:
        return int(self._bindings.initial_count())

    def prepare(self, count: int) -> ContactTask | None:
        return self._bindings.prepare(int(count))

    def measure(
        self,
        *,
        position: int,
        total: int,
        prepare_task: ContactTask | None,
    ) -> list[RouteMeasurementSample] | None:
        return self._bindings.measure(position, total, prepare_task)

    def close_output(self) -> None:
        self._bindings.close_output()


class _QualityPort(RouteContactQualityPort):
    def __init__(self, bindings: ContactQualityBindings) -> None:
        self._bindings = bindings

    def reset_seek(self) -> None:
        self._bindings.reset_seek()

    def current_seek(self) -> RouteContactSeekResult | None:
        return self._bindings.current_seek()

    def auto_seek_enabled(self) -> bool:
        return bool(self._bindings.auto_seek_enabled())

    def set_auto_seek_enabled(self, enabled: bool) -> None:
        self._bindings.set_auto_seek_enabled(bool(enabled))

    def record(
        self,
        point: RouteMeasurementPoint,
        samples: list[RouteMeasurementSample],
    ) -> RouteMeasurementRecord:
        return self._bindings.record(point, samples)

    def placement_success(self, record: RouteMeasurementRecord) -> bool:
        return bool(self._bindings.placement_success(record))

    def placement_message(self, message: RouteContactMessage) -> str:
        return self._bindings.placement_message(message)


class _ControlPort(RouteContactInterruptPort, RouteContactEventPort):
    def __init__(self, bindings: ContactControlBindings) -> None:
        self._bindings = bindings

    def clear(self) -> None:
        self._bindings.clear_interrupt()

    def requested(self) -> bool:
        return bool(self._bindings.interrupted())

    def status(self, message: str) -> None:
        self._bindings.status(message)

    def pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None:
        self._bindings.pre_contact_photo(point, position, total)

    def contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        success: bool,
    ) -> None:
        self._bindings.contact_photo(point, record, position, total, success)


def build_route_contact_flow(
    *,
    stage: ContactStageBindings,
    meter: ContactMeterBindings,
    quality: ContactQualityBindings,
    control: ContactControlBindings,
) -> RouteContactFlow:
    """Bind runner dependencies once and return the deep contact transaction."""

    control_port = _ControlPort(control)
    return RouteContactFlow(
        stage=_StagePort(stage),
        meter=_MeterPort(meter),
        quality=_QualityPort(quality),
        interrupt=control_port,
        events=control_port,
    )


class RouteContactRunnerAdapter:
    """Preserve runner call signatures while the flow owns contact ordering."""

    def __init__(
        self,
        flow: RouteContactFlow,
        *,
        autofocus: Callable[[RouteMeasurementPoint, int, int], object | None],
        capture_photo: Callable[
            [RouteMeasurementPoint, int, int, object | None], str
        ],
    ) -> None:
        self._flow = flow
        self._autofocus = autofocus
        self._capture_photo = capture_photo

    def place_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
        move_to_point: bool = True,
        lift_before_move: bool = True,
        lift_on_failure: bool = True,
        clear_interrupt: bool = True,
    ) -> RouteContactPlacementResult:
        result = self._flow.place_contact(
            RouteContactRequest(
                point=point,
                position=position,
                total=total,
                move_to_point=move_to_point,
                lift_before_move=lift_before_move,
                lift_on_failure=lift_on_failure,
                clear_interrupt=clear_interrupt,
            )
        )
        return result.require_placement()

    def prepare_external_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
        photo_enabled: bool = False,
        photo_focus_enabled: bool = False,
        move_to_point: bool = True,
        lift_before_move: bool = True,
        lift_on_failure: bool = False,
    ) -> RouteExternalContactPreparation:
        autofocus = (
            CallbackContactAutofocusAdapter(self._autofocus)
            if photo_focus_enabled
            else None
        )
        photo = (
            CallbackContactPhotoAdapter(self._capture_photo)
            if photo_enabled
            else None
        )
        result = self._flow.prepare_external_contact(
            RouteContactRequest(
                point=point,
                position=position,
                total=total,
                move_to_point=move_to_point,
                lift_before_move=lift_before_move,
                lift_on_failure=lift_on_failure,
                clear_interrupt=False,
                autofocus=autofocus,
                photo=photo,
            )
        )
        return result.require_preparation()

    def lift_after_external_measurement(
        self,
        *,
        position: int = 1,
        total: int = 1,
    ) -> None:
        self._flow.lift_after_external_measurement(
            position=position,
            total=total,
        )

    def check_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
    ) -> RouteContactPlacementResult:
        result = self._flow.check_contact(
            RouteContactRequest(point=point, position=position, total=total)
        )
        return result.require_placement()

    def seek_contact(
        self,
        point: RouteMeasurementPoint,
        *,
        position: int = 1,
        total: int = 1,
    ) -> RouteContactPlacementResult:
        result = self._flow.seek_contact(
            RouteContactRequest(point=point, position=position, total=total)
        )
        return result.require_placement()


__all__ = [
    "ContactControlBindings",
    "ContactMeterBindings",
    "ContactQualityBindings",
    "ContactStageBindings",
    "ExternalContactStageController",
    "RouteContactRunnerAdapter",
    "build_route_contact_flow",
]
