"""Route measurement dialog defaults and session planning helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from probe_station_gui.route.measurement_settings import RouteMeasurementSettingsStore


@dataclass(frozen=True)
class RouteDialogDefaults:
    csv_path: str
    photo_dir: str


@dataclass(frozen=True)
class RouteDialogRestorePlan:
    session_active: bool
    current_point: int | None
    open_dialog: bool


@dataclass(frozen=True)
class RouteMeasurementSessionStartPlan:
    accepted: bool
    point_number: int | None
    session_active: bool
    pending: bool
    status_message: str
    status_timeout_ms: int


@dataclass(frozen=True)
class RouteMeasurementSessionCancelPlan:
    accepted: bool
    point_number: int | None
    session_active: bool
    pending: bool
    status_message: str
    status_timeout_ms: int


@dataclass(frozen=True)
class RouteDialogOpenState:
    session_active: bool
    current_point: int | None
    running: bool
    waiting: bool
    idle_status_message: str | None


def route_dialog_defaults(route: object, document: object | None) -> RouteDialogDefaults:
    route_path = getattr(route, "path", None)
    if route_path is not None:
        path = Path(route_path)
        return RouteDialogDefaults(
            csv_path=str(path.with_name(f"{path.stem}-measurements.csv")),
            photo_dir=str(path.with_name(f"{path.stem}-photos")),
        )
    if document is not None:
        base_dir = Path(getattr(document, "path")).parent
        return RouteDialogDefaults(
            csv_path=str(base_dir / "probe_route_measurements.csv"),
            photo_dir=str(base_dir / "probe_route_photos"),
        )
    return RouteDialogDefaults(
        csv_path="probe_route_measurements.csv",
        photo_dir="probe_route_photos",
    )


def route_dialog_restore_plan(
    state: dict[str, object],
    route: object,
) -> RouteDialogRestorePlan:
    current_point = RouteMeasurementSettingsStore.current_point(state)
    session_active = RouteMeasurementSettingsStore.session_active(state)
    if session_active and not RouteMeasurementSettingsStore.settings_match_route(
        state,
        route,
    ):
        session_active = False
    return RouteDialogRestorePlan(
        session_active=session_active,
        current_point=current_point,
        open_dialog=session_active,
    )


def route_measurement_session_start_plan(
    *,
    thread_active: bool,
    dialog_configuration: object | None,
    current_point: int | None,
) -> RouteMeasurementSessionStartPlan:
    if thread_active:
        return RouteMeasurementSessionStartPlan(
            accepted=False,
            point_number=None,
            session_active=False,
            pending=False,
            status_message="Route measurement is already active.",
            status_timeout_ms=4000,
        )
    point_number = int(
        getattr(dialog_configuration, "current_point", current_point or 1)
    )
    return RouteMeasurementSessionStartPlan(
        accepted=True,
        point_number=point_number,
        session_active=True,
        pending=True,
        status_message=f"Route point set to point {point_number}.",
        status_timeout_ms=5000,
    )


def route_measurement_session_cancel_plan(
    *,
    thread_active: bool,
) -> RouteMeasurementSessionCancelPlan:
    if thread_active:
        return RouteMeasurementSessionCancelPlan(
            accepted=False,
            point_number=None,
            session_active=False,
            pending=False,
            status_message="Stop route measurement before canceling the session.",
            status_timeout_ms=5000,
        )
    return RouteMeasurementSessionCancelPlan(
        accepted=True,
        point_number=1,
        session_active=False,
        pending=False,
        status_message="Route measurement session cancelled.",
        status_timeout_ms=5000,
    )


def route_dialog_open_state(
    *,
    session_active: bool,
    current_point: int | None,
    thread_active: bool,
    waiting: bool,
) -> RouteDialogOpenState:
    return RouteDialogOpenState(
        session_active=bool(session_active),
        current_point=current_point,
        running=bool(thread_active),
        waiting=bool(waiting),
        idle_status_message=(
            "Choose a point, then Measure or Move."
            if session_active and not thread_active
            else None
        ),
    )


def open_or_update_route_measurement_dialog(
    *,
    dialog: object | None,
    route: object,
    document: object | None,
    meter_type: str,
    settings_path: Path,
    parent: object,
    session_active: bool,
    current_point: int | None,
    thread_active: bool,
    waiting: bool,
    create_dialog: Callable[..., Any],
) -> object:
    defaults = route_dialog_defaults(route, document)
    if dialog is None:
        dialog = create_dialog(
            route_name=getattr(route, "name", ""),
            route_point_count=len(getattr(route, "points", ())),
            default_csv_path=defaults.csv_path,
            default_photo_dir=defaults.photo_dir,
            default_meter_type=meter_type,
            settings_path=settings_path,
            parent=parent,
        )
    else:
        dialog.set_route(
            route_name=getattr(route, "name", ""),
            route_point_count=len(getattr(route, "points", ())),
            default_csv_path=defaults.csv_path,
            default_photo_dir=defaults.photo_dir,
        )
    _apply_route_dialog_open_state(
        dialog,
        route_dialog_open_state(
            session_active=session_active,
            current_point=current_point,
            thread_active=thread_active,
            waiting=waiting,
        ),
    )
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def _apply_route_dialog_open_state(dialog: object, state: RouteDialogOpenState) -> None:
    dialog.set_measurement_session_active(state.session_active, save=False)
    if state.current_point is not None:
        dialog.set_current_point(state.current_point, save=False)
    if state.running:
        dialog.set_running(True)
        dialog.set_waiting(state.waiting)
    elif state.idle_status_message is not None:
        dialog.set_status(state.idle_status_message)


__all__ = [
    "RouteDialogDefaults",
    "RouteDialogOpenState",
    "RouteDialogRestorePlan",
    "RouteMeasurementSessionCancelPlan",
    "RouteMeasurementSessionStartPlan",
    "open_or_update_route_measurement_dialog",
    "route_dialog_defaults",
    "route_dialog_open_state",
    "route_dialog_restore_plan",
    "route_measurement_session_cancel_plan",
    "route_measurement_session_start_plan",
]
