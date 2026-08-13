from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from probe_station_gui.camera import microscope_scan
from probe_station_gui.camera.microscope_scan_runtime import (
    MicroscopeScanRunRequest,
    MicroscopeScanRuntime,
)
from probe_station_gui.camera.microscope_scan_runtime_adapters import (
    MicroscopeDesignScanRequest,
    MicroscopeScanArtifactAdapter,
    MicroscopeScanCameraAdapter,
    MicroscopeScanEventAdapter,
    MicroscopeScanSessionAdapter,
    MicroscopeScanStageAdapter,
    build_microscope_scan_plan,
)
from probe_station_gui.route.measurement import (
    route_measurement_sample_from_raw,
    summarize_route_contact_quality,
)
from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle
from probe_station_gui.stage import sample_handling
from probe_station_gui.views import main_window_connection_flow as connection_flow

logger = logging.getLogger("main")

if TYPE_CHECKING:
    from probe_station_gui.dialogs.microscope_scan_dialog import (
        MicroscopeScanConfiguration,
    )


class _MainScanSampleMeterMixin:
    def _clear_microscope_scan_dialog(self) -> None:
        self.microscope_scan_dialog = None

    def _request_stop_microscope_scan(self) -> None:
        if not self._microscope_scan_running():
            self._show_status("No microscope scan is running.", 3000)
            return
        self._microscope_scan_stop_requested.set()
        message = "Microscope scan stop requested."
        self._show_status(message, 5000)
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.set_status(message)

    def _show_microscope_scan_start_rejection(self, decision: object) -> bool:
        if bool(getattr(decision, "accepted", False)):
            return False
        status = getattr(decision, "status", None)
        if status is not None:
            self._show_status(status.message, status.timeout_ms)
        return True

    def _start_microscope_scan(
        self,
        configuration: MicroscopeScanConfiguration,
    ) -> None:
        preflight = microscope_scan.start_environment_decision(
            scan_running=self._microscope_scan_running(),
            serial_connected=(
                self.serial_connection is not None and self.serial_connection.is_open
            ),
        )
        if self._show_microscope_scan_start_rejection(preflight):
            return
        document = self._design_session.document
        frame_usability = self._coordinate_system_coordinator.current_design_lease()
        design_preflight = microscope_scan.start_design_decision(
            document=document,
            registration_valid=frame_usability.usable,
        )
        if self._show_microscope_scan_start_rejection(design_preflight):
            if document is not None and not frame_usability.usable:
                self._show_status(
                    str(
                        frame_usability.rejection_reason
                        or "Design coordinate frame is unavailable."
                    ),
                    6000,
                )
            return
        scale = self._active_microscope_scale()
        scale_preflight = microscope_scan.start_scale_decision(scale=scale)
        if self._show_microscope_scan_start_rejection(scale_preflight):
            return
        try:
            launch_snapshot = self._capture_microscope_scan_launch_snapshot(
                scale=scale,
                document=document,
                frame_usability_snapshot=frame_usability,
            )
            planning_request = MicroscopeDesignScanRequest(
                bounds=tuple(float(value) for value in document.bounds),
                overlap_fraction=float(configuration.overlap_fraction),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            self._show_status("Design registration is invalid.", 6000)
            return
        if not self._coordinate_system_coordinator.design_lease_is_current(
            frame_usability
        ):
            self._show_status(
                "Design coordinate frame changed before scan start.",
                6000,
            )
            return
        self._microscope_scan_stop_requested.clear()
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.set_running(True)
            self.microscope_scan_dialog.set_status("Microscope scan starting.")
        thread = threading.Thread(
            target=self._run_microscope_scan,
            args=(configuration, planning_request, launch_snapshot),
            name="MicroscopeDesignScan",
            daemon=True,
        )
        self._microscope_scan_thread = thread
        try:
            thread.start()
        except Exception as exc:
            if self._microscope_scan_thread is thread:
                self._microscope_scan_thread = None
            message = f"Microscope scan could not start: {exc}"
            if self.microscope_scan_dialog is not None:
                self.microscope_scan_dialog.set_running(False)
                self.microscope_scan_dialog.set_status(message)
            self._show_status(message, 8000)
        self._update_stage_coordinate_apply_state()

    def _run_microscope_scan(
        self,
        configuration: MicroscopeScanConfiguration,
        planning_request: object,
        launch_snapshot: _MicroscopeScanLaunchSnapshot,  # noqa: F821
    ) -> None:
        flat_field_options = getattr(
            configuration,
            "flat_field_options",
            microscope_scan.FlatFieldScanOptions(enabled=False),
        )
        camera_lock_settings = getattr(
            configuration,
            "camera_lock_settings",
            microscope_scan.CameraLockSettings(enabled=False),
        )
        request = MicroscopeScanRunRequest(
            output_dir=microscope_scan.output_dir_from_configuration(configuration),
            scale=launch_snapshot.scale,
            plan_factory=lambda frame_size, start_xy: build_microscope_scan_plan(
                planning_request,
                frame_size,
                launch=launch_snapshot,
                center_stage_xy=start_xy,
            ),
            flat_field_options=flat_field_options,
            camera_lock_settings=camera_lock_settings,
            settle_s=float(configuration.settle_s),
            scan_pattern=str(getattr(configuration, "scan_pattern", "grid") or "grid"),
            refine_scale_from_overlaps=bool(
                getattr(configuration, "refine_scale_from_overlaps", True)
            ),
        )
        runtime = MicroscopeScanRuntime(
            stage=MicroscopeScanStageAdapter(
                reserve_task=self.stage_controller.reserve_external_task,
                read_reserved_position=self.stage_controller.run_external_current_stage_position,
                raise_action=self.stage_controller.run_external_needles_action,
                needle_feedrate=self._current_needle_feedrate,
                move_xy=self.stage_controller.run_external_move_to_xy,
                latest_position=self.stage_controller.latest_stage_position,
            ),
            camera=MicroscopeScanCameraAdapter(
                grabber=self.grabber,
                stop_event=self._microscope_scan_stop_requested,
                latest_frame_counter=self._latest_camera_counter,
                wait_for_frame=self._wait_for_camera_frame,
                latest_raw_frame_counter=self._latest_raw_camera_counter,
                wait_for_raw_frame=self._wait_for_raw_camera_frame,
                correct_lens=self._correct_camera_frame_for_active_objective,
                settings_timeout_s=self.MICROSCOPE_SCAN_CAMERA_SETTINGS_TIMEOUT_S,
            ),
            artifacts=MicroscopeScanArtifactAdapter(
                launch=launch_snapshot,
                stage_position_for_metadata=self._stage_position_for_image_metadata,
            ),
            event_sink=MicroscopeScanEventAdapter(
                status_callback=self.microscope_scan_status.emit,
                finished_callback=self.microscope_scan_finished.emit,
            ),
            session=MicroscopeScanSessionAdapter(self._optical_session_manager),
        )
        runtime.run(request)

    def _on_microscope_scan_status(self, message: str) -> None:
        self._show_status(message)
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.set_status(message)

    def _on_microscope_scan_finished(self, success: bool, message: str) -> None:
        thread = self._microscope_scan_thread
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._microscope_scan_thread = None
        self._microscope_scan_stop_requested.clear()
        self._update_stage_coordinate_apply_state()
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.set_running(False)
            self.microscope_scan_dialog.set_status(message)
        self._show_status(message, 10000 if success else 8000)

    def _move_to_design_window_point(self, x_value: float, y_value: float) -> None:
        design_xy = (float(x_value), float(y_value))
        if not self._move_to_design_coordinate(design_xy, source_label="design window"):
            return
        self._show_status(
            f"Moving to design point X={design_xy[0]:.3f}, Y={design_xy[1]:.3f}.",
            3000,
        )

    def _on_needle_height_changed(self, lowering_mm: float) -> None:
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_current_needle_lowering(lowering_mm)

    def _on_lcr_connection_changed(
        self, connected: bool, backend_name: str, description: str
    ) -> None:
        if connected:
            connection_flow.persist_lcr_connection_state(
                self,
                True,
                description=description,
            )
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_connection_state(
                connected, backend_name, description
            )
        if self.resistance_panel is not None:
            self.resistance_panel.set_standby_enabled(
                self.lcr_controller.live_polling_enabled()
            )
            self.resistance_panel.set_connection_state(
                connected, backend_name, description
            )

    def _on_lcr_reading_updated(self, resistance_ohm: float, is_short: bool) -> None:
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_reading(resistance_ohm, is_short)

    def _on_lcr_reading_started(self, sample_count: int) -> None:
        if self.resistance_panel is not None:
            self.resistance_panel.set_reading_pending(int(sample_count))

    def _on_lcr_reading_summary_updated(
        self, resistance_ohm: float, is_short: bool, sample_count: int
    ) -> None:
        if self.resistance_panel is not None:
            self.resistance_panel.set_reading_summary(
                resistance_ohm, is_short, sample_count
            )

    def _on_resistance_standby_enabled_changed(self, enabled: bool) -> None:
        self.lcr_controller.set_live_polling_enabled(bool(enabled))
        if self.resistance_panel is not None:
            self.resistance_panel.set_standby_enabled(
                self.lcr_controller.live_polling_enabled()
            )

    def _resume_resistance_standby_polling(self) -> None:
        lcr_controller = getattr(self, "lcr_controller", None)
        if lcr_controller is not None and lcr_controller.live_polling_enabled():
            lcr_controller.set_live_polling_enabled(True)

    def _cancel_contact_seek(self) -> None:
        self._contact_seek_stop_requested.set()
        self.stage_controller.cancel_active_motion("Contact seek cancel requested.")
        self._show_status("Contact seek cancel requested.")

    def _contact_seek_measure_quality(self, count: int):
        raw_batch = self.lcr_controller.read_route_measurement_batch_now(int(count))
        samples = tuple(
            route_measurement_sample_from_raw(raw, index)
            for index, raw in enumerate(raw_batch, start=1)
        )
        return summarize_route_contact_quality(samples)

    def _display_a_for_needle_lowering(self, lowering_mm: float | None) -> float | None:
        if lowering_mm is None:
            return None
        target_raw_a = self.stage_controller.axis_a_configured_coordinate_for_lowering(
            lowering_mm
        )
        return self.stage_controller.calibrated_axis_display_value("A", target_raw_a)

    def _set_design_snap_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._design_snap_enabled = enabled
        if self.design_layout_window is not None:
            self.design_layout_window.set_snap_enabled(enabled)

    def _on_design_snap_enabled_changed(self, enabled: bool) -> None:
        self._set_design_snap_enabled(enabled)

    def _sample_handling_active(self) -> bool:
        thread = getattr(self, "_sample_handling_thread", None)
        return thread is not None and thread.is_alive()

    def _latest_stage_z(self) -> float | None:
        return sample_handling.latest_stage_z(
            self.stage_controller.latest_stage_position()
        )

    def _active_sample_objective_name(self) -> str:
        try:
            objectives = self.settings_manager.objectives_configuration()
            raw_name = getattr(objectives, "active_name", "")
        except Exception:
            logger.debug(
                "Unable to read active objective for sample focus.", exc_info=True
            )
            raw_name = ""
        return sample_handling.active_sample_objective_name(raw_name)

    def _remember_sample_focus_from_latest(self) -> float | None:
        return sample_handling.remember_sample_focus(
            self._sample_focus_cache(),
            raw_objective_name=self._active_sample_objective_name(),
            latest_position=self.stage_controller.latest_stage_position(),
        )

    def _sample_load_focus_z(self) -> float | None:
        return sample_handling.sample_load_focus_z(
            self._sample_focus_cache(),
            raw_objective_name=self._active_sample_objective_name(),
            latest_position=self.stage_controller.latest_stage_position(),
        )

    def _sample_focus_cache(self) -> dict[str, float]:
        focus_by_objective = getattr(self, "_last_sample_focus_z_by_objective", None)
        if focus_by_objective is None:
            focus_by_objective = {}
            self._last_sample_focus_z_by_objective = focus_by_objective
        return focus_by_objective

    def _sample_workflow_can_start(self, action: str) -> bool:
        decision = sample_handling.sample_start_decision(
            action,
            stage_ready=self._stage_serial_ready(),
            sample_active=self._sample_handling_active(),
            cancelable_operation=stage_move_lifecycle.has_cancelable_operation(self),
        )
        if not decision.accepted:
            self._show_status(decision.status_message, 4000)
            return False
        return True

    def _design_registration_is_active(self) -> bool:
        return bool(
            self._coordinate_system_coordinator.snapshot().registration.registration_valid
        )

    def _run_sample_unload(
        self,
        xy_feedrate: float,
        needle_feedrate: float,
    ) -> None:
        sample_handling.run_sample_unload(
            self.stage_controller,
            xy_feedrate=xy_feedrate,
            needle_feedrate=needle_feedrate,
            emit_status=self.sample_handling_status.emit,
            emit_finished=self.sample_handling_finished.emit,
            unload_x_mm=self.SAMPLE_UNLOAD_X_MM,
            unload_y_mm=self.SAMPLE_UNLOAD_Y_MM,
        )

    def _run_sample_load(
        self,
        focus_z_mm: float | None,
        xy_feedrate: float,
        focus_feedrate: float,
        needle_feedrate: float,
    ) -> None:
        sample_handling.run_sample_load(
            self.stage_controller,
            focus_z_mm=focus_z_mm,
            xy_feedrate=xy_feedrate,
            focus_feedrate=focus_feedrate,
            needle_feedrate=needle_feedrate,
            emit_status=self.sample_handling_status.emit,
            emit_finished=self.sample_handling_finished.emit,
            load_x_mm=self.SAMPLE_LOAD_X_MM,
            load_y_mm=self.SAMPLE_LOAD_Y_MM,
        )

    def _on_oscillation_state_changed(self, running: bool, axis: str) -> None:
        if self.oscillation_panel:
            self.oscillation_panel.set_running(running, axis)
        self._update_stage_coordinate_apply_state()

    def _save_oscillation_configuration(
        self,
        mode: str,
        amplitude_mm: float,
        feedrate_mm_min: float,
        turns_per_sweep: float,
    ) -> None:
        settings = self.settings_manager.settings.clone()
        settings.oscillation.mode = str(mode).strip().upper() or "X"
        settings.oscillation.amplitude_mm = float(amplitude_mm)
        settings.oscillation.feedrate_mm_min = float(feedrate_mm_min)
        settings.oscillation.turns_per_sweep = float(turns_per_sweep)
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )
