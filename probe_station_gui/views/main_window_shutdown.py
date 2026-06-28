"""Main-window shutdown and auxiliary-window close orchestration."""

from __future__ import annotations

import logging
from typing import Any, Protocol


logger = logging.getLogger(__name__)


class MainWindowShutdownOwner(Protocol):
    serial_connection: Any
    lcr_controller: Any
    joystick_panel: Any
    serial_terminal_panel: Any
    serial_connection_panel: Any
    stage_controller: Any
    grabber: Any
    thread: Any
    design_layout_window: Any
    contact_calibration_window: Any
    surface_map_window: Any
    microscope_scan_dialog: Any
    _api_server: Any
    _design_position_timer: Any
    _manual_jog_timer: Any
    _stage_motion_blink_timer: Any
    _linear_feedrate_save_timer: Any
    _route_measurement_runner: Any
    _route_measurement_thread: Any
    _microscope_scan_thread: Any
    _microscope_scan_stop_requested: Any
    _route_measurement_dialog: Any

    def _persist_serial_connection_state(self, connected: bool) -> None: ...
    def _persist_lcr_connection_state(self, connected: bool) -> None: ...
    def _persist_controller_state(self) -> None: ...
    def _stop_telegram_bot_service(self) -> None: ...
    def _save_pending_linear_feedrate_default(self) -> None: ...
    def _stop_jog_before_serial_close(self, reason: str) -> None: ...
    def _close_auxiliary_windows(self, *, force_route_dialog: bool = False) -> None: ...
    def _route_runtime_presenter(self) -> Any: ...


def close_event(owner: MainWindowShutdownOwner, event: Any) -> None:
    serial_was_connected = bool(
        owner.serial_connection is not None and owner.serial_connection.is_open
    )
    lcr_was_connected = bool(owner.lcr_controller.is_connected())
    _persist_shutdown_state(
        owner,
        serial_was_connected=serial_was_connected,
        lcr_was_connected=lcr_was_connected,
    )
    _stop_services_and_timers(owner)
    _stop_route_worker(owner)
    _stop_microscope_scan(owner)
    _close_serial_and_panels(owner)
    _shutdown_controllers(owner)
    owner._close_auxiliary_windows(force_route_dialog=True)
    if owner.serial_connection_panel:
        owner.serial_connection_panel.shutdown()
    event.accept()


def _persist_shutdown_state(
    owner: MainWindowShutdownOwner,
    *,
    serial_was_connected: bool,
    lcr_was_connected: bool,
) -> None:
    owner._persist_serial_connection_state(serial_was_connected)
    owner._persist_lcr_connection_state(lcr_was_connected)
    if serial_was_connected:
        owner._persist_controller_state()


def _stop_services_and_timers(owner: MainWindowShutdownOwner) -> None:
    if owner._api_server is not None:
        owner._api_server.stop()
    owner._stop_telegram_bot_service()
    owner._design_position_timer.stop()
    owner._manual_jog_timer.stop()
    owner._stage_motion_blink_timer.stop()
    if owner._linear_feedrate_save_timer.isActive():
        owner._linear_feedrate_save_timer.stop()
    owner._save_pending_linear_feedrate_default()


def _stop_route_worker(owner: MainWindowShutdownOwner) -> None:
    if owner._route_measurement_runner is not None:
        owner._route_measurement_runner.stop()
    if (
        owner._route_measurement_thread is not None
        and owner._route_measurement_thread.is_alive()
    ):
        owner._route_measurement_thread.join(timeout=2.0)


def _stop_microscope_scan(owner: MainWindowShutdownOwner) -> None:
    if owner._microscope_scan_thread is not None:
        owner._microscope_scan_stop_requested.set()
    if (
        owner._microscope_scan_thread is not None
        and owner._microscope_scan_thread.is_alive()
    ):
        owner._microscope_scan_thread.join(timeout=2.0)


def _close_serial_and_panels(owner: MainWindowShutdownOwner) -> None:
    owner._stop_jog_before_serial_close("application shutdown")
    owner.grabber.stop()
    owner.thread.quit()
    owner.thread.wait()
    if owner.serial_connection and owner.serial_connection.is_open:
        owner.serial_connection.close()
    if owner.joystick_panel:
        owner.joystick_panel.set_serial(None)
    if owner.serial_terminal_panel:
        owner.serial_terminal_panel.set_serial(None)


def _shutdown_controllers(owner: MainWindowShutdownOwner) -> None:
    owner.stage_controller.request_stop_oscillation()
    owner.stage_controller.shutdown()
    owner.lcr_controller.shutdown()


def close_auxiliary_windows(
    owner: MainWindowShutdownOwner,
    *,
    force_route_dialog: bool = False,
) -> None:
    if owner._route_measurement_dialog is not None:
        if force_route_dialog:
            owner._route_runtime_presenter().set_running(False)
        owner._route_measurement_dialog.close()
    if owner.design_layout_window is not None:
        owner.design_layout_window.close()
    if owner.contact_calibration_window is not None:
        owner.contact_calibration_window.close()
    if owner.surface_map_window is not None:
        owner.surface_map_window.close()
    if owner.microscope_scan_dialog is not None:
        owner.microscope_scan_dialog.close()
    serial_connection_dialog = getattr(owner, "serial_connection_dialog", None)
    if serial_connection_dialog is not None:
        serial_connection_dialog.close()


def stop_jog_before_serial_close(
    owner: MainWindowShutdownOwner,
    reason: str,
) -> None:
    if owner.serial_connection is None or not owner.serial_connection.is_open:
        return
    logger.warning("Stopping active jog before %s.", reason)
    if owner.joystick_panel is not None:
        owner.joystick_panel.stop_jog()
    try:
        owner.stage_controller.force_jog_stop(timeout=0.8)
    except Exception:
        logger.exception("Failed to force jog stop before %s.", reason)
