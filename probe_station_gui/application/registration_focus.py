from __future__ import annotations

import json
import logging
from pathlib import Path

from probe_station_gui.application.stage_motion_types import PlannedXYMoveRequest
from probe_station_gui.coordinates.coordinator_model import (
    AutofocusResult as CoordinateAutofocusResult,
)
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    FocusCandidateRequest,
    FocusMoveResult,
    FocusReferenceRequest,
    FocusReferenceResetRequest,
    MachinePoseCaptureResult,
    RegistrationCaptureRequest,
    RegistrationCheckMarkRequest,
    RegistrationInvalidationRequest,
    RegistrationOpticalObservation,
    RegistrationSourceMarkRequest,
)
from probe_station_gui.design import navigation_targeting
from probe_station_gui.design.focus_candidate import select_central_focus_candidate
from probe_station_gui.design.klayout_structure_bounds_worker import (
    KLayoutStructureBoundsWorker,
)
from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    StructureBoundsFailure,
    StructureBoundsRequest,
    StructureBoundsResult,
)
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.settings.objective_config import normalize_objective_name
from probe_station_gui.views import (
    main_window_connection_flow as connection_flow,
)
from probe_station_gui.views import (
    main_window_coordinate_flow as coordinate_flow,
)
from probe_station_gui.views.main_window_auxiliary import toggle_design_layout_window

logger = logging.getLogger("main")


class _MainRegistrationFocusMixin:
    def on_autofocus_finished(self, success: bool, message: str) -> None:
        if message:
            self._show_status(message, 5000)
        if success:
            self._remember_sample_focus_from_latest()
        else:
            logger.error("Autofocus failed: %s", message)
        self._schedule_cancel_state_refresh()

    def _add_design_source_mark(self, x_value: float, y_value: float) -> None:
        if not self._design_mutation_ready():
            return
        transition = self._coordinate_system_coordinator.set_registration_source_mark(
            RegistrationSourceMarkRequest((x_value, y_value))
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        self._refresh_design_panel()
        self._show_status(
            f"Design source mark captured at X={x_value:.3f}, Y={y_value:.3f}.",
            4000,
        )

    def _add_design_check_mark(self, x_value: float, y_value: float) -> None:
        if not self._design_mutation_ready():
            return
        transition = self._coordinate_system_coordinator.add_registration_check_mark(
            RegistrationCheckMarkRequest((x_value, y_value))
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        self._refresh_design_panel()
        self._show_status(
            f"Design check mark captured at X={x_value:.3f}, Y={y_value:.3f}.",
            4000,
        )

    def _capture_stage_source_mark(self) -> None:
        self._capture_stage_registration_mark(check_mark=False)

    def _capture_stage_check_mark(self) -> None:
        self._capture_stage_registration_mark(check_mark=True)

    def _capture_stage_registration_mark(self, *, check_mark: bool) -> None:
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
                check_mark=check_mark,
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _on_registration_machine_coordinate_snapshot_finished(
        self,
        request_id: object,
        success: bool,
        snapshot: object,
        message: str,
    ) -> None:
        if not isinstance(request_id, int):
            return
        transition = self._coordinate_system_coordinator.complete(
            CoordinateAdapterCompletion(
                intent_id=request_id,
                result=MachinePoseCaptureResult(
                    request_id,
                    succeeded=bool(success),
                    snapshot=snapshot,
                    message=str(message or ""),
                    active_operator_pick_slot=getattr(
                        self,
                        "_manual_alignment_pick_slot",
                        None,
                    ),
                    active_operator_pick_generation=getattr(
                        self,
                        "_manual_alignment_pick_generation",
                        None,
                    ),
                ),
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _design_spacing_ratio_is_reasonable(self, ratio: float) -> bool:
        return abs(float(ratio) - 1.0) <= self.DESIGN_SPACING_RATIO_TOLERANCE

    def _clear_design_registration(self) -> None:
        transition = coordinate_flow.activate_current_design(self, create_new=True)
        if transition is None or not transition.accepted:
            return
        self._stage_motion.discard_alignment_rotation()
        self._last_selected_design_point = None
        self._set_design_snap_enabled(True)
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status("Design calibration restarted.", 4000)

    def _design_registration_instances(self) -> tuple[tuple[str, str], ...]:
        return self._coordinate_system_coordinator.snapshot().registration.registration_instances

    def _reconcile_missing_design_registration_instance(self) -> None:
        snapshot = self._coordinate_system_coordinator.snapshot().registration
        if (
            snapshot.active_frame_id
            == self._coordinate_system_coordinator.current_design_lease().frame_id
        ):
            return
        coordinate_flow.activate_current_design(self)

    def _select_design_registration_instance(self, frame_id: str) -> None:
        if not self._design_edit_safe():
            self._show_status("Design editing is locked.", 4000)
            return
        selected_id = str(frame_id).strip()
        if (
            selected_id
            == self._coordinate_system_coordinator.snapshot().registration.active_frame_id
        ):
            return
        transition = coordinate_flow.activate_current_design(
            self,
            requested_frame_id=selected_id,
        )
        if transition is None or not transition.accepted or transition.notices:
            return
        self._stage_motion.discard_alignment_rotation()
        self._last_selected_design_point = None
        self._clear_design_focus_overlay_state()
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status("Registration selected.", 4000)

    def _new_design_registration_instance(self) -> None:
        if not self._design_edit_safe():
            self._show_status("Design editing is locked.", 4000)
            return
        self._clear_design_registration()

    def _invalidate_design_registration(self, reason: str) -> None:
        self._stage_motion.discard_alignment_rotation()
        transition = self._coordinate_system_coordinator.invalidate_registration(
            RegistrationInvalidationRequest(reason)
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        if self._design_session.document is not None:
            self._set_design_snap_enabled(True)

    def _on_design_target_selected(self, target_id: str) -> None:
        navigation_targeting.select_design_target(self._design_session, target_id)
        self._refresh_design_panel()

    def _select_next_design_target(self) -> None:
        plan = navigation_targeting.select_next_design_target(self._design_session)
        self._refresh_design_panel()
        if plan.status_message is not None:
            self._show_status(plan.status_message, plan.status_timeout_ms)

    def _select_previous_design_target(self) -> None:
        plan = navigation_targeting.select_previous_design_target(self._design_session)
        self._refresh_design_panel()
        if plan.status_message is not None:
            self._show_status(plan.status_message, plan.status_timeout_ms)

    def _move_to_design_target(self, target_id: str) -> None:
        selection = navigation_targeting.select_design_target(
            self._design_session,
            target_id,
        )
        target = selection.target
        stage_xy = (
            self._raw_stage_xy_from_design_xy(target.design_center)
            if target is not None
            else None
        )
        plan = navigation_targeting.plan_design_target_move(
            self._design_session,
            target_id,
            stage_xy,
        )
        if not plan.accepted:
            if plan.status_message is not None:
                self._show_status(plan.status_message, plan.status_timeout_ms)
            return
        self._refresh_design_panel()
        assert plan.stage_xy is not None
        self._stage_motion.request_planned_xy_move(
            PlannedXYMoveRequest(
                target_stage_xy=plan.stage_xy,
                source_label="design target",
            )
        )

    def _move_to_minimap_design_point(self, x_value: float, y_value: float) -> None:
        design_xy = (float(x_value), float(y_value))
        if not self._move_to_design_coordinate(design_xy, source_label="minimap point"):
            return
        self._show_status(
            f"Moving to minimap point X={design_xy[0]:.3f}, Y={design_xy[1]:.3f}.",
            3000,
        )

    def _open_design_window_from_minimap_point(
        self,
        x_value: float,
        y_value: float,
    ) -> None:
        _ = float(x_value), float(y_value)
        toggle_design_layout_window(self, True)

    def _move_to_design_coordinate(
        self,
        design_xy: tuple[float, float],
        *,
        source_label: str,
    ) -> bool:
        document = self._design_session.document
        stage_xy = (
            self._raw_stage_xy_from_design_xy(design_xy)
            if document is not None
            else None
        )
        plan = navigation_targeting.plan_design_coordinate_move(
            document is not None,
            self.stage_controller.is_busy() if document is not None else False,
            design_xy,
            stage_xy,
            source_label,
        )
        if not plan.accepted:
            if plan.status_message is not None:
                self._show_status(plan.status_message, plan.status_timeout_ms)
            return False
        self._last_selected_design_point = plan.last_selected_design_point
        self._refresh_design_panel()
        assert plan.stage_xy is not None and plan.design_xy is not None
        if not self._stage_motion.request_planned_xy_move(
            PlannedXYMoveRequest(
                target_stage_xy=plan.stage_xy,
                source_label=plan.source_label,
            )
        ):
            return False
        logger.debug(
            "DESIGN MOVE source=%s design=(%.3f, %.3f) stage=(%.3f, %.3f)",
            plan.source_label,
            plan.design_xy[0],
            plan.design_xy[1],
            plan.stage_xy[0],
            plan.stage_xy[1],
        )
        return True

    def _find_design_focus_reference(self) -> None:
        document = self._design_session.document
        fov_size = self._resolve_design_fov_size()
        if document is None or fov_size is None:
            self._show_status("Current field of view is unavailable.", 5000)
            return
        self._observe_design_focus_context()
        self._focus_structure_request_id = (
            int(getattr(self, "_focus_structure_request_id", 0)) + 1
        )
        request_id = self._focus_structure_request_id
        optical = self._registration_optical_observation()
        context = self._coordinate_system_coordinator.focus_search_lease(optical)
        if context is None:
            self._show_status("Design focus context is unavailable.", 5000)
            return
        if document.file_backed:
            config = KLayoutConfig(
                path=Path(document.path).expanduser().resolve(),
                top_cell_name=document.top_cell_name,
                visible_layers=frozenset(document.visible_layers),
                source_bounds=tuple(document.cell_bounds[document.top_cell_name]),
                display_bounds=tuple(document.bounds),
                rotation_quarter_turns=document.rotation_quarter_turns,
                generation=request_id,
                source_load_id=document.source_load_id,
            )
            fixture_polygons: tuple[object, ...] = ()
        else:
            config = None
            fixture_polygons = tuple(
                polygon
                for layer in sorted(document.visible_layers)
                for polygon in document.polygons_by_layer.get(layer, ())
            )
        request = StructureBoundsRequest(
            request_id=request_id,
            generation=request_id,
            config=config,
            fixture_polygons=fixture_polygons,
        )
        self._pending_focus_structure_request_id = request_id
        self._pending_focus_structure_context = context
        self._pending_focus_structure_fov = tuple(fov_size)
        self._pending_focus_structure_design_bounds = tuple(document.bounds)
        worker = self._ensure_focus_structure_bounds_worker()
        worker.submit(request)
        self._show_status("Finding focus reference.", 3000)

    def _ensure_focus_structure_bounds_worker(self):
        worker = getattr(self, "_focus_structure_bounds_worker", None)
        if worker is not None:
            return worker
        worker = KLayoutStructureBoundsWorker(self)
        worker.ready.connect(self._on_focus_structure_bounds_ready)
        worker.failed.connect(self._on_focus_structure_bounds_failed)
        self._focus_structure_bounds_worker = worker
        return worker

    def _on_focus_structure_bounds_ready(
        self,
        result: StructureBoundsResult,
    ) -> None:
        pending_id = getattr(self, "_pending_focus_structure_request_id", None)
        pending_context = getattr(self, "_pending_focus_structure_context", None)
        if (
            result.request_id != pending_id
            or result.generation != pending_id
            or pending_context
            != self._coordinate_system_coordinator.focus_search_lease(
                self._registration_optical_observation()
            )
        ):
            return
        self._pending_focus_structure_request_id = None
        fov_size = getattr(self, "_pending_focus_structure_fov", None)
        design_bounds = getattr(self, "_pending_focus_structure_design_bounds", None)
        if fov_size is None or design_bounds is None:
            return
        try:
            candidate = select_central_focus_candidate(
                design_bounds=design_bounds,
                structure_bounds=result.structure_bounds,
                fov_size=fov_size,
            )
        except ValueError as exc:
            self._show_status(str(exc), 5000)
            return
        if candidate is None:
            self._show_status(
                "No focus structure fits the current field of view.", 5000
            )
            return
        transition = self._coordinate_system_coordinator.offer_focus_candidate(
            FocusCandidateRequest(
                candidate=candidate,
                optical=self._registration_optical_observation(),
                lease=pending_context,
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        self._show_status(
            "Focus reference found. Review and use the selected point.", 5000
        )

    def _on_focus_structure_bounds_failed(
        self,
        failure: StructureBoundsFailure,
    ) -> None:
        if failure.request_id != getattr(
            self, "_pending_focus_structure_request_id", None
        ):
            return
        self._pending_focus_structure_request_id = None
        self._show_status("Unable to inspect visible design structures.", 5000)

    def _design_focus_optical_context_key(self) -> tuple[str, str]:
        try:
            objectives = self.settings_manager.objectives_configuration()
            objective_name = normalize_objective_name(objectives.active_name)
            profile = objectives.objectives.get(objective_name)
            payload = None if profile is None else profile.to_dict()
            identity = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        except (AttributeError, TypeError, ValueError):
            return "", ""
        return objective_name, identity

    def _clear_design_focus_overlay_state(self, *, clear_window: bool = True) -> None:
        self._pending_focus_structure_request_id = None
        self._pending_focus_structure_context = None
        self._pending_focus_structure_fov = None
        self._pending_focus_structure_design_bounds = None
        window = getattr(self, "design_layout_window", None)
        if window is not None and clear_window:
            window.set_focus_candidate(None)
            window.set_selected_focus_point(None)

    def _use_selected_design_focus_reference(
        self,
        design_point: tuple[float, float],
    ) -> None:
        snapshot = self.stage_controller.latest_machine_coordinate_snapshot()
        if snapshot is None:
            self._show_status("Current Machine coordinates are unavailable.", 5000)
            return
        try:
            pivot = self._rotation_geometry_snapshot().pivot_machine_xy
            objective_offset = self._active_objective_xy_offset()
        except (DesignModelError, TypeError, ValueError) as exc:
            self._show_status(str(exc), 5000)
            return
        transition = self._coordinate_system_coordinator.use_focus_reference(
            FocusReferenceRequest(
                design_point=(float(design_point[0]), float(design_point[1])),
                optical=self._registration_optical_observation(),
                machine_snapshot=snapshot,
                pivot_machine_xy=(float(pivot[0]), float(pivot[1])),
                objective_xy_offset=(
                    float(objective_offset[0]),
                    float(objective_offset[1]),
                ),
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _observe_design_focus_context(self) -> None:
        transition = self._coordinate_system_coordinator.observe_focus_context(
            self._registration_optical_observation()
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _on_registration_focus_move_finished(
        self,
        completed_token: object,
        completed_target_xy: object,
        success: bool,
        message: str,
    ) -> None:
        if not isinstance(completed_token, int):
            return
        try:
            completed_target = (
                float(completed_target_xy[0]),
                float(completed_target_xy[1]),
            )
        except (IndexError, TypeError, ValueError):
            completed_target = None
        transition = self._coordinate_system_coordinator.complete(
            CoordinateAdapterCompletion(
                completed_token,
                FocusMoveResult(
                    completed_token,
                    succeeded=bool(success),
                    message=str(message or ""),
                    completed_target_xy=completed_target,
                ),
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _on_registration_focus_move_signal(
        self,
        completed_token: object,
        completed_target_xy: object,
        success: bool,
        message: str,
    ) -> None:
        self._on_registration_focus_move_finished(
            completed_token,
            completed_target_xy,
            success,
            message,
        )

    def _on_registration_focus_autofocus_finished(
        self,
        token: object,
        success: bool,
        physical_z_mm: object,
        message: str,
    ) -> None:
        if not isinstance(token, int):
            return
        try:
            physical_z = None if physical_z_mm is None else float(physical_z_mm)
        except (TypeError, ValueError):
            physical_z = None
        transition = self._coordinate_system_coordinator.complete(
            CoordinateAdapterCompletion(
                token,
                CoordinateAutofocusResult(
                    token,
                    succeeded=bool(success),
                    physical_z_mm=physical_z,
                    message=str(message or ""),
                ),
            ),
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _registration_optical_observation(self) -> RegistrationOpticalObservation:
        fov_size = self._resolve_design_fov_size() or (0.0, 0.0)
        objective_name, identity = self._design_focus_optical_context_key()
        return RegistrationOpticalObservation(
            fov_size=(float(fov_size[0]), float(fov_size[1])),
            objective_name=objective_name,
            optical_calibration_identity=identity,
        )

    def _reset_design_focus_reference(self) -> None:
        snapshot = self.stage_controller.latest_machine_coordinate_snapshot()
        if snapshot is None:
            self._show_status("Current Machine coordinates are unavailable.", 5000)
            return
        try:
            pivot = self._rotation_geometry_snapshot().pivot_machine_xy
            objective_offset = self._active_objective_xy_offset()
        except (DesignModelError, TypeError, ValueError) as exc:
            self._show_status(str(exc), 5000)
            return
        transition = self._coordinate_system_coordinator.reset_focus_reference(
            FocusReferenceResetRequest(
                machine_snapshot=snapshot,
                pivot_machine_xy=(float(pivot[0]), float(pivot[1])),
                objective_xy_offset=(
                    float(objective_offset[0]),
                    float(objective_offset[1]),
                ),
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _connect_design_focus_signals(self) -> None:
        window = getattr(self, "design_layout_window", None)
        if window is None or bool(
            getattr(self, "_design_focus_signals_connected", False)
        ):
            return
        window.find_focus_reference_requested.connect(self._find_design_focus_reference)
        window.focus_reference_requested.connect(
            lambda x_value, y_value: self._use_selected_design_focus_reference(
                (x_value, y_value)
            )
        )
        window.reset_focus_reference_requested.connect(
            self._reset_design_focus_reference
        )
        window.registration_instance_selected.connect(
            self._select_design_registration_instance
        )
        window.new_registration_requested.connect(
            self._new_design_registration_instance
        )
        self._design_focus_signals_connected = True

    def _refresh_design_panel(self) -> None:
        self._reconcile_missing_design_registration_instance()
        self._observe_design_focus_context()
        panel = self.design_navigator_panel
        self._connect_design_focus_signals()
        route_execution = self._route_run_execution.snapshot()
        coordinate_snapshot = self._coordinate_system_coordinator.snapshot()
        p = navigation_targeting.design_panel_presentation(
            self._design_session,
            coordinate_snapshot.registration,
            route_running=route_execution.thread_alive,
            design_snap_enabled=self._design_snap_enabled,
        )
        registration_instances = self._design_registration_instances()
        active_frame_id = coordinate_snapshot.registration.active_frame_id
        active_record = next(
            (
                record
                for record in coordinate_snapshot.records
                if record.frame_id == active_frame_id
            ),
            None,
        )
        if panel is not None:
            panel.set_document(p.document)
            panel.set_design_registration_active(p.registration_valid)
            panel.set_targets(p.targets, selected_target_id=p.selected_target_id)
            panel.set_route(
                p.route, selected_route_point_index=p.selected_route_point_index
            )
            panel.set_route_measurement_running(p.route_measurement_running)
            panel.set_calibration_prompt(p.calibration_prompt)
            panel.set_registration_status(p.registration_status)
            if hasattr(panel, "set_registration_instances"):
                panel.set_registration_instances(
                    registration_instances,
                    selected_frame_id=active_frame_id,
                )
            panel.set_registration_marks(p.source_design_marks, p.check_design_marks)
            panel.set_stage_registration_marks(p.source_stage_marks)
            if hasattr(panel, "set_focus_reference_state"):
                panel.set_focus_reference_state(
                    z_ready=bool(
                        active_record is not None
                        and active_record.readiness["Z"].available
                    ),
                    a_ready=bool(
                        active_record is not None
                        and active_record.readiness["A"].available
                    ),
                )
        if self.design_layout_window is not None:
            self.design_layout_window.set_snap_enabled(p.design_snap_enabled)
            self.design_layout_window.set_document(p.document)
            self.design_layout_window.set_targets(
                p.targets, selected_target_id=p.selected_target_id
            )
            self.design_layout_window.set_probe_route(
                p.route, selected_route_point_index=p.selected_route_point_index
            )
            self.design_layout_window.set_markup(getattr(self, "_design_markup", None))
            self.design_layout_window.set_guide_undo_available(
                bool(self._prune_design_guide_undo_stack())
            )
            self.design_layout_window.set_route_edit_enabled(self._design_edit_safe())
            self.design_layout_window.set_navigation_enabled(p.registration_valid)
            self.design_layout_window.set_registration_marks(
                p.source_design_marks, p.check_design_marks
            )
            self.design_layout_window.set_stage_registration_marks(p.source_stage_marks)
            self.design_layout_window.set_registration_instances(
                registration_instances,
                selected_frame_id=active_frame_id,
            )
        self._refresh_manual_alignment_ui()
        self._update_design_position(self._current_design_stage_xy)
        connection_flow.persist_controller_state_if_available(self)
