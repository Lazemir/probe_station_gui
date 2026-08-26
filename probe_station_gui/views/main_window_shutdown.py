"""Main-window shutdown and auxiliary-window close orchestration."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from probe_station_gui.stage.types import StageMotionResetReason
from probe_station_gui.views import main_window_connection_flow as connection_flow


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
    _api_bridge: Any
    _api_stage_command_runtime: Any
    _stage_motion: Any
    _design_position_timer: Any
    _stage_motion_blink_timer: Any
    _linear_feedrate_save_timer: Any
    _route_run_execution: Any
    _microscope_scan_thread: Any
    _microscope_scan_stop_requested: Any
    _manual_alignment_capture_context: Any
    _manual_alignment_pick_slot: Any
    _optical_calibration_runtime: Any
    _live_camera_frame_processor: Any
    _exposure_policy_adapter: Any
    _exposure_policy_controller: Any
    _route_measurement_dialog: Any
    _telegram_runtime: Any

    def _save_pending_linear_feedrate_default(self) -> None: ...
    def _stop_design_markup_store(self) -> None: ...
    def _stop_coordinate_frame_store(self) -> None: ...
    def _stop_software_coordinate_selection_store(self) -> None: ...
    def _route_runtime_presenter(self) -> Any: ...
    def _show_status(self, message: str, timeout: int) -> None: ...


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
    _quiesce_api_intake(owner)
    try:
        _stop_api_stage_command_workers(owner)
        _drain_api_bridge(owner)
        _stop_route_worker(owner)
        _stop_microscope_scan(owner)
        _stop_manual_alignment_capture(owner)
        _stop_optical_calibration(owner)
        _close_serial_and_panels(owner)
        _finalize_api_shutdown(owner)
    except Exception as exc:
        logger.exception("Camera shutdown blocked before worker teardown")
        owner._show_status(f"Camera shutdown blocked: {exc}", 10000)
        _resume_api_intake_after_abort(owner)
        event.ignore()
        return
    _stop_services_and_timers(owner)
    _retire_focus_structure_worker(owner)
    _shutdown_controllers(owner)
    close_auxiliary_windows(owner, force_route_dialog=True)
    if owner.serial_connection_panel:
        owner.serial_connection_panel.shutdown()
    event.accept()


def _persist_shutdown_state(
    owner: MainWindowShutdownOwner,
    *,
    serial_was_connected: bool,
    lcr_was_connected: bool,
) -> None:
    connection_flow.persist_serial_connection_state(owner, serial_was_connected)
    connection_flow.persist_lcr_connection_state(owner, lcr_was_connected)
    if serial_was_connected:
        connection_flow.persist_controller_state(owner)


def _stop_services_and_timers(owner: MainWindowShutdownOwner) -> None:
    if owner._api_server is not None:
        owner._api_server.stop()
    owner._telegram_runtime.stop()
    owner._design_position_timer.stop()
    owner._stage_motion.reset(StageMotionResetReason.APPLICATION_CLOSED)
    owner._stage_motion_blink_timer.stop()
    if owner._linear_feedrate_save_timer.isActive():
        owner._linear_feedrate_save_timer.stop()
    owner._save_pending_linear_feedrate_default()
    owner._stop_design_markup_store()
    stop_coordinate_frames = getattr(owner, "_stop_coordinate_frame_store", None)
    if callable(stop_coordinate_frames):
        stop_coordinate_frames()
    stop_coordinate_selection = getattr(
        owner,
        "_stop_software_coordinate_selection_store",
        None,
    )
    if callable(stop_coordinate_selection):
        stop_coordinate_selection()


def _quiesce_api_intake(owner: MainWindowShutdownOwner) -> None:
    api_bridge = getattr(owner, "_api_bridge", None)
    if api_bridge is not None:
        api_bridge.stop_accepting()


def _resume_api_intake_after_abort(owner: MainWindowShutdownOwner) -> None:
    api_bridge = getattr(owner, "_api_bridge", None)
    resume = getattr(api_bridge, "resume_accepting", None)
    if callable(resume):
        resume()


def _retire_focus_structure_worker(owner: MainWindowShutdownOwner) -> None:
    worker = getattr(owner, "_focus_structure_bounds_worker", None)
    if worker is None:
        return
    timeout = float(getattr(owner, "FOCUS_STRUCTURE_WORKER_SHUTDOWN_TIMEOUT_S", 0.25))
    worker.stop(timeout_s=max(0.0, timeout))


def _stop_route_worker(owner: MainWindowShutdownOwner) -> None:
    execution = owner._route_run_execution.snapshot()
    if execution.runner is not None:
        execution.runner.stop()
    if execution.thread_alive:
        execution.thread.join(timeout=2.0)


def _stop_microscope_scan(owner: MainWindowShutdownOwner) -> None:
    if owner._microscope_scan_thread is not None:
        owner._microscope_scan_stop_requested.set()
    if (
        owner._microscope_scan_thread is not None
        and owner._microscope_scan_thread.is_alive()
    ):
        owner._microscope_scan_thread.join(timeout=2.0)


def _stop_api_stage_command_workers(owner: MainWindowShutdownOwner) -> None:
    if not owner._api_stage_command_runtime.wait_until_idle(timeout_s=0.0):
        raise RuntimeError("API stage command is still stopping.")


def _stop_manual_alignment_capture(owner: MainWindowShutdownOwner) -> None:
    context = getattr(owner, "_manual_alignment_capture_context", None)
    if context is None:
        return
    context.cancelled.set()
    owner.stage_controller.cancel_clicked_point_resolution(
        context.request_id, "Alignment point capture cancelled for shutdown."
    )
    if not owner.stage_controller.wait_for_active_task(timeout_s=2.0):
        raise RuntimeError("Alignment point capture is still stopping.")
    owner._manual_alignment_capture_context = None
    owner._manual_alignment_pick_slot = None


def _drain_api_bridge(owner: MainWindowShutdownOwner) -> None:
    bridge = getattr(owner, "_api_bridge", None)
    if bridge is None:
        return
    if not bridge.wait_for_inflight(timeout_s=2.0):
        raise RuntimeError("API request completion is still pending.")


def _finalize_api_shutdown(owner: MainWindowShutdownOwner) -> None:
    bridge = getattr(owner, "_api_bridge", None)
    if bridge is None:
        return
    if not bridge.close():
        raise RuntimeError("API request completion could not be published.")


def _drain_and_close_api_bridge(owner: MainWindowShutdownOwner) -> None:
    """Compatibility wrapper for focused bridge shutdown tests."""

    _drain_api_bridge(owner)
    _finalize_api_shutdown(owner)


def _stop_optical_calibration(owner: MainWindowShutdownOwner) -> None:
    runtime = getattr(owner, "_optical_calibration_runtime", None)
    if runtime is None:
        return
    if not runtime.shutdown(2.0):
        raise RuntimeError("Optical calibration is still stopping.")


def _close_serial_and_panels(owner: MainWindowShutdownOwner) -> None:
    stop_jog_before_serial_close(owner, "application shutdown")
    owner._exposure_policy_adapter.shutdown(timeout_s=2.0)
    owner._exposure_policy_controller.shutdown(timeout_s=2.0)
    owner._live_camera_frame_processor.shutdown(timeout_s=2.0)
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
    serial_terminal_panel = getattr(owner, "serial_terminal_panel", None)
    if serial_terminal_panel is not None:
        serial_terminal_panel.close()
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
