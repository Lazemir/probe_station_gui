"""Combined design window and controls for GDS-backed workflows."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.design.model import (
    DesignDocument,
    MeasurementTarget,
    Point2D,
    SnapResult,
)
from probe_station_gui.design.selection_model import (
    EntityOwner,
    SelectionModel,
)
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.views.design_navigator_enablement import (
    DesignNavigatorEnablement,
)
from probe_station_gui.views.design_document_controls import DesignDocumentControls
from probe_station_gui.views.design_plot_pane import pg
from probe_station_gui.views.design_registration_controls import (
    DesignRegistrationControls,
)
from probe_station_gui.views.design_route_controls import DesignRouteControls
from probe_station_gui.views.design_route_run_controls import DesignRouteRunControls
from probe_station_gui.views.design_tool_controls import DesignToolControls


class DesignNavigatorPanel(QWidget):
    """Control panel for loading a design, registration, and target navigation."""

    load_design_requested = Signal(str)
    unload_design_requested = Signal()
    top_cell_changed = Signal(str)
    layer_visibility_changed = Signal(int, int, bool)
    design_rotate_requested = Signal(int)
    move_to_target_requested = Signal(str)
    next_target_requested = Signal()
    previous_target_requested = Signal()
    target_selected = Signal(str)
    snap_enabled_changed = Signal(bool)
    route_new_requested = Signal()
    route_open_requested = Signal(str)
    route_save_requested = Signal()
    route_save_as_requested = Signal(str)
    route_add_current_requested = Signal()
    route_remove_selected_requested = Signal()
    route_clear_requested = Signal()
    route_selected = Signal(int)
    route_measurement_run_requested = Signal()
    route_measurement_measure_requested = Signal(int)
    route_measurement_stop_requested = Signal()
    route_measurement_interrupt_requested = Signal()
    route_measurement_pause_requested = Signal()
    route_measurement_save_shift_requested = Signal(int)
    route_measurement_confirmation_requested = Signal(str)
    route_measurement_jump_requested = Signal(int)
    route_measurement_move_requested = Signal(int)
    route_offsets_changed = Signal(float, float, float, float)
    route_edit_enabled_changed = Signal(bool)
    route_pick_mode_changed = Signal(object)
    route_preview_changed = Signal(object)
    tool_measure_preview_changed = Signal(object)
    tool_measurements_changed = Signal(object)
    active_design_tool_changed = Signal(str)
    alignment_draft_changed = Signal(object)
    alignment_draft_accepted = Signal(object)
    alignment_draft_discarded = Signal()
    selection_requested = Signal(object, str)
    delete_selection_requested = Signal()
    guide_undo_requested = Signal()
    guide_clear_requested = Signal()
    markup_visibility_changed = Signal(bool)
    mixed_array_requested = Signal(object)
    mixed_array_preview_changed = Signal(object, object)
    route_array_requested = Signal(
        float,
        float,
        float,
        float,
        int,
        float,
        float,
        int,
        bool,
        bool,
        object,
    )
    find_focus_reference_requested = Signal()
    use_selected_focus_requested = Signal()
    reset_focus_reference_requested = Signal()
    registration_instance_selected = Signal(str)
    new_registration_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._snap_enabled = True
        self._targets: list[MeasurementTarget] = []
        self._selected_target_id: str | None = None
        self._current_design_position: Point2D | None = None

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        self._availability_label = QLabel(self)
        self._availability_label.setWordWrap(True)
        root_layout.addWidget(self._availability_label)
        self._snap_checkbox = QCheckBox("Snap To Geometry", self)
        self._snap_checkbox.setChecked(True)
        self._snap_checkbox.toggled.connect(self._on_snap_checkbox_toggled)
        root_layout.addWidget(self._snap_checkbox)
        self._snap_hint_label = QLabel("Hover snap: move over a line or corner.", self)
        self._snap_hint_label.setWordWrap(True)
        self._snap_hint_label.setStyleSheet("QLabel { color: #b0bec5; }")
        root_layout.addWidget(self._snap_hint_label)

        self.document_controls = DesignDocumentControls(self)
        self.document_controls.load_requested.connect(self.load_design_requested.emit)
        self.document_controls.unload_requested.connect(
            self.unload_design_requested.emit
        )
        self.document_controls.top_cell_changed.connect(self.top_cell_changed.emit)
        self.document_controls.layer_visibility_changed.connect(
            self.layer_visibility_changed.emit
        )
        root_layout.addWidget(self.document_controls)

        self.registration_controls = DesignRegistrationControls(self)
        self.registration_controls.registration_instance_selected.connect(
            self.registration_instance_selected.emit
        )
        self.registration_controls.new_registration_requested.connect(
            self.new_registration_requested.emit
        )
        self.registration_controls.find_focus_reference_requested.connect(
            self.find_focus_reference_requested.emit
        )
        self.registration_controls.use_selected_focus_requested.connect(
            self.use_selected_focus_requested.emit
        )
        self.registration_controls.reset_focus_reference_requested.connect(
            self.reset_focus_reference_requested.emit
        )
        root_layout.addWidget(self.registration_controls)

        self.tool_controls = DesignToolControls(self)
        self.route_run_controls = DesignRouteRunControls(self)
        self.route_controls = DesignRouteControls(
            tool_controls=self.tool_controls,
            run_controls=self.route_run_controls,
            parent=self,
        )
        self.route_controls.new_requested.connect(self.route_new_requested.emit)
        self.route_controls.open_requested.connect(self.route_open_requested.emit)
        self.route_controls.save_requested.connect(self.route_save_requested.emit)
        self.route_controls.save_as_requested.connect(self.route_save_as_requested.emit)
        self.route_controls.add_current_requested.connect(
            self.route_add_current_requested.emit
        )
        self.route_controls.remove_selected_requested.connect(
            self.route_remove_selected_requested.emit
        )
        self.route_controls.clear_requested.connect(self.route_clear_requested.emit)
        self.route_controls.route_selected.connect(self.route_selected.emit)
        self.route_controls.selection_requested.connect(
            self._on_route_selection_requested
        )
        self.route_controls.offsets_changed.connect(self._on_route_offsets_changed)

        self.route_run_controls.measure_requested.connect(
            self._emit_route_measurement_measure_selected
        )
        self.route_run_controls.pause_requested.connect(
            self.route_measurement_pause_requested.emit
        )
        self.route_run_controls.interrupt_requested.connect(
            self.route_measurement_interrupt_requested.emit
        )
        self.route_run_controls.resume_requested.connect(
            lambda: self.route_measurement_confirmation_requested.emit("next")
        )
        self.route_run_controls.stop_requested.connect(
            self.route_measurement_stop_requested.emit
        )
        self.route_run_controls.save_shift_requested.connect(
            self._emit_route_measurement_save_shift_selected
        )
        self.route_run_controls.confirmation_requested.connect(
            self.route_measurement_confirmation_requested.emit
        )
        self.route_run_controls.jump_requested.connect(
            self._emit_route_measurement_jump_to_selected
        )
        self.route_run_controls.move_requested.connect(
            self._emit_route_measurement_move_to_selected
        )

        self.tool_controls.rotate_requested.connect(self.design_rotate_requested.emit)
        self.tool_controls.active_tool_changed.connect(
            self.active_design_tool_changed.emit
        )
        self.tool_controls.route_pick_mode_changed.connect(
            self.route_pick_mode_changed.emit
        )
        self.tool_controls.route_preview_changed.connect(
            self.route_preview_changed.emit
        )
        self.tool_controls.measure_preview_changed.connect(
            self.tool_measure_preview_changed.emit
        )
        self.tool_controls.measurements_changed.connect(
            self.tool_measurements_changed.emit
        )
        self.tool_controls.alignment_draft_changed.connect(
            self.alignment_draft_changed.emit
        )
        self.tool_controls.alignment_draft_accepted.connect(
            self.alignment_draft_accepted.emit
        )
        self.tool_controls.alignment_draft_discarded.connect(
            self.alignment_draft_discarded.emit
        )
        self.tool_controls.delete_selection_requested.connect(
            self.delete_selection_requested.emit
        )
        self.tool_controls.guide_undo_requested.connect(self.guide_undo_requested.emit)
        self.tool_controls.guide_clear_requested.connect(
            self.guide_clear_requested.emit
        )
        self.tool_controls.markup_visibility_changed.connect(
            self._on_markup_visibility_changed
        )
        self.tool_controls.mixed_array_requested.connect(
            self.mixed_array_requested.emit
        )
        self.tool_controls.mixed_array_preview_changed.connect(
            self.mixed_array_preview_changed.emit
        )
        root_layout.addWidget(self.route_controls)
        self._current_position_label = QLabel("Stage: unavailable", self)
        self._current_position_label.setWordWrap(True)
        root_layout.addWidget(self._current_position_label)
        root_layout.addStretch(1)

        self._update_availability()
        self._update_enabled_state()

    def set_document(self, document: DesignDocument | None) -> None:
        if document is self.document_controls.document:
            return
        self.document_controls.set_document(document)
        self.registration_controls.reset_marks()
        self.route_controls.set_design_directory(
            document.path.parent if document is not None else None
        )
        self._replace_tool_context(route_changed=True)
        self._update_enabled_state()

    def set_design_load_pending(self, pending: bool) -> None:
        self.document_controls.set_load_pending(pending)
        self._update_enabled_state()

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._targets = list(targets)
        self._selected_target_id = selected_target_id
        self._update_enabled_state()

    def set_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> None:
        self.route_controls.set_route(
            route,
            selected_route_point_index=selected_route_point_index,
        )
        self._replace_tool_context(route_changed=True)
        self._update_enabled_state()

    def set_selectable_entities(self, entities: object) -> None:
        self.tool_controls.set_selectable_entities(entities)
        self._sync_route_selection()
        self._replace_tool_context()
        self._update_enabled_state()

    def set_selection(self, selection: SelectionModel) -> None:
        self.tool_controls.set_selection(selection)
        self._sync_route_selection()
        self._replace_tool_context()
        self._update_enabled_state()

    def set_markup_visible(self, visible: bool) -> None:
        self.tool_controls.set_markup_visible(visible)
        self._sync_route_selection()
        self._update_enabled_state()

    def set_markup_state(self, *, visible: bool, guide_count: int) -> None:
        self.tool_controls.set_markup_state(
            visible=visible,
            guide_count=guide_count,
        )
        self._sync_route_selection()
        self._update_enabled_state()

    def set_guide_undo_available(self, available: bool) -> None:
        self.tool_controls.set_guide_undo_available(available)
        self._update_enabled_state()

    def set_route_measurement_running(self, running: bool) -> None:
        self.route_run_controls.set_running(running)
        self._replace_tool_context()
        self._update_enabled_state()

    def set_route_measurement_waiting(
        self,
        waiting: bool,
        reason: str = "",
    ) -> None:
        self.route_run_controls.set_waiting(waiting, reason)
        self._update_enabled_state()

    def set_route_measurement_pause_request_pending(self, pending: bool) -> None:
        self.route_run_controls.set_pause_request_pending(pending)
        self._update_enabled_state()

    def set_route_measurement_interrupt_request_pending(self, pending: bool) -> None:
        self.route_run_controls.set_interrupt_request_pending(pending)
        self._update_enabled_state()

    def set_route_measurement_status(self, text: str) -> None:
        self.route_run_controls.set_status(text)

    def set_registration_status(self, text: str) -> None:
        self.registration_controls.set_status(text)

    def set_design_registration_active(self, active: bool) -> None:
        self.registration_controls.set_active(active)
        self._update_enabled_state()

    def set_registration_instances(
        self,
        instances: object,
        *,
        selected_frame_id: str | None,
    ) -> None:
        self.registration_controls.set_instances(
            instances,
            selected_frame_id=selected_frame_id,
        )
        self._update_enabled_state()

    def set_focus_selection_available(self, available: bool) -> None:
        self.registration_controls.set_focus_selection_available(available)
        self._update_enabled_state()

    def set_focus_reference_state(self, *, z_ready: bool, a_ready: bool) -> None:
        self.registration_controls.set_focus_reference_state(
            z_ready=z_ready,
            a_ready=a_ready,
        )
        self._update_enabled_state()

    def set_calibration_prompt(self, text: str) -> None:
        self.registration_controls.set_calibration_prompt(text)

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> None:
        _ = check_design_marks
        self.registration_controls.set_registration_marks(source_design_marks)
        self._update_enabled_state()

    def set_stage_registration_marks(
        self, source_stage_marks: list[Point2D | None]
    ) -> None:
        self.registration_controls.set_stage_registration_marks(source_stage_marks)
        self._update_enabled_state()

    def set_current_position(
        self,
        stage_xy: Point2D | None,
        design_xy: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        _ = fov_design_size
        if stage_xy is None:
            self._current_position_label.setText("Stage: unavailable")
            self._current_design_position = None
        elif design_xy is None:
            self._current_design_position = None
            self._current_position_label.setText(
                f"Stage X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f}"
            )
        else:
            self._current_design_position = design_xy
            self._current_position_label.setText(
                f"Stage X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f} | "
                f"Design X={design_xy[0]:.3f}, Y={design_xy[1]:.3f}"
            )
        self._update_enabled_state()

    def set_status_message(self, text: str) -> None:
        self._availability_label.setText(text)

    def detach_tool_toolbar(self) -> QWidget:
        return self.tool_controls.detach_toolbar()

    def detach_tool_options_panel(self) -> QWidget:
        return self.tool_controls.detach_options_panel()

    def set_design_dialog_directory(self, directory: str | Path | None) -> None:
        """Update the preferred starting directory for opening GDS files."""
        self.document_controls.set_dialog_directory(directory)

    def set_hover_snap(self, snap_result: SnapResult | None) -> None:
        if not self._snap_enabled:
            self._snap_hint_label.setText(
                "Snap off: clicks use the exact cursor position."
            )
            return
        if snap_result is None:
            self._snap_hint_label.setText("Hover snap: move over a line or corner.")
            return
        if snap_result.mode == "free":
            self._snap_hint_label.setText(
                "Hover snap: no nearby geometry, click uses the exact cursor position."
            )
            return
        label = {
            "segment_center": "Center",
            "segment": "Line",
            "vertex": "Corner",
            "guide_end": "Guide End",
            "guide_center": "Guide Center",
            "guide_intersection": "Guide Intersection",
        }.get(snap_result.mode, "Line")
        self._snap_hint_label.setText(
            f"Hover snap: {label} at X={snap_result.point[0]:.3f}, "
            f"Y={snap_result.point[1]:.3f} | distance {snap_result.distance:.4f}"
        )

    def set_tool_hover_snap(
        self,
        snap_result: SnapResult | None,
        shift: bool,
        control: bool,
    ) -> None:
        self.tool_controls.hover(
            snap_result,
            shift=shift,
            control=control,
        )

    def _update_availability(self) -> None:
        messages: list[str] = []
        if pg is None:
            messages.append("pyqtgraph not installed: design window is disabled.")
        else:
            messages.append(
                "Load a GDS for registered navigation. Use Alignment to capture chip points, then use Move in the design view."
            )
        self._availability_label.setText(" ".join(messages))

    def _update_enabled_state(self) -> None:
        state = self._enabled_state()
        self.document_controls.apply_enablement(state)
        self._snap_checkbox.setEnabled(state.can_use_document_controls)
        self.registration_controls.apply_enablement(state)
        self.route_controls.apply_enablement(state)
        self.route_run_controls.apply_enablement(state)
        self.tool_controls.apply_enablement(state)

    def _enabled_state(self) -> DesignNavigatorEnablement:
        route = self.route_controls.route
        return DesignNavigatorEnablement(
            has_document=self.document_controls.document is not None,
            has_route=route is not None,
            route_saved=bool(route is not None and route.path is not None),
            route_has_points=bool(route is not None and route.points),
            has_route_selection=bool(
                route is not None
                and 0 <= self.route_controls.selected_index < len(route.points)
            ),
            has_current_design_position=self._current_design_position is not None,
            route_running=self.route_run_controls.running,
            design_registration_active=self.registration_controls.active,
            route_control=self.route_run_controls.presentation,
            design_load_pending=self.document_controls.load_pending,
        )

    def cancel_active_tool(self) -> None:
        self.tool_controls.cancel()

    def append_alignment_point(self, x_value: float, y_value: float) -> None:
        self.tool_controls.append_alignment_point(x_value, y_value)

    def accept_alignment_draft(self) -> None:
        self.tool_controls.accept_alignment_draft()

    def apply_route_pick(
        self,
        mode: str,
        x_value: float,
        y_value: float,
        shift: bool = False,
        control: bool = False,
    ) -> None:
        self.tool_controls.apply_route_pick(
            mode,
            x_value,
            y_value,
            shift=shift,
            control=control,
        )

    def _replace_tool_context(self, *, route_changed: bool = False) -> None:
        document = self.document_controls.document
        self.tool_controls.replace_context(
            document_token=id(document) if document is not None else None,
            route=self.route_controls.route,
            route_changed=route_changed,
            edit_safe=not self.route_run_controls.running,
        )

    def _sync_route_selection(self) -> None:
        selection = self.tool_controls.selection
        entities = self.tool_controls.selectable_entities
        route_rows = sorted(
            entity.route_index
            for entity in entities
            if entity.id in selection.ids
            and entity.owner is EntityOwner.ROUTE
            and entity.route_index is not None
        )
        self.route_controls.set_selection(
            route_rows,
            selected_route_ids=frozenset(
                entity_id
                for entity_id in selection.ids
                if entity_id.startswith("route:")
            ),
        )

    def _on_route_selection_requested(
        self,
        entity_ids: object,
        mode: str,
    ) -> None:
        self.selection_requested.emit(entity_ids, mode)
        self._update_enabled_state()
        self.tool_controls.refresh_array_preview()

    def _on_route_offsets_changed(
        self,
        needle_1_dx: float,
        needle_1_dy: float,
        needle_2_dx: float,
        needle_2_dy: float,
    ) -> None:
        self.route_offsets_changed.emit(
            needle_1_dx,
            needle_1_dy,
            needle_2_dx,
            needle_2_dy,
        )
        self.tool_controls.refresh_array_preview()

    def _on_markup_visibility_changed(self, visible: bool) -> None:
        self.markup_visibility_changed.emit(visible)
        self._update_enabled_state()

    def set_snap_enabled(self, enabled: bool) -> None:
        """Update the visible snap toggle state without re-emitting it."""

        self._snap_enabled = bool(enabled)
        self._snap_checkbox.blockSignals(True)
        self._snap_checkbox.setChecked(self._snap_enabled)
        self._snap_checkbox.blockSignals(False)
        self.set_hover_snap(None)

    def _on_snap_checkbox_toggled(self, checked: bool) -> None:
        self._snap_enabled = bool(checked)
        self.set_hover_snap(None)
        self.snap_enabled_changed.emit(self._snap_enabled)

    def _emit_route_measurement_jump_to_selected(self) -> None:
        selected_index = self.route_controls.selected_index
        if selected_index < 0:
            return
        self.route_measurement_jump_requested.emit(selected_index + 1)

    def _emit_route_measurement_measure_selected(self) -> None:
        if self.route_run_controls.waiting:
            self.route_measurement_confirmation_requested.emit("measure")
            return
        selected_index = self.route_controls.selected_index
        if selected_index < 0:
            return
        self.route_measurement_measure_requested.emit(selected_index + 1)

    def _emit_route_measurement_save_shift_selected(self) -> None:
        selected_index = self.route_controls.selected_index
        if selected_index < 0:
            return
        self.route_measurement_save_shift_requested.emit(selected_index + 1)

    def _emit_route_measurement_move_to_selected(self) -> None:
        selected_index = self.route_controls.selected_index
        if selected_index < 0:
            return
        self.route_measurement_move_requested.emit(selected_index + 1)


__all__ = ["DesignNavigatorPanel"]
