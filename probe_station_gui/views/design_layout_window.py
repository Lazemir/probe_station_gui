"""Top-level Design Window composition and Qt state adapter."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from probe_station_gui.design.focus_candidate import FocusCandidate
from probe_station_gui.design.layout_state import DesignLayoutState
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import (
    DesignDocument,
    MeasurementTarget,
    Point2D,
    SnapResult,
)
from probe_station_gui.design.selection_model import (
    MixedArrayPlan,
    MixedArrayRequest,
    MixedDeletePlan,
    SelectionModel,
)
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel
from probe_station_gui.views.design_plot_pane import _DesignPlotPane


class DesignLayoutWindow(QWidget):
    """Top-level design window combining the layout view and design controls."""

    calibration_point_selected = Signal(int, float, float)
    alignment_draft_accepted = Signal(object)
    alignment_draft_discarded = Signal()
    move_requested = Signal(float, float)
    route_point_requested = Signal(float, float)
    point_requested = Signal(float, float)
    guide_requested = Signal(object, object)
    selection_changed = Signal(object)
    delete_selection_requested = Signal()
    guide_undo_requested = Signal()
    guide_clear_requested = Signal()
    markup_visibility_changed = Signal(bool)
    mixed_array_requested = Signal(object)
    hover_snap_changed = Signal(object)
    visibility_changed = Signal(bool)
    find_focus_reference_requested = Signal()
    focus_reference_requested = Signal(float, float)
    reset_focus_reference_requested = Signal()
    registration_instance_selected = Signal(str)
    new_registration_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        _ = parent
        super().__init__(None)
        self._layout_state = DesignLayoutState()
        self._plot_document_attached = False
        self._plot_close_detached = False
        self.setWindowTitle("Design Window")
        self.setWindowFlag(Qt.Window, True)
        self.resize(1480, 920)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        self._main_view = _DesignPlotPane(parent=self)
        self.navigator_panel = DesignNavigatorPanel(self)
        tool_toolbar = self.navigator_panel.detach_tool_toolbar()
        tool_toolbar.setObjectName("DesignToolToolbar")
        tool_toolbar.setMinimumHeight(52)
        tool_options = self.navigator_panel.detach_tool_options_panel()
        tool_options.setMinimumWidth(300)
        tool_options.setMaximumWidth(360)
        self.navigator_panel.setMinimumWidth(400)
        navigator_scroll = QScrollArea(self)
        navigator_scroll.setWidgetResizable(True)
        navigator_scroll.setFrameShape(QScrollArea.NoFrame)
        navigator_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        navigator_scroll.setWidget(self.navigator_panel)
        navigator_scroll.setMinimumWidth(430)
        self._main_view.calibration_point_selected.connect(
            self.calibration_point_selected.emit
        )
        self._main_view.move_requested.connect(self.move_requested.emit)
        self._main_view.route_point_requested.connect(self.route_point_requested.emit)
        self._main_view.point_requested.connect(self.point_requested.emit)
        self._main_view.alignment_point_requested.connect(
            self.navigator_panel.append_alignment_point
        )
        self._main_view.guide_requested.connect(self.guide_requested.emit)
        self._main_view.entity_selection_requested.connect(
            self._apply_selection_request
        )
        self._main_view.route_pick_requested.connect(
            self.navigator_panel.apply_route_pick
        )
        self._main_view.tool_hover_snap_changed.connect(
            self.navigator_panel.set_tool_hover_snap
        )
        self._main_view.hover_snap_changed.connect(self.hover_snap_changed.emit)
        self._main_view.selected_focus_point_changed.connect(
            lambda point: self.navigator_panel.set_focus_selection_available(
                point is not None
            )
        )
        self.navigator_panel.route_pick_mode_changed.connect(
            self._main_view.set_route_pick_mode
        )
        self.navigator_panel.route_preview_changed.connect(
            self._main_view.set_probe_route_preview
        )
        self.navigator_panel.tool_measure_preview_changed.connect(
            self._main_view.set_tool_measure_points
        )
        self.navigator_panel.tool_measurements_changed.connect(
            self._main_view.set_tool_measure_segments
        )
        self.navigator_panel.active_design_tool_changed.connect(
            self._main_view.set_active_design_tool
        )
        self.navigator_panel.alignment_draft_changed.connect(
            self._main_view.set_alignment_draft_points
        )
        self.navigator_panel.alignment_draft_accepted.connect(
            self.alignment_draft_accepted.emit
        )
        self.navigator_panel.alignment_draft_discarded.connect(
            self.alignment_draft_discarded.emit
        )
        self.navigator_panel.selection_requested.connect(
            self._apply_selection_request
        )
        self.navigator_panel.delete_selection_requested.connect(
            self.delete_selection_requested.emit
        )
        self.navigator_panel.guide_undo_requested.connect(
            self.guide_undo_requested.emit
        )
        self.navigator_panel.guide_clear_requested.connect(
            self.guide_clear_requested.emit
        )
        self.navigator_panel.markup_visibility_changed.connect(
            self.markup_visibility_changed.emit
        )
        self.navigator_panel.mixed_array_requested.connect(
            self.mixed_array_requested.emit
        )
        self.navigator_panel.mixed_array_preview_changed.connect(
            self._main_view.set_mixed_array_preview
        )
        self.navigator_panel.find_focus_reference_requested.connect(
            self.find_focus_reference_requested.emit
        )
        self.navigator_panel.use_selected_focus_requested.connect(
            self._emit_selected_focus_reference
        )
        self.navigator_panel.reset_focus_reference_requested.connect(
            self.reset_focus_reference_requested.emit
        )
        self.navigator_panel.registration_instance_selected.connect(
            self.registration_instance_selected.emit
        )
        self.navigator_panel.new_registration_requested.connect(
            self.new_registration_requested.emit
        )
        self._escape_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._escape_shortcut.setContext(Qt.WindowShortcut)
        self._escape_shortcut.activated.connect(self._cancel_active_interaction)
        self._home_shortcut = QShortcut(QKeySequence("Home"), self)
        self._home_shortcut.setContext(Qt.WindowShortcut)
        self._home_shortcut.activated.connect(self._main_view.focus_gds_bounds)
        self._accept_alignment_shortcut = QShortcut(QKeySequence("Return"), self)
        self._accept_alignment_shortcut.setContext(Qt.WindowShortcut)
        self._accept_alignment_shortcut.activated.connect(
            self.navigator_panel.accept_alignment_draft
        )
        self._accept_alignment_enter_shortcut = QShortcut(QKeySequence("Enter"), self)
        self._accept_alignment_enter_shortcut.setContext(Qt.WindowShortcut)
        self._accept_alignment_enter_shortcut.activated.connect(
            self.navigator_panel.accept_alignment_draft
        )
        self._main_view.set_active_design_tool("select")

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.addWidget(tool_options, 0)
        content_layout.addWidget(self._main_view, 1)
        content_layout.addWidget(navigator_scroll, 0)
        root_layout.addWidget(tool_toolbar, 0)
        root_layout.addLayout(content_layout, 1)

    @property
    def selection(self) -> SelectionModel:
        return self._layout_state.selection

    def set_document(self, document: DesignDocument | None) -> None:
        self._layout_state = self._layout_state.with_document(document)
        if not self._plot_close_detached:
            self._main_view.set_document(document)
            self._plot_document_attached = document is not None
        self.navigator_panel.set_document(document)

    def set_design_load_pending(self, pending: bool) -> None:
        self.navigator_panel.set_design_load_pending(pending)
        self._escape_shortcut.setEnabled(not bool(pending))

    def set_document_preview(self, document: DesignDocument) -> None:
        if not self._plot_close_detached:
            self._main_view.set_document_preview(document)
            self._plot_document_attached = True
        self.navigator_panel.set_status_message("Loading Markup...")

    def finish_document_preview(
        self,
        document: DesignDocument | None,
    ) -> None:
        if self._plot_close_detached:
            self._main_view.finish_document_preview(None)
            self._plot_document_attached = False
        else:
            self._main_view.finish_document_preview(document)
            self._plot_document_attached = document is not None
        self.navigator_panel.set_status_message("")

    def set_markup(self, markup: MarkupDocument | None) -> None:
        self._layout_state = self._layout_state.with_markup(markup)
        self._main_view.set_markup(markup)
        self.navigator_panel.set_markup_state(
            visible=bool(markup is not None and markup.visible),
            guide_count=len(markup.guides) if markup is not None else 0,
        )
        self._render_layout_selection()

    def set_selection(self, selection: SelectionModel) -> None:
        self._layout_state = self._layout_state.with_selection(selection)
        self._render_layout_selection()

    def plan_mixed_delete(self, *, edit_safe: bool) -> MixedDeletePlan:
        return self._layout_state.plan_mixed_delete(edit_safe=edit_safe)

    def plan_mixed_array(
        self,
        request: MixedArrayRequest,
        *,
        edit_safe: bool,
    ) -> MixedArrayPlan:
        return self._layout_state.plan_mixed_array(
            request,
            edit_safe=edit_safe,
        )

    def set_guide_undo_available(self, available: bool) -> None:
        self.navigator_panel.set_guide_undo_available(available)

    def _render_layout_selection(self) -> None:
        entities = self._layout_state.selectable_entities
        selection = self._layout_state.selection
        self._main_view.set_selectable_entities(entities)
        self.navigator_panel.set_selectable_entities(entities)
        self._main_view.set_selection(selection)
        self.navigator_panel.set_selection(selection)

    def _apply_selection_request(self, entity_ids: object, mode: str) -> None:
        self._layout_state = self._layout_state.apply_selection(entity_ids, str(mode))
        self._render_layout_selection()
        self.selection_changed.emit(self.selection)

    def _cancel_active_interaction(self) -> None:
        self._main_view.cancel_active_interaction()
        self.navigator_panel.cancel_active_tool()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._main_view.set_document(None)
        self._plot_document_attached = False
        self._plot_close_detached = True
        super().closeEvent(event)

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._main_view.set_targets(targets, selected_target_id=selected_target_id)
        self.navigator_panel.set_targets(targets, selected_target_id=selected_target_id)

    def set_probe_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> None:
        self._layout_state = self._layout_state.with_route(
            route,
            selected_route_point_index=selected_route_point_index,
        )
        self._main_view.set_probe_route(
            route,
            selected_route_point_index=selected_route_point_index,
        )
        self.navigator_panel.set_route(
            route,
            selected_route_point_index=selected_route_point_index,
        )
        self._render_layout_selection()

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> None:
        self._main_view.set_registration_marks(source_design_marks, check_design_marks)
        self.navigator_panel.set_registration_marks(source_design_marks, check_design_marks)

    def set_alignment_capture_points(self, points: object) -> None:
        """Keep accepted D1..Dn visible while their stage points are captured."""

        self._main_view.set_alignment_draft_points(points)

    def set_navigation_enabled(self, enabled: bool) -> None:
        """Toggle click-to-move behavior in the design plot."""

        self._main_view.set_navigation_enabled(enabled)

    def set_route_edit_enabled(self, enabled: bool) -> None:
        """Toggle route point placement in the design plot."""

        self._main_view.set_route_edit_enabled(enabled)

    def set_snap_enabled(self, enabled: bool) -> None:
        """Toggle geometry snapping in the design plot and sidebar."""

        self._main_view.set_snap_enabled(enabled)
        self.navigator_panel.set_snap_enabled(enabled)

    def set_stage_registration_marks(self, source_stage_marks: list[Point2D | None]) -> None:
        self.navigator_panel.set_stage_registration_marks(source_stage_marks)

    def set_calibration_prompt(self, text: str) -> None:
        self.navigator_panel.set_calibration_prompt(text)

    def set_registration_status(self, text: str) -> None:
        self.navigator_panel.set_registration_status(text)

    def set_registration_instances(
        self,
        instances: object,
        *,
        selected_frame_id: str | None,
    ) -> None:
        self.navigator_panel.set_registration_instances(
            instances,
            selected_frame_id=selected_frame_id,
        )

    def set_focus_candidate(self, candidate: FocusCandidate | None) -> None:
        self._main_view.set_focus_candidate(candidate)

    def set_selected_focus_point(self, point: Point2D | None) -> None:
        self._main_view.set_selected_focus_point(point)

    def set_focus_reference_state(self, *, z_ready: bool, a_ready: bool) -> None:
        self.navigator_panel.set_focus_reference_state(
            z_ready=z_ready,
            a_ready=a_ready,
        )

    def _emit_selected_focus_reference(self) -> None:
        point = self._main_view.selected_focus_point
        if point is not None:
            self.focus_reference_requested.emit(point[0], point[1])

    def set_status_message(self, text: str) -> None:
        self._main_view.set_status_message(text)
        self.navigator_panel.set_status_message(text)

    def set_hover_snap(self, snap_result: SnapResult | None) -> None:
        self.navigator_panel.set_hover_snap(snap_result)

    def set_current_position(
        self,
        stage_xy: Point2D | None,
        design_xy: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        self.navigator_panel.set_current_position(
            stage_xy,
            design_xy,
            fov_design_size=fov_design_size,
        )

    def set_current_design_position(
        self,
        point: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        self._main_view.set_current_design_position(
            point,
            fov_design_size=fov_design_size,
        )

    def show_and_raise(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.setWindowState(self.windowState() | Qt.WindowActive)
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # type: ignore[override]
        document = self._layout_state.document
        if document is not None and not self._plot_document_attached:
            self._main_view.set_document(document)
            self._plot_document_attached = True
        self._plot_close_detached = False
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self.visibility_changed.emit(False)


__all__ = ["DesignLayoutWindow"]
