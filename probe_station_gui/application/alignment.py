from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from probe_station_gui.application.stage_motion_types import AlignmentRotationCompletion
from probe_station_gui.coordinates.coordinator_model import (
    RegistrationCaptureRequest,
    RegistrationAlignmentRequest,
)
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.design.session_registration import AlignmentPreparation
from probe_station_gui.design import objective_alignment as alignment
from probe_station_gui.settings.objective_config import normalize_objective_name
from probe_station_gui.stage.exact_step import ExactStepClearReason
from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow
from probe_station_gui.views import main_window_coordinate_step as coordinate_step

logger = logging.getLogger("main")


@dataclass(frozen=True)
class _ManualAlignmentCaptureContext:
    request_id: str
    slot: int
    cancelled: threading.Event


@dataclass(frozen=True)
class _AlignmentRotationCorrelation:
    preparation: AlignmentPreparation | None
    collapse_design_on_success: bool


class _MainAlignmentMixin:
    def _design_backed_alignment_active(self) -> bool:
        return len(getattr(self, "_alignment_design_draft", ())) >= 2

    def _alignment_capture_slot_count(self) -> int:
        if self._design_backed_alignment_active():
            return len(self._alignment_design_draft)
        return 2

    def _design_window_is_open(self) -> bool:
        return (
            self.design_layout_window is not None
            and self.design_layout_window.isVisible()
        )

    def _collapse_alignment_panel_if_ready(self) -> None:
        if self.alignment_dock is None or not self._design_window_is_open():
            return
        if self._coordinate_system_coordinator.snapshot().registration.registration_valid:
            self.alignment_dock.set_collapsed(True)

    def _collapse_alignment_panel_if_design_open(self) -> None:
        if self.alignment_dock is None or not self._design_window_is_open():
            return
        self.alignment_dock.set_collapsed(True)

    def _set_alignment_panel_expanded(self) -> None:
        if self.alignment_dock is None:
            return
        self.alignment_dock.setVisible(True)
        self.alignment_dock.set_collapsed(False)
        self.alignment_dock.raise_()

    def _arm_manual_alignment_pick(self, slot: int) -> None:
        if not 0 <= slot < self._alignment_capture_slot_count():
            return
        if getattr(self, "_manual_alignment_capture_context", None) is not None:
            self._show_status("Alignment point capture is already running.", 4000)
            return
        self._manual_alignment_pick_generation = (
            getattr(self, "_manual_alignment_pick_generation", 0) + 1
        )
        self._manual_alignment_pick_slot = slot
        self._set_alignment_panel_expanded()
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)
        self._update_stage_coordinate_apply_state()
        self._show_status(
            f"Chip alignment: pick point {slot + 1} in the image, or press Space to capture the crosshair center.",
            6000,
        )

    def _cancel_manual_alignment_pick(self) -> None:
        context = getattr(self, "_manual_alignment_capture_context", None)
        if self._manual_alignment_pick_slot is None and context is None:
            return
        if isinstance(context, _ManualAlignmentCaptureContext):
            context.cancelled.set()
            self.stage_controller.cancel_clicked_point_resolution(
                context.request_id, "Alignment point capture cancelled."
            )
        self._manual_alignment_pick_slot = None
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)
        self._update_stage_coordinate_apply_state()
        self._show_status("Chip alignment image pick cancelled.", 3000)

    def _reset_manual_alignment(self, *, cancel_pick: bool = True) -> None:
        self._manual_alignment_points = [None, None]
        if cancel_pick:
            self._manual_alignment_pick_slot = None
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)
        self._update_stage_coordinate_apply_state()

    def _reset_alignment_capture_points(self) -> None:
        if self._design_backed_alignment_active():
            transition = self._coordinate_system_coordinator.clear_registration_source_stage_marks()
            coordinate_flow.apply_coordinate_transition(self, transition)
            self._stage_motion.discard_alignment_rotation()
            self._alignment_stage_draft = [None] * len(self._alignment_design_draft)
            self._alignment_draft_fit_residuals = None
            self._set_design_snap_enabled(True)
        else:
            self._reset_manual_alignment(cancel_pick=False)
            self._stage_motion.discard_alignment_rotation()
        self._manual_alignment_pick_slot = None
        self._set_alignment_panel_expanded()
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)
        self._update_stage_coordinate_apply_state()

    def _capture_manual_alignment_center_shortcut(self) -> None:
        if self._manual_alignment_pick_slot is None:
            return
        self._capture_manual_alignment_center(self._manual_alignment_pick_slot)

    def _resolve_alignment_capture_stage_position(self) -> tuple[float, float] | None:
        try:
            stage_position = self.stage_controller.current_stage_position()
        except Exception as exc:
            plan = alignment.alignment_capture_position_plan(
                error_message=str(exc),
                latest_position=self.stage_controller.latest_stage_position(),
            )
        else:
            plan = alignment.alignment_capture_position_plan(
                stage_position=stage_position
            )
        if plan.request_status_refresh:
            self.stage_controller.request_status_refresh()
        if plan.status:
            self._show_status(plan.status, plan.status_timeout_ms)
        return plan.stage_xy

    def _capture_manual_alignment_center(self, slot: int) -> None:
        if not 0 <= slot < self._alignment_capture_slot_count():
            return
        if getattr(self, "_manual_alignment_capture_context", None) is not None:
            self._show_status("Alignment point capture is already running.", 4000)
            return
        if self._design_backed_alignment_active():
            self._request_operator_alignment_machine_capture(
                slot,
                configured_target_xy=None,
                source="center",
            )
            return
        center_xy = self._resolve_alignment_capture_stage_position()
        if center_xy is None:
            return
        self._capture_manual_alignment_point(slot, center_xy, source="center")

    def _request_alignment_capture(self, slot: int, mode: str) -> None:
        if mode == "image":
            self._arm_manual_alignment_pick(slot)
            return
        self._capture_manual_alignment_center(slot)

    def _zero_b_axis(self) -> None:
        try:
            coordinate_step.clear_exact_steps(
                self,
                ExactStepClearReason.ALIGNMENT_CHANGED,
            )
            self._invalidate_design_registration(
                "Design registration cleared after B-axis zeroing."
            )
            self.stage_controller.zero_b_axis()
        except Exception as exc:
            self._show_status(str(exc), 5000)

    def _reset_click_calibration(self) -> tuple[bool, str]:
        active_name = normalize_objective_name(
            self.settings_manager.settings.objectives.active_name
        )
        if self._objective_profile_mutation_busy(active_name):
            message = (
                "Click-to-move calibration cannot be reset while a scan or "
                "calibration is active."
            )
            self._refresh_click_calibration_ui()
            self._show_status(message, 5000)
            return False, message
        message = (
            "Click-to-move calibration cleared. Click in the microscope view "
            "to recalibrate the active objective."
        )
        try:
            self.stage_controller.reset_calibration(message)
        except Exception as exc:
            error = str(exc)
            self._refresh_click_calibration_ui()
            self._show_status(error, 5000)
            return False, error
        return True, message

    def _capture_manual_alignment_clicked(
        self, dx_pixels: float = 0.0, dy_pixels: float = 0.0
    ) -> None:
        slot = self._manual_alignment_pick_slot
        if slot is None:
            return
        if getattr(self, "_manual_alignment_capture_context", None) is not None:
            self._show_status("Alignment point capture is already running.", 4000)
            return
        context = _ManualAlignmentCaptureContext(
            request_id=uuid.uuid4().hex,
            slot=slot,
            cancelled=threading.Event(),
        )
        self._manual_alignment_capture_context = context
        try:
            accepted = self.stage_controller.request_clicked_point_resolution(
                context.request_id,
                dx_pixels,
                dy_pixels,
            )
        except Exception as exc:
            accepted = False
            message = str(exc) or type(exc).__name__
        else:
            message = "Stage is busy; alignment point capture not started."
        if not accepted:
            if self._manual_alignment_capture_context is context:
                self._manual_alignment_capture_context = None
            self._refresh_manual_alignment_ui()
            self._update_stage_coordinate_apply_state()
            self._show_status(message, 5000)
            return
        self._refresh_manual_alignment_ui()
        self._update_stage_coordinate_apply_state()

    def _on_manual_alignment_point_resolved(
        self,
        request_id: object,
        success: bool,
        center_xy: object,
        captured_xy: object,
        message: str,
    ) -> None:
        context = getattr(self, "_manual_alignment_capture_context", None)
        if (
            not isinstance(context, _ManualAlignmentCaptureContext)
            or request_id != context.request_id
        ):
            return
        self._manual_alignment_capture_context = None
        if (
            context.cancelled.is_set()
            or self._manual_alignment_pick_slot != context.slot
        ):
            self._refresh_manual_alignment_ui()
            self._update_stage_coordinate_apply_state()
            return
        if not success:
            self._refresh_manual_alignment_ui()
            self._update_stage_coordinate_apply_state()
            self._show_status(
                str(message) or "Alignment point capture failed.",
                5000,
            )
            return
        try:
            center = (float(center_xy[0]), float(center_xy[1]))
            captured = (float(captured_xy[0]), float(captured_xy[1]))
        except (IndexError, TypeError, ValueError):
            self._refresh_manual_alignment_ui()
            self._update_stage_coordinate_apply_state()
            self._show_status(
                "Alignment point capture returned invalid coordinates.", 5000
            )
            return
        self._update_coordinate_display(center_xy=center, cursor_xy=captured)
        self._capture_manual_alignment_point(context.slot, captured, source="image")

    def _capture_manual_alignment_point(
        self, slot: int, captured: tuple[float, float], *, source: str
    ) -> None:
        if not 0 <= slot < self._alignment_capture_slot_count():
            return
        if self._design_backed_alignment_active():
            self._request_operator_alignment_machine_capture(
                slot,
                configured_target_xy=(float(captured[0]), float(captured[1])),
                source=source,
            )
            return

        self._manual_alignment_pick_slot = None
        self._refresh_manual_alignment_ui()
        self._update_stage_coordinate_apply_state()

        plan = alignment.manual_alignment_capture_plan(
            slot,
            captured,
            source=source,
            manual_points=self._manual_alignment_points,
            target_angles=self.ALIGNMENT_TARGET_ANGLES,
        )
        self._apply_alignment_capture_plan(plan)

    def _request_operator_alignment_machine_capture(
        self,
        slot: int,
        *,
        configured_target_xy: tuple[float, float] | None,
        source: str,
    ) -> None:
        try:
            pivot_value = self._rotation_geometry_snapshot().pivot_machine_xy
            pivot = (float(pivot_value[0]), float(pivot_value[1]))
            camera_origin = self._camera_stage_xy_from_raw_stage_xy((0.0, 0.0))
            objective_offset = (
                -float(camera_origin[0]),
                -float(camera_origin[1]),
            )
        except (DesignModelError, TypeError, ValueError) as exc:
            self._show_status(str(exc), 6000)
            return
        transition = self._coordinate_system_coordinator.capture_registration_mark(
            RegistrationCaptureRequest(
                pivot_machine_xy=pivot,
                objective_xy_offset=objective_offset,
                operator_alignment=True,
                mark_index=int(slot),
                configured_target_xy=configured_target_xy,
                capture_source=str(source),
                operator_pick_generation=(
                    getattr(self, "_manual_alignment_pick_generation", 0)
                    if getattr(self, "_manual_alignment_pick_slot", None) == slot
                    else None
                ),
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _apply_alignment_capture_plan(self, plan) -> None:
        if plan.points is not None:
            self._manual_alignment_points = plan.points
        if plan.apply_prepared_alignment and plan.preparation is not None:
            transition = (
                self._coordinate_system_coordinator.apply_registration_alignment(
                    RegistrationAlignmentRequest(plan.preparation)
                )
            )
            coordinate_flow.apply_coordinate_transition(self, transition)
            self._finish_alignment_draft()
        if plan.request_b_rotation and plan.rotation_deg is not None:
            accepted = self._stage_motion.request_alignment_rotation(
                plan.rotation_deg,
                _AlignmentRotationCorrelation(
                    preparation=plan.pending_preparation,
                    collapse_design_on_success=plan.pending_quick_alignment_rotation,
                ),
            )
            if not accepted:
                return
        if plan.invalidate_design_registration:
            self._invalidate_design_registration(
                "Design registration cleared after B-axis rotation."
            )
        for enabled, callback in (
            (plan.disable_snap, lambda: self._set_design_snap_enabled(False)),
            (plan.refresh_manual_ui, self._refresh_manual_alignment_ui),
            (plan.refresh_design_panel, self._refresh_design_panel),
            (plan.refresh_design_position, self._refresh_design_position),
            (plan.expand_alignment, self._set_alignment_panel_expanded),
            (plan.collapse_alignment_if_ready, self._collapse_alignment_panel_if_ready),
            (
                plan.collapse_alignment_if_design_open,
                self._collapse_alignment_panel_if_design_open,
            ),
        ):
            if enabled:
                callback()
        if plan.status:
            self._show_status(plan.status, plan.status_timeout_ms)

    def _on_alignment_rotation_finished(
        self,
        completion: AlignmentRotationCompletion,
    ) -> None:
        correlation = completion.correlation
        if not isinstance(correlation, _AlignmentRotationCorrelation):
            return
        preparation = correlation.preparation
        if preparation is not None:
            if completion.success:
                transition = (
                    self._coordinate_system_coordinator.apply_registration_alignment(
                        RegistrationAlignmentRequest(preparation)
                    )
                )
                coordinate_flow.apply_coordinate_transition(self, transition)
                self._finish_alignment_draft()
                self._set_design_snap_enabled(False)
                self._refresh_design_panel()
                self._refresh_design_position()
                self._collapse_alignment_panel_if_ready()
                self._microscope_interaction.clear_target()
                self._show_status(
                    "Design calibration complete. "
                    f"Rotation {preparation.rotation_deg:+.3f} deg, "
                    f"spacing ratio {preparation.distance_ratio:.3f}. "
                    f"RMS {preparation.rms_residual_mm:.4f} mm, "
                    f"max {preparation.max_residual_mm:.4f} mm.",
                    7000,
                )
            else:
                self._show_status(
                    f"Design calibration rotation failed: {completion.message}",
                    7000,
                )
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        if completion.message:
            self._show_status(completion.message, 5000)
        if completion.success:
            if correlation.collapse_design_on_success:
                self._collapse_alignment_panel_if_design_open()
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
        self._schedule_cancel_state_refresh()

    def _finish_alignment_draft(self) -> None:
        self._alignment_design_draft = ()
        self._alignment_stage_draft = []
        self._alignment_draft_fit_residuals = None
        design_layout_window = getattr(self, "design_layout_window", None)
        if design_layout_window is not None:
            design_layout_window.set_alignment_capture_points(())

    def _refresh_manual_alignment_ui(self) -> None:
        presentation = alignment.alignment_presentation(
            design_backed=self._design_backed_alignment_active(),
            design_stage_marks=self._alignment_stage_draft,
            manual_points=self._manual_alignment_points,
            pick_slot=self._manual_alignment_pick_slot,
            required_design_mark_count=len(self._alignment_design_draft),
        )
        if self.alignment_panel is not None:
            self.alignment_panel.set_design_marks(self._alignment_design_draft)
            self.alignment_panel.set_captured_points(presentation.captured_points)
            self.alignment_panel.set_pick_slot(presentation.pick_slot)
            self.alignment_panel.set_capture_running(
                getattr(self, "_manual_alignment_capture_context", None) is not None
            )
            self.alignment_panel.set_registration_status(
                self._coordinate_system_coordinator.snapshot().registration.registration_status
            )
            if self._alignment_draft_fit_residuals is not None:
                self.alignment_panel.set_fit_residuals(
                    *self._alignment_draft_fit_residuals
                )
        self._microscope_interaction.set_alignment_mode(presentation.alignment_mode)
        self._microscope_interaction.set_alignment_instruction(presentation.instruction)

    def _update_coordinate_display(
        self,
        *,
        center_xy: tuple[float, float] | None = None,
        cursor_xy: tuple[float, float] | None = None,
    ) -> None:
        latest = self.stage_controller.latest_stage_position()
        if (
            center_xy is None
            and latest is not None
            and len(latest) >= 2
            and self.stage_controller.axes_are_homed({"X", "Y"})
        ):
            center_xy = (float(latest[0]), float(latest[1]))
        if self.alignment_panel is not None:
            self.alignment_panel.set_coordinate_labels(
                self._format_active_coordinate_label("Center", center_xy),
                self._format_active_coordinate_label("Cursor", cursor_xy),
            )

    def _can_display_design_position(self) -> bool:
        return bool(
            self._design_session.document is not None
            and self._coordinate_system_coordinator.snapshot().registration.registration_valid
        )

    def _format_coordinate_label(
        self, prefix: str, fluidnc_xy: tuple[float, float] | None
    ) -> str:
        if fluidnc_xy is None:
            return f"{prefix}: unavailable"
        systems = self._resolve_coordinate_systems(fluidnc_xy)
        parts = [
            f"{name} X={coords[0]:.3f}, Y={coords[1]:.3f}"
            for name, coords in systems.items()
        ]
        return f"{prefix}: " + " | ".join(parts)

    def _format_active_coordinate_label(
        self, prefix: str, fluidnc_xy: tuple[float, float] | None
    ) -> str:
        if fluidnc_xy is None:
            return f"{prefix}: unavailable"
        return (
            f"{prefix}: {self.stage_controller.coordinate_display_name()} "
            f"X={fluidnc_xy[0]:.3f}, Y={fluidnc_xy[1]:.3f}"
        )

    def _resolve_coordinate_systems(
        self, fluidnc_xy: tuple[float, float]
    ) -> dict[str, tuple[float, float]]:
        coordinates = {
            f"FluidNC {self.stage_controller.coordinate_display_name()}": fluidnc_xy
        }
        chip_xy = self._resolve_chip_coordinates(fluidnc_xy)
        if chip_xy is not None:
            coordinates["Chip/Stage registered"] = chip_xy
        design_xy = self._resolve_design_coordinates(fluidnc_xy)
        if design_xy is not None:
            coordinates["Design"] = design_xy
        return coordinates

    def _resolve_chip_coordinates(
        self, fluidnc_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        registration = self._coordinate_system_coordinator.snapshot().registration.registration_projection
        if registration is None or not registration.valid:
            return None
        if not registration.source_stage_marks:
            return None
        camera_xy = self._camera_stage_xy_from_raw_stage_xy(fluidnc_xy)
        origin = registration.source_stage_marks[0]
        return (
            float(camera_xy[0]) - float(origin[0]),
            float(camera_xy[1]) - float(origin[1]),
        )

    def _resolve_design_coordinates(
        self, fluidnc_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        try:
            return self._design_xy_from_raw_stage_xy(fluidnc_xy)
        except Exception:
            return None
