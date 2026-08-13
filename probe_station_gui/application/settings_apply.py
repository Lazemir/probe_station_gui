from __future__ import annotations

import logging
from dataclasses import replace

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from probe_station_gui.camera.optical_calibration_adapters import (
    optical_calibration_blocks_mutation,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    OpticalCalibrationOutcome,
)
from probe_station_gui.coordinates import PhysicalMachinePose
from probe_station_gui.coordinates.coordinator_model import (
    RegistrationSourceMarkRequest,
    RegistrationSourceMarksRequest,
)
from probe_station_gui.coordinates.design_calibration import (
    design_calibration_fingerprints,
)
from probe_station_gui.design import objective_alignment as alignment
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.settings.dialog_transaction import SettingsDialogContext
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import normalize_objective_name
from probe_station_gui.views import main_window_connection_flow as connection_flow
from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow
from probe_station_gui.views import (
    main_window_needle_calibration as needle_calibration_ui,
)
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)

logger = logging.getLogger("main")


class _MainSettingsApplyMixin:
    def _on_design_layout_point_selected(
        self, slot: int, x_value: float, y_value: float
    ) -> None:
        document = self._design_session.document
        if document is None:
            return
        if slot not in (0, 1):
            return
        snapped_point = (float(x_value), float(y_value))
        if not self._start_fresh_design_frame_for_source_replacement():
            return
        self._manual_alignment_pick_slot = None
        self._manual_alignment_points = [None, None]
        self._pending_alignment_preparation = None
        transition = self._coordinate_system_coordinator.set_registration_source_mark(
            RegistrationSourceMarkRequest(snapped_point, slot=slot)
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        self._last_selected_design_point = snapped_point
        self._set_design_snap_enabled(True)
        self._refresh_design_panel()
        self._set_alignment_panel_expanded()
        slot_label = "1" if slot == 0 else "2"
        self._show_status(
            f"Design mark {slot_label} snapped to X={snapped_point[0]:.3f}, Y={snapped_point[1]:.3f}.",
            4000,
        )

    def _start_fresh_design_frame_for_source_replacement(self) -> bool:
        transition = coordinate_flow.activate_current_design(
            self,
            create_new_if_registered=True,
        )
        return transition is not None and transition.accepted

    def _on_alignment_draft_accepted(self, points: object) -> None:
        if not isinstance(points, (list, tuple)):
            return
        try:
            normalized = tuple(
                (float(point[0]), float(point[1]))
                for point in points
                if isinstance(point, (list, tuple)) and len(point) == 2
            )
        except (TypeError, ValueError):
            return
        if len(normalized) < 2 or len(set(normalized)) < 2:
            self._show_status(
                "Align requires at least two distinct design points.", 5000
            )
            return
        coordinate_snapshot = self._coordinate_system_coordinator.snapshot()
        frame_id = coordinate_snapshot.registration.active_frame_id
        record = next(
            (item for item in coordinate_snapshot.records if item.frame_id == frame_id),
            None,
        )
        if (
            not coordinate_snapshot.frames_loaded
            or record is None
            or self._design_session.document is None
        ):
            self._show_status(
                "A durable Design coordinate frame is required before alignment.",
                6000,
            )
            return
        if not self._start_fresh_design_frame_for_source_replacement():
            return
        coordinate_snapshot = self._coordinate_system_coordinator.snapshot()
        frame_id = coordinate_snapshot.registration.active_frame_id
        record = next(
            (item for item in coordinate_snapshot.records if item.frame_id == frame_id),
            None,
        )
        if record is None:
            self._show_status("Design coordinate frame is unavailable.", 6000)
            return
        transition = (
            self._coordinate_system_coordinator.replace_registration_source_marks(
                RegistrationSourceMarksRequest(normalized)
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        self._alignment_design_draft = normalized
        self._alignment_stage_draft = [None] * len(normalized)
        self._alignment_draft_fit_residuals = None
        self._pending_alignment_preparation = None
        self._manual_alignment_pick_slot = None
        design_layout_window = getattr(self, "design_layout_window", None)
        if design_layout_window is not None:
            design_layout_window.set_alignment_capture_points(normalized)
        self._set_alignment_panel_expanded()
        self._refresh_manual_alignment_ui()
        self._update_stage_coordinate_apply_state()
        self._show_status(
            f"Align: {len(normalized)} design points ready. Capture S1 next.",
            5000,
        )

    def _on_alignment_draft_discarded(self) -> None:
        self._show_status("Align draft discarded.", 2500)

    def _apply_settings(self, *, apply_objective_runtime: bool = True) -> None:
        self._clear_exact_step_targets()
        connection_flow.apply_axis_feedrate_limits(
            self,
            self.stage_controller.axis_max_feedrates(),
        )
        if self.joystick_panel:
            bindings = self.settings_manager.control_bindings()
            self.joystick_panel.apply_control_bindings(bindings)
            logger.debug("Joystick bindings reapplied from settings")
            connection_flow.apply_joystick_feedrate_preferences(self)
        jog = self.settings_manager.jog_configuration()
        self.stage_controller.set_motion_safety_disabled(jog.motion_safety_disabled)
        needle_settings = self.settings_manager.needle_calibration_configuration()
        oscillation_settings = self.settings_manager.oscillation_configuration()
        self.stage_controller.apply_axis_calibrations(
            self.settings_manager.axis_calibrations_configuration()
        )
        self.stage_controller.apply_precision_approach_configuration(
            self.settings_manager.precision_approach_configuration()
        )
        needle_calibration_ui.apply_needle_calibration_runtime(self, needle_settings)
        coordinate_settings = self.settings_manager.coordinate_system_configuration()
        self.stage_controller.apply_coordinate_system_configuration(
            position_mode=coordinate_settings.position_mode,
            startup_mode=coordinate_settings.startup_mode,
            preferred_system=coordinate_settings.preferred_system,
        )
        latest_snapshot = getattr(
            self.stage_controller,
            "latest_machine_coordinate_snapshot",
            None,
        )
        machine_snapshot = latest_snapshot() if callable(latest_snapshot) else None
        self._latest_physical_machine_pose = (
            machine_snapshot.physical_machine_pose
            if machine_snapshot is not None
            else PhysicalMachinePose({})
        )
        if apply_objective_runtime:
            self._apply_objective_settings()
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_design_dialog_directory(
                self.settings_manager.design_last_directory()
            )
        if self.design_layout_window is not None:
            self.design_layout_window.set_snap_enabled(self._design_snap_enabled)
        self.lcr_controller.apply_configuration(
            meter_type=needle_settings.meter_type,
            resource_name=needle_settings.visa_resource,
            keithley_source_resource=needle_settings.keithley_source_resource,
            keithley_voltmeter_resource=needle_settings.keithley_voltmeter_resource,
            measurement_function=needle_settings.measurement_function,
            range_mode=needle_settings.range_mode,
            auto_range_enabled=needle_settings.auto_range_enabled,
            impedance_range=needle_settings.impedance_range,
            dcr_range=needle_settings.dcr_range,
            frequency_hz=needle_settings.frequency_hz,
            level_mode=needle_settings.level_mode,
            voltage_level_v=needle_settings.voltage_level_v,
            current_level_a=needle_settings.current_level_a,
            source_resistance_ohm=needle_settings.source_resistance_ohm,
            aperture_rate=needle_settings.aperture_rate,
            aperture_averages=needle_settings.aperture_averages,
            trigger_source=needle_settings.trigger_source,
            trigger_delay_s=needle_settings.trigger_delay_s,
            bias_enabled=needle_settings.bias_enabled,
            bias_level_v=needle_settings.bias_level_v,
            monitor1=needle_settings.monitor1,
            monitor2=needle_settings.monitor2,
            alc_enabled=needle_settings.alc_enabled,
            short_threshold_ohm=needle_settings.short_threshold_ohm,
            poll_interval_ms=needle_settings.poll_interval_ms,
        )
        self.lcr_controller.request_reconfigure()
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_resource(
                self.lcr_controller.connection_label()
            )
        if self.oscillation_panel:
            self.oscillation_panel.apply_configuration(
                mode=oscillation_settings.mode,
                amplitude_mm=oscillation_settings.amplitude_mm,
                feedrate_mm_min=oscillation_settings.feedrate_mm_min,
                turns_per_sweep=oscillation_settings.turns_per_sweep,
            )
        if self.serial_connection and self.serial_connection.is_open:
            self.stage_controller.request_startup_sync(auto_home_a=False)
            self._schedule_cancel_state_refresh()
        if self._api_bridge is not None:
            self._configure_api_server_from_settings(start_if_enabled=True)
        self._telegram_runtime.configure(self.settings_manager.telegram_configuration())
        self._update_coordinate_display(cursor_xy=None)

    def _apply_settings_from_dialog(self, new_settings: object) -> None:
        if not isinstance(new_settings, Settings):
            return
        stage_controller = getattr(self, "stage_controller", None)
        stage_busy = bool(stage_controller is not None and stage_controller.is_busy())
        latest_snapshot = (
            getattr(
                stage_controller,
                "latest_machine_coordinate_snapshot",
                None,
            )
            if not stage_busy
            else None
        )
        context = SettingsDialogContext(
            stage_busy=stage_busy,
            objective_mutation_busy=(
                self._objective_mutation_busy() if not stage_busy else False
            ),
            machine_snapshot=(latest_snapshot() if callable(latest_snapshot) else None),
        )
        outcome = self._settings_dialog_transaction.apply(
            new_settings,
            context,
        )
        if not outcome.accepted:
            return
        if outcome.refresh_coordinate_frame_display:
            stage_position_panel_adapter.refresh_coordinate_frame_display(self)
        if outcome.apply_objective_runtime:
            self._apply_settings()
        else:
            self._apply_settings(apply_objective_runtime=False)
        if outcome.observe_coordinate_authority:
            coordinate_flow.observe_coordinate_authority(self)
        for notice in outcome.post_apply_notices:
            self._show_status(notice.message, notice.timeout_ms)
        logger.info("Settings updated from dialog")

    def _design_metadata_with_calibration_fingerprints(
        self,
        metadata: DesignFrameMetadata | None,
    ) -> DesignFrameMetadata | None:
        if metadata is None:
            return None
        settings = getattr(getattr(self, "settings_manager", None), "settings", None)
        calibrations = getattr(settings, "axis_calibrations", None)
        if not isinstance(calibrations, dict):
            return metadata
        return replace(
            metadata,
            calibration_fingerprints=design_calibration_fingerprints(calibrations),
        )

    def _latest_camera_frame_photo(self) -> tuple[bytes, str] | None:
        frame = self._latest_camera_frame_for_notifications
        return self._qimage_telegram_photo(frame)

    @staticmethod
    def _qimage_telegram_photo(frame: QImage | None) -> tuple[bytes, str] | None:
        if frame is None or frame.isNull():
            return None
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return None
        if frame.save(buffer, "JPG", 88):
            return bytes(buffer.data()), "microscope.jpg"
        buffer.close()
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return None
        if frame.save(buffer, "PNG"):
            return bytes(buffer.data()), "microscope.png"
        return None

    @staticmethod
    def _route_attention_status(message: str) -> bool:
        text = str(message or "")
        if not text.startswith("Route measurement: point "):
            return False
        return "interrupted" in text or "Save Shift" in text

    def _sync_objective_combo(self, objective_name: str) -> None:
        combo = self._objective_combo
        if combo is None:
            return
        current_names = [
            str(combo.itemData(index) or "") for index in range(combo.count())
        ]
        plan = alignment.objective_combo_sync_plan(
            current_names, self._objective_names(), objective_name
        )
        if plan.rebuild_items:
            combo.blockSignals(True)
            combo.clear()
            for name in plan.names:
                combo.addItem(name, name)
            combo.blockSignals(False)
        if plan.selected_index < 0:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(plan.selected_index)
        combo.blockSignals(False)
        if plan.refresh_calibration_ui:
            self._refresh_objective_calibration_ui()

    def _on_objective_combo_changed(self, _index: int) -> None:
        combo = self._objective_combo
        if combo is None:
            return
        objective_name = str(combo.currentData() or "").strip().upper()
        if objective_name:
            self._set_active_objective(objective_name, apply_motion=True)

    def _set_active_objective(
        self,
        objective_name: str,
        *,
        apply_motion: bool,
        allow_stage_task: bool = False,
    ) -> None:
        plan = alignment.select_active_objective(
            self.settings_manager.settings,
            objective_name,
            is_busy=self._objective_mutation_busy(
                allow_stage_task=allow_stage_task,
            ),
            apply_motion=apply_motion,
        )
        if plan.refresh_calibration_ui:
            self._refresh_objective_calibration_ui()
            return
        if plan.restore_combo_name is not None:
            self._sync_objective_combo(plan.restore_combo_name)
        if not self._persist_objective_plan(plan, show_status=False):
            self._show_plan_status(plan)
            return
        if plan.apply_offset_motion:
            self._apply_objective_change_offset(plan.old_name, plan.new_name)
        self._show_plan_status(plan)

    def _objective_mutation_busy(
        self,
        *,
        allow_stage_task: bool = False,
        optical_context: OpticalCalibrationOutcome | None = None,
    ) -> bool:
        if self._api_stage_command_runtime.active():
            return True
        if self._microscope_scan_running():
            return True
        optical_owner = isinstance(optical_context, OpticalCalibrationOutcome)
        if optical_calibration_blocks_mutation(
            self._optical_calibration_runtime.state(),
            outcome_owns_mutation=optical_owner,
        ):
            return True
        stage_controller = getattr(self, "stage_controller", None)
        return (
            stage_controller is not None
            and stage_controller.is_busy()
            and not allow_stage_task
        )

    def _objective_profile_mutation_busy(
        self,
        objective_name: str,
        *,
        allow_stage_task: bool = False,
        optical_context: OpticalCalibrationOutcome | None = None,
    ) -> bool:
        name = normalize_objective_name(objective_name)
        active_name = normalize_objective_name(
            self.settings_manager.settings.objectives.active_name
        )
        if not name or name != active_name:
            return False
        return self._objective_mutation_busy(
            allow_stage_task=allow_stage_task,
            optical_context=optical_context,
        )

    def _apply_objective_change_offset(self, old_name: str, new_name: str) -> None:
        plan = alignment.objective_change_offset_plan(
            self.settings_manager.objectives_configuration(),
            old_name,
            new_name,
            self.stage_controller.latest_stage_position(),
            self.stage_controller.is_busy(),
            display_axis_value_from_raw=(
                lambda axis, raw: (
                    stage_position_panel_adapter.display_axis_value_from_raw(
                        self,
                        axis,
                        raw,
                    )
                )
            ),
            raw_axis_value_from_display=(
                lambda axis, display: (
                    stage_position_panel_adapter.raw_axis_value_from_display(
                        self,
                        axis,
                        display,
                    )
                )
            ),
        )
        if plan.status:
            self._show_plan_status(plan)
            return
        if plan.raw_targets is None:
            return
        accepted = self.stage_controller.request_absolute_axis_targets_move(
            plan.raw_targets,
            feedrate=self._current_linear_feedrate(),
            allow_unhomed=False,
        )
        status = plan.accepted_status if accepted else plan.rejected_status
        if status:
            self._show_status(status, plan.status_timeout_ms)

    def _apply_objective_settings(self) -> None:
        objective_settings = self.settings_manager.objectives_configuration()
        active_objective, objectives = alignment.active_objective_configuration(
            objective_settings
        )
        self.stage_controller.apply_objective_configuration(
            active_objective, objectives
        )
        self._sync_objective_combo(objective_settings.active_name)
        self._refresh_objective_calibration_ui()
        if self._design_session.document is not None:
            self._refresh_design_position()

    def _show_plan_status(self, plan) -> None:
        if plan.status:
            self._show_status(plan.status, plan.status_timeout_ms)

    def _persist_objective_plan(
        self,
        plan,
        *,
        show_status: bool = True,
        apply_objective_runtime: bool = True,
    ) -> bool:
        if plan.settings is None:
            if show_status:
                self._show_plan_status(plan)
            return False
        self.settings_manager.replace_and_save(
            plan.settings,
            preserve_exposure_policy=True,
        )
        if getattr(plan, "apply_settings", False) and apply_objective_runtime:
            self._apply_objective_settings()
        if getattr(plan, "refresh_design_position", False):
            self._refresh_design_position()
        if getattr(plan, "refresh_calibration_ui", False):
            self._refresh_objective_calibration_ui()
        coordinate_flow.observe_coordinate_authority(self)
        if show_status:
            self._show_plan_status(plan)
        return True
