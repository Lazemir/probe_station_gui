"""Explicit production ports for the route contact lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from probe_station_gui.route.contact_lifecycle import ContactTask, RouteContactMessage
from probe_station_gui.route.contact_quality import RouteMeasurementSample
from probe_station_gui.route.measurement_records import (
    Point2D,
    RouteContactSeekResult,
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


class ContactMeasure(Protocol):
    def __call__(
        self,
        *,
        position: int,
        total: int,
        prepare_task: ContactTask | None,
    ) -> list[RouteMeasurementSample] | None: ...


@dataclass(frozen=True)
class ContactStageBindings:
    """Stage callables that directly satisfy ``RouteContactStagePort``."""

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

    def raise_needles(self) -> None:
        self._needles("raise")

    def lift_needles(self) -> None:
        self._needles("lift")

    def move_to_contact(self, point: RouteMeasurementPoint) -> None:
        self._move_to(self.contact_xy(point))

    def move_to_photo(self, point: RouteMeasurementPoint) -> Point2D:
        photo_xy = self.photo_xy(point)
        self._move_to(photo_xy)
        return photo_xy

    def photo_matches_contact(
        self,
        point: RouteMeasurementPoint,
        photo_xy: Point2D,
    ) -> bool:
        contact_xy = self.contact_xy(point)
        return (
            abs(float(photo_xy[0]) - float(contact_xy[0])) <= 1e-9
            and abs(float(photo_xy[1]) - float(contact_xy[1])) <= 1e-9
        )

    def _needles(self, action: str) -> None:
        self.controller.run_external_needles_action(
            action,
            self.needle_feedrate(),
        )

    def _move_to(self, xy: Point2D) -> None:
        self.controller.run_external_move_to_xy(*xy)


@dataclass(frozen=True)
class ContactMeterBindings:
    """Meter callables that directly satisfy ``RouteContactMeterPort``."""

    initial_count: Callable[[], int]
    prepare: Callable[[int], ContactTask | None]
    measure: ContactMeasure
    close_output: Callable[[], None]


@dataclass(frozen=True)
class ContactQualityBindings:
    """Quality callables that directly satisfy ``RouteContactQualityPort``."""

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
    """Control callables satisfying the interrupt and event ports directly."""

    clear: Callable[[], None]
    requested: Callable[[], bool]
    status: Callable[[str], None]
    pre_contact_photo: Callable[[RouteMeasurementPoint, int, int], None]
    contact_photo: Callable[
        [RouteMeasurementPoint, RouteMeasurementRecord, int, int, bool], None
    ]


__all__ = [
    "ContactControlBindings",
    "ContactMeterBindings",
    "ContactQualityBindings",
    "ContactStageBindings",
    "ExternalContactStageController",
]
