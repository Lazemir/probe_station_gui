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


@dataclass(frozen=True)
class RouteDialogHandlers:
    measure_requested: Callable[[object], None]
    start_session_requested: Callable[[], None]
    cancel_session_requested: Callable[[], None]
    next_requested: Callable[[], None]
    remeasure_requested: Callable[[], None]
    measure_current_requested: Callable[[], None]
    skip_requested: Callable[[], None]
    save_shift_requested: Callable[[int], None]
    interrupt_requested: Callable[..., None]
    pause_requested: Callable[[], None]
    stop_requested: Callable[[], None]
    jump_requested: Callable[[object], None]
    move_requested: Callable[..., None]
    current_point_changed: Callable[[int], None]
    finished: Callable[[int], None]


def route_dialog_handlers(owner: object) -> RouteDialogHandlers:
    submit_confirmation = getattr(owner, "_submit_route_measurement_confirmation")
    return RouteDialogHandlers(
        measure_requested=getattr(owner, "_start_route_measurement"),
        start_session_requested=getattr(owner, "_start_route_measurement_session"),
        cancel_session_requested=getattr(owner, "_cancel_route_measurement_session"),
        next_requested=lambda: submit_confirmation("next"),
        remeasure_requested=lambda: submit_confirmation("remeasure"),
        measure_current_requested=lambda: submit_confirmation("measure"),
        skip_requested=lambda: submit_confirmation("skip"),
        save_shift_requested=getattr(owner, "_save_route_measurement_shift"),
        interrupt_requested=getattr(owner, "_request_route_measurement_point_correction"),
        pause_requested=getattr(owner, "_request_pause_route_measurement"),
        stop_requested=getattr(owner, "_request_stop_route_measurement"),
        jump_requested=getattr(owner, "_submit_route_measurement_jump"),
        move_requested=getattr(owner, "_request_route_contact_move"),
        current_point_changed=getattr(owner, "_on_route_measurement_current_point_changed"),
        finished=lambda _result: getattr(owner, "_clear_route_measurement_dialog")(),
    )


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
    handlers: RouteDialogHandlers,
) -> object:
    defaults = route_dialog_defaults(route, document)
    if dialog is None:
        dialog = _create_route_measurement_dialog(
            route_name=getattr(route, "name", ""),
            route_point_count=len(getattr(route, "points", ())),
            default_csv_path=defaults.csv_path,
            default_photo_dir=defaults.photo_dir,
            default_meter_type=meter_type,
            settings_path=settings_path,
            parent=parent,
            handlers=handlers,
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


def _create_route_measurement_dialog(
    *,
    route_name: str,
    route_point_count: int,
    default_csv_path: str,
    default_photo_dir: str,
    default_meter_type: str,
    settings_path: Path,
    parent: object,
    handlers: RouteDialogHandlers,
) -> object:
    from probe_station_gui.dialogs.route_measurement_dialog import (
        RouteMeasurementDialog,
    )

    dialog = RouteMeasurementDialog(
        route_name=route_name,
        route_point_count=route_point_count,
        default_csv_path=default_csv_path,
        default_photo_dir=default_photo_dir,
        default_meter_type=default_meter_type,
        settings_path=settings_path,
        parent=parent,
    )
    dialog.measure_requested.connect(handlers.measure_requested)
    dialog.start_session_requested.connect(handlers.start_session_requested)
    dialog.cancel_session_requested.connect(handlers.cancel_session_requested)
    dialog.next_requested.connect(handlers.next_requested)
    dialog.remeasure_requested.connect(handlers.remeasure_requested)
    dialog.measure_current_requested.connect(handlers.measure_current_requested)
    dialog.skip_requested.connect(handlers.skip_requested)
    dialog.save_shift_requested.connect(handlers.save_shift_requested)
    dialog.interrupt_requested.connect(handlers.interrupt_requested)
    dialog.pause_requested.connect(handlers.pause_requested)
    dialog.stop_requested.connect(handlers.stop_requested)
    dialog.jump_requested.connect(handlers.jump_requested)
    dialog.move_requested.connect(handlers.move_requested)
    dialog.current_point_changed.connect(handlers.current_point_changed)
    dialog.finished.connect(handlers.finished)
    return dialog


def current_route_measurement_configuration(
    runtime_configuration: object | None,
    fallback: object,
) -> object:
    return runtime_configuration or fallback


def route_measurement_setup_changed(previous: object | None, current: object) -> bool:
    if previous is None:
        return False
    return (
        getattr(previous, "operation_mode", None) != getattr(current, "operation_mode", None)
        or bool(getattr(previous, "previous_ok_only", False))
        != bool(getattr(current, "previous_ok_only", False))
        or str(getattr(previous, "previous_csv_path", "")).strip()
        != str(getattr(current, "previous_csv_path", "")).strip()
    )


def request_route_measurement_for_point(
    *,
    point_number: int,
    thread_active: bool,
    waiting: bool,
    open_dialog: Callable[..., None],
    current_dialog: Callable[[], object | None],
    submit_confirmation: Callable[[str], None],
    request_point_correction: Callable[..., None],
    start_measurement: Callable[[object], None],
    set_pending_point: Callable[[int], None],
    clear_pending_point: Callable[[], None],
) -> None:
    point_number = int(point_number)
    if thread_active:
        if waiting:
            clear_pending_point()
            submit_confirmation(f"jump:{point_number}")
            return
        set_pending_point(point_number)
        request_point_correction(pending_point_number=point_number)
        return
    open_dialog(start_context=False)
    dialog = current_dialog()
    if dialog is None:
        return
    dialog.set_current_point(point_number)
    start_measurement(dialog.current_configuration())


def route_measurement_point_request_handler(
    *,
    thread_active: Callable[[], bool],
    waiting: Callable[[], bool],
    open_dialog: Callable[..., None],
    current_dialog: Callable[[], object | None],
    submit_confirmation: Callable[[str], None],
    request_point_correction: Callable[..., None],
    start_measurement: Callable[[object], None],
    set_pending_point: Callable[[int], None],
    clear_pending_point: Callable[[], None],
) -> Callable[[int], None]:
    def handler(point_number: int) -> None:
        request_route_measurement_for_point(
            point_number=point_number,
            thread_active=thread_active(),
            waiting=waiting(),
            open_dialog=open_dialog,
            current_dialog=current_dialog,
            submit_confirmation=submit_confirmation,
            request_point_correction=request_point_correction,
            start_measurement=start_measurement,
            set_pending_point=set_pending_point,
            clear_pending_point=clear_pending_point,
        )

    return handler


def route_measurement_point_request_handler_for_owner(owner: object) -> Callable[[int], None]:
    return route_measurement_point_request_handler(
        thread_active=lambda: (
            getattr(owner, "_route_measurement_thread", None) is not None
            and owner._route_measurement_thread.is_alive()
        ),
        waiting=lambda: bool(getattr(owner, "_route_measurement_waiting", False)),
        open_dialog=getattr(owner, "_open_route_measurement_dialog"),
        current_dialog=lambda: getattr(owner, "_route_measurement_dialog", None),
        submit_confirmation=getattr(owner, "_submit_route_measurement_confirmation"),
        request_point_correction=getattr(owner, "_request_route_measurement_point_correction"),
        start_measurement=getattr(owner, "_start_route_measurement"),
        set_pending_point=lambda point: setattr(owner, "_pending_route_measure_point", point),
        clear_pending_point=lambda: setattr(owner, "_pending_route_measure_point", None),
    )


def restart_waiting_route_measurement(
    *,
    configuration: object,
    route_offset_xy: tuple[float, float],
    old_runner: object | None,
    old_thread: object | None,
    clear_waiting_state: Callable[[], None],
    start_measurement: Callable[[object], None],
    current_runner: Callable[[], object | None],
    current_thread: Callable[[], object | None],
    show_status: Callable[[str, int], None],
) -> bool:
    if old_runner is not None:
        old_runner.stop()
    if old_thread is not None and old_thread.is_alive():
        old_thread.join(timeout=2.0)
        if old_thread.is_alive():
            show_status("Waiting route measurement did not stop.", 8000)
            return False
    clear_waiting_state()
    start_measurement(configuration, wait_before_first_point=True)
    new_runner = current_runner()
    if new_runner is None or not hasattr(new_runner, "set_route_offset_xy"):
        return False
    new_runner.set_route_offset_xy(route_offset_xy)
    if new_runner.wait_until_waiting(timeout_s=10.0):
        return True
    new_runner.stop()
    thread = current_thread()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    show_status("Route measurement did not reach waiting state.", 8000)
    return False


__all__ = [
    "RouteDialogHandlers",
    "RouteDialogDefaults",
    "RouteDialogOpenState",
    "RouteDialogRestorePlan",
    "RouteMeasurementSessionCancelPlan",
    "RouteMeasurementSessionStartPlan",
    "current_route_measurement_configuration",
    "open_or_update_route_measurement_dialog",
    "request_route_measurement_for_point",
    "route_dialog_handlers",
    "route_measurement_point_request_handler",
    "route_measurement_point_request_handler_for_owner",
    "restart_waiting_route_measurement",
    "route_dialog_defaults",
    "route_dialog_open_state",
    "route_dialog_restore_plan",
    "route_measurement_setup_changed",
    "route_measurement_session_cancel_plan",
    "route_measurement_session_start_plan",
]
