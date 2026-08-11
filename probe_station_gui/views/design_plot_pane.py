"""Pyqtgraph design plot pane for the design navigator."""

from __future__ import annotations

import logging
import math
from time import perf_counter

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from probe_station_gui.design.focus_candidate import FocusCandidate
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import DesignDocument, MeasurementTarget, Point2D, SnapResult
from probe_station_gui.design.klayout_types import RenderFailure
from probe_station_gui.design.plot_interaction import (
    ClickEffect,
    HoverEffect,
    InteractionTransition,
    PlotAction,
    PlotInteraction,
    PointerModifiers,
    ScheduleMoveSuppressionClear,
    SelectionClick,
    SelectionRequest,
    SnapClickIntent,
    SnapHoverIntent,
)
from probe_station_gui.design.plot_presentation import PlotPresentation
from probe_station_gui.design.selection_model import SelectionModel
from probe_station_gui.design.snap_coordinator import (
    ClickPublication,
    HoverPublication,
    SnapCoordinator,
    SnapNotice,
    SnapTransition,
    SnapWorkerEvent,
)
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.views.design_klayout_raster import KLayoutRasterController, KLayoutRasterItem
from probe_station_gui.views.design_plot_rendering import DesignPlotRenderer
from probe_station_gui.views.design_plot_viewport import DesignPlotViewport
from probe_station_gui.views.design_snap_runtime import DesignSnapRuntime

try:  # pragma: no cover - optional runtime dependency
    import pyqtgraph as pg
except ImportError:  # pragma: no cover - optional runtime dependency
    pg = None


logger = logging.getLogger(__name__)


class _DesignPlotPane(QWidget):
    """Public Qt facade for Design plot presentation and input."""

    calibration_point_selected = Signal(int, float, float)
    move_requested = Signal(float, float)
    route_point_requested = Signal(float, float)
    point_requested = Signal(float, float)
    alignment_point_requested = Signal(float, float)
    guide_requested = Signal(object, object)
    entity_selection_requested = Signal(object, str)
    route_pick_requested = Signal(str, float, float, bool, bool)
    hover_snap_changed = Signal(object)
    tool_hover_snap_changed = Signal(object, bool, bool)
    selected_focus_point_changed = Signal(object)

    SNAP_RADIUS_PX = 14.0

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._presentation = PlotPresentation()
        self._plot_interaction = PlotInteraction()
        self._pending_hover_scene_pos = None
        self._shutdown = False
        self._render_error_visible = False
        self._snap_failure_visible = False
        self._plot = None
        self._status_label: QLabel | None = None
        self._viewport: DesignPlotViewport | None = None
        self._renderer: DesignPlotRenderer | None = None
        self._raster_item: KLayoutRasterItem | None = None
        self._raster_controller: KLayoutRasterController | None = None
        self._snap_coordinator = SnapCoordinator(screen_distance=_euclidean_distance)
        self._snap_runtime = DesignSnapRuntime(self)
        self._snap_runtime.event_ready.connect(self._on_snap_worker_event)
        self._snap_runtime.geometry_ready.connect(self._on_snap_geometry_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        if pg is None:
            status_label = QLabel("pyqtgraph is not installed.", self)
            status_label.setAlignment(Qt.AlignCenter)
            status_label.setMinimumHeight(320)
            status_label.setStyleSheet(
                "QLabel { border: 1px dashed palette(mid); color: palette(mid); }"
            )
            layout.addWidget(status_label, 1)
            return

        self._status_label = QLabel("No design loaded.", self)
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setMinimumHeight(320)
        self._status_label.setStyleSheet(
            "QLabel { border: 1px dashed palette(mid); color: palette(mid); }"
        )
        self._plot = pg.PlotWidget(parent=self)
        self._viewport = DesignPlotViewport(self._plot)
        self._viewport.configure_plot()
        view_box = self._viewport.view_box

        self._raster_item = KLayoutRasterItem()
        self._plot.addItem(self._raster_item)
        self._raster_controller = KLayoutRasterController(
            view_box,
            self._raster_item,
            device_pixel_ratio=self._plot.devicePixelRatioF,
            parent=self,
        )
        self._raster_controller.failed.connect(self._on_klayout_render_failed)
        self._raster_controller.succeeded.connect(self._on_klayout_render_succeeded)
        self._renderer = DesignPlotRenderer(self._plot, self._viewport, parent=self)
        self._snap_coordinator = SnapCoordinator(
            screen_distance=self._viewport.screen_distance
        )
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(16)
        self._hover_timer.timeout.connect(self._flush_hover_snap)
        self._plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)
        self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self._plot.scene().installEventFilter(self)
        layout.addWidget(self._status_label, 1)
        layout.addWidget(self._plot, 1)
        self._plot.hide()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._renderer is not None:
            self._renderer.shown()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._renderer is not None:
            self._renderer.shown()
        if self._viewport is not None:
            self._viewport.resize()

    def set_document(self, document: DesignDocument | None) -> None:
        same_document = document is self._presentation.document
        if not same_document:
            self._apply_snap_transition(self._snap_coordinator.reset_document())
        change = self._presentation.set_document(document)
        self._apply_interaction_transition(
            self._plot_interaction.set_context(
                document_present=document is not None,
                preview_active=self._presentation.preview_active,
                document_generation=self._snap_coordinator.document_generation,
            )
        )
        if document is None:
            self._detach_file_backed_document(timeout_s=0.0)
            if self._viewport is not None:
                self._viewport.clear()
            self.set_status_message("No design loaded.")
        elif not same_document:
            self.set_status_message("")
            if not change.same_content and self._viewport is not None:
                self._viewport.apply_presentation(
                    self._presentation, recompute=True, focus=True
                )
            if document.file_backed:
                self._configure_file_backed_document(document)
            else:
                self._detach_file_backed_document(timeout_s=0.0)
        if self._renderer is not None:
            self._renderer.render(change)
        if document is not None and not same_document and not document.file_backed:
            self._start_snap_geometry_build(document)

    def _configure_file_backed_document(self, document: DesignDocument) -> None:
        controller = self._raster_controller
        if controller is None:
            return
        controller.set_document(document)
        config = controller.config
        if config is None:
            self._apply_snap_transition(self._snap_coordinator.configure(None))
            return
        self._apply_snap_transition(self._snap_coordinator.configure(config))

    def _detach_file_backed_document(self, *, timeout_s: float) -> None:
        del timeout_s
        self._apply_snap_transition(self._snap_coordinator.configure(None))
        if self._raster_controller is not None:
            self._raster_controller.set_document(None)

    def _on_klayout_render_failed(self, failure: RenderFailure) -> None:
        logger.warning("KLayout design render failed: %s", failure.message)
        self._render_error_visible = True
        self.set_status_message("Design rendering failed.")

    def _on_klayout_render_succeeded(self) -> None:
        if not self._render_error_visible:
            return
        self._render_error_visible = False
        if not self._snap_failure_visible:
            self.set_status_message("")

    def _on_snap_worker_event(self, event: SnapWorkerEvent) -> None:
        self._apply_snap_transition(self._snap_coordinator.worker_event(event))

    def _apply_snap_transition(self, transition: SnapTransition) -> None:
        current = transition
        while current.commands or current.publications or current.notices:
            for notice in current.notices:
                self._show_snap_notice(notice)
            self._snap_runtime.apply(current.commands)
            click_applied = False
            for publication in current.publications:
                if isinstance(publication, HoverPublication):
                    self._apply_hover_publication(publication)
                elif isinstance(publication, ClickPublication):
                    self._apply_click_publication(publication)
                    click_applied = True
            if not click_applied:
                break
            current = self._snap_coordinator.continue_ready_clicks()

    def _show_snap_notice(self, notice: SnapNotice) -> None:
        logger.warning("KLayout design snap failed: %s", notice.message)
        self._snap_failure_visible = True
        self.set_status_message("Snap failed. Try again.")
        QTimer.singleShot(1500, self._clear_snap_failure_status)

    def _apply_hover_publication(self, publication: HoverPublication) -> None:
        self._apply_interaction_transition(
            DesignPlotViewport.complete_hover_publication(
                self._plot_interaction, publication
            )
        )
        if (
            publication.result is not None
            and publication.raw_point is not None
            and publication.elapsed_ms is not None
            and self._viewport is not None
        ):
            self._viewport.log_hover_snap(
                publication.raw_point,
                publication.result,
                publication.elapsed_ms,
                log=logger,
            )

    def _apply_click_publication(self, publication: ClickPublication) -> None:
        hover = DesignPlotViewport.begin_click_publication(
            self._plot_interaction, publication
        )
        self._apply_interaction_transition(hover)
        DesignPlotViewport.log_click_snap(publication, log=logger)
        click = DesignPlotViewport.complete_click_publication(
            self._plot_interaction, publication
        )
        self._apply_interaction_transition(click)

    def _apply_interaction_transition(self, transition: InteractionTransition) -> None:
        if transition.invalidate_transient_snaps:
            self._apply_snap_transition(
                self._snap_coordinator.set_interaction_generation(transition.generation)
            )
        for effect in transition.effects:
            visual = (
                self._renderer is not None
                and self._renderer.apply_interaction_effect(effect, self._presentation)
            )
            if isinstance(effect, HoverEffect):
                self._publish_hover_snap(effect.result, shift=effect.shift, control=effect.control)
            elif isinstance(effect, ClickEffect):
                self._emit_click_effect(effect)
            elif isinstance(effect, SelectionClick):
                self.selected_focus_point_changed.emit(effect.point)
                self._emit_click_selection(effect.point, effect.modifiers)
            elif isinstance(effect, SelectionRequest):
                self.entity_selection_requested.emit(set(effect.entity_ids), effect.mode)
            elif isinstance(effect, ScheduleMoveSuppressionClear):
                QTimer.singleShot(
                    0,
                    lambda generation=effect.generation: self._plot_interaction.clear_move_suppression(generation),
                )
            elif isinstance(effect, SnapClickIntent):
                self._dispatch_snap_click(effect)
            elif visual:
                continue

    def _emit_click_effect(self, effect: ClickEffect) -> None:
        x_value, y_value = effect.point
        coordinate_signals = {
            PlotAction.ROUTE_POINT: self.route_point_requested,
            PlotAction.POINT: self.point_requested,
            PlotAction.ALIGNMENT_POINT: self.alignment_point_requested,
            PlotAction.MOVE: self.move_requested,
        }
        signal = coordinate_signals.get(effect.action)
        if signal is not None:
            signal.emit(x_value, y_value)
        elif effect.action is PlotAction.ROUTE_PICK and effect.payload:
            self.route_pick_requested.emit(
                str(effect.payload[0]), x_value, y_value, effect.shift, effect.control
            )
        elif effect.action is PlotAction.GUIDE_POINT and len(effect.points) == 2:
            self.guide_requested.emit(effect.points[0], effect.points[1])
        elif effect.action is PlotAction.CALIBRATION and effect.payload:
            self.calibration_point_selected.emit(int(effect.payload[0]), x_value, y_value)

    def _clear_snap_failure_status(self) -> None:
        if not self._snap_failure_visible:
            return
        self._snap_failure_visible = False
        if not self._render_error_visible:
            self.set_status_message("")

    def set_status_message(self, message: str) -> None:
        if self._status_label is None or self._viewport is None:
            return
        self._viewport.set_status_message(self._status_label, self, message)

    def set_targets(self, targets: list[MeasurementTarget], *, selected_target_id: str | None) -> None:
        self._apply_presentation(
            self._presentation.set_targets, targets, selected_target_id=selected_target_id
        )

    def set_probe_route(self, route: MeasurementRoute | None, *, selected_route_point_index: int) -> None:
        if self._renderer is None:
            self._presentation.set_probe_route(
                route, selected_route_point_index=selected_route_point_index
            )
            return
        self._renderer.apply_navigation(
            self._presentation,
            self._presentation.set_probe_route,
            route,
            selected_route_point_index=selected_route_point_index,
        )

    def set_probe_route_preview(self, preview: object) -> None:
        self._apply_presentation(self._presentation.set_probe_route_preview, preview)

    def set_tool_measure_points(self, points: object) -> None:
        self._apply_presentation(self._presentation.set_tool_measure_points, points)

    def set_tool_measure_segments(self, segments: object) -> None:
        self._apply_presentation(self._presentation.set_tool_measure_segments, segments)

    def set_document_preview(self, document: DesignDocument) -> None:
        view_range = self._viewport.capture_view_range() if self._viewport is not None else None
        self._presentation.start_preview(view_range)
        self.set_document(document)
        if self._viewport is not None:
            self._viewport.apply_presentation(self._presentation, recompute=True, focus=True)
        if self._renderer is not None:
            self._renderer.set_preview_visibility(False)
        self.set_status_message("")

    def finish_document_preview(self, document: DesignDocument | None) -> None:
        restore_view = self._presentation.end_preview(document)
        self.set_document(document)
        if self._viewport is not None:
            self._viewport.apply_presentation(self._presentation, recompute=True)
            self._viewport.restore_view_range(restore_view)
        self._presentation.clear_preview_history()
        if self._renderer is not None:
            self._renderer.set_preview_visibility(True)
            self._renderer.render(self._presentation.refresh_overlays())
        if document is not None:
            self.set_status_message("")

    @property
    def active_design_tool(self) -> str:
        return self._plot_interaction.active_tool

    @property
    def guide_anchor(self) -> Point2D | None:
        return self._plot_interaction.guide_anchor

    def set_active_design_tool(self, tool: str) -> None:
        normalized = str(tool).strip().lower()
        self._apply_interaction_transition(self._plot_interaction.set_tool(normalized))
        if self._viewport is not None:
            self._viewport.set_tool_cursor(normalized)
        self._apply_presentation(self._presentation.refresh_overlays)

    def set_markup(self, markup: MarkupDocument | None) -> None:
        change = self._presentation.set_markup(markup)
        if self._viewport is not None:
            self._viewport.apply_presentation(self._presentation, recompute=True)
        if change.markup_changed:
            self._apply_snap_transition(
                self._snap_coordinator.set_markup_candidates(
                    change.markup_snap_candidates
                )
            )
        if self._renderer is not None:
            self._renderer.render(change)

    def set_selectable_entities(self, entities: object) -> None:
        self._apply_presentation(self._presentation.set_selectable_entities, entities)

    def set_selection(self, selection: SelectionModel) -> None:
        self._apply_presentation(self._presentation.set_selection, selection)

    def set_mixed_array_preview(self, points: object, segments: object) -> None:
        self._apply_presentation(self._presentation.set_mixed_array_preview, points, segments)

    def cancel_active_interaction(self) -> None:
        self._apply_interaction_transition(self._plot_interaction.cancel())
        self._apply_presentation(self._presentation.refresh_overlays)

    def set_alignment_draft_points(self, points: object) -> None:
        self._apply_presentation(self._presentation.set_alignment_draft_points, points)

    def set_current_design_position(
        self, point: Point2D | None, *, fov_design_size: Point2D | None = None
    ) -> None:
        self._apply_presentation(
            self._presentation.set_current_design_position, point, fov_design_size=fov_design_size
        )

    def set_registration_marks(
        self, source_design_marks: list[Point2D | None], check_design_marks: list[Point2D]
    ) -> None:
        self._apply_presentation(
            self._presentation.set_registration_marks, source_design_marks, check_design_marks
        )

    def set_focus_candidate(self, candidate: FocusCandidate | None) -> None:
        self._apply_presentation(self._presentation.set_focus_candidate, candidate)

    def set_selected_focus_point(self, point: Point2D | None) -> None:
        self._apply_presentation(self._presentation.set_selected_focus_point, point)
        self.selected_focus_point_changed.emit(self._presentation.selected_focus_point)

    @property
    def selected_focus_point(self) -> Point2D | None:
        return self._presentation.selected_focus_point

    def set_navigation_enabled(self, enabled: bool) -> None:
        """Enable click-to-move on the layout plot once registration is valid."""

        self._apply_interaction_transition(self._plot_interaction.set_navigation_enabled(enabled))

    def set_route_edit_enabled(self, enabled: bool) -> None:
        """Enable route point placement by left-clicking the layout plot."""

        self._apply_interaction_transition(self._plot_interaction.set_route_edit_enabled(enabled))

    def set_route_pick_mode(self, mode: object) -> None:
        """Use the next left click as a route array helper point."""

        self._apply_interaction_transition(
            self._plot_interaction.set_route_pick_mode(mode)
        )
        if self._plot is not None:
            self._plot.setCursor(Qt.CrossCursor if mode else Qt.ArrowCursor)

    def set_snap_enabled(self, enabled: bool) -> None:
        """Enable or disable geometry snapping for design clicks and hover."""

        self._apply_snap_transition(self._snap_coordinator.set_enabled(enabled))

    def focus_gds_bounds(self) -> None:
        if self._viewport is not None:
            self._viewport.focus_gds_bounds()

    def _start_snap_geometry_build(self, document: DesignDocument) -> None:
        if document.file_backed or document.has_snap_geometry():
            return
        self._snap_runtime.build_geometry(self._snap_coordinator.document_generation, document)

    def _on_snap_geometry_ready(
        self, generation: int, document: object, geometry: object, result: object
    ) -> None:
        if not self._snap_coordinator.geometry_is_current(generation):
            return
        install = self._presentation.install_snap_geometry(document, geometry, result)
        if install.failure is not None:
            logger.warning("Design snap geometry build failed: %s", install.failure)
            return
        if not install.installed:
            return
        if self._pending_hover_scene_pos is not None and not self._hover_timer.isActive():
            self._hover_timer.start()

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        if (
            self._plot is None
            or self._viewport is None
            or watched is not self._plot.scene()
            or self._presentation.preview_active
            or self._presentation.document is None
        ):
            return super().eventFilter(watched, event)
        dispatch = self._viewport.dispatch_scene_event(
            event,
            interaction=self._plot_interaction,
            presentation=self._presentation,
            drag_threshold=QApplication.startDragDistance(),
            tolerance=self._snap_distance(),
        )
        if dispatch.transition is not None:
            self._apply_interaction_transition(dispatch.transition)
        if dispatch.handled:
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _on_mouse_clicked(self, event) -> None:  # pragma: no cover - UI interaction
        if self._viewport is None:
            return
        transition = self._viewport.click_transition(
            event, self._plot_interaction, self._presentation
        )
        if transition is not None:
            self._apply_interaction_transition(transition)

    def _dispatch_snap_click(self, intent: SnapClickIntent) -> None:
        document = self._presentation.document
        if bool(getattr(document, "file_backed", False)):
            threshold = self._snap_distance()
            self._apply_snap_transition(
                self._snap_coordinator.submit_click(
                    intent,
                    radius=0.0 if threshold is None else threshold,
                )
            )
            return
        started = perf_counter()
        snap_result = self._resolve_snap_result(intent.raw_point)
        elapsed_ms = (perf_counter() - started) * 1000.0
        self._apply_click_publication(ClickPublication(intent, snap_result, elapsed_ms))

    def _emit_click_selection(self, point: Point2D, modifiers: PointerModifiers) -> None:
        matched, mode = DesignPlotViewport.selection_request(
            self._presentation, point, modifiers, tolerance=self._snap_distance()
        )
        self.entity_selection_requested.emit(matched, mode)

    def _on_mouse_moved(self, position) -> None:  # pragma: no cover - UI interaction
        self._pending_hover_scene_pos = position
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    def _flush_hover_snap(self) -> None:  # pragma: no cover - UI interaction
        if (
            self._viewport is None or self._presentation.document is None
            or self._presentation.preview_active
        ):
            self._apply_interaction_transition(self._plot_interaction.hover(None))
            return
        position = self._pending_hover_scene_pos
        self._pending_hover_scene_pos = None
        if position is None:
            self._apply_interaction_transition(self._plot_interaction.hover(None))
            return
        modifiers = QApplication.keyboardModifiers()
        hover = self._viewport.hover_input(position, modifiers)
        if not hover.inside:
            self._apply_snap_transition(self._snap_coordinator.cancel_hover())
            self._apply_interaction_transition(self._plot_interaction.hover(None))
            return
        if hover.point is None:
            self._apply_interaction_transition(self._plot_interaction.hover(None))
            return
        if self._presentation.document.file_backed:
            self._submit_file_backed_hover(hover.point, modifiers=modifiers)
            return
        started = perf_counter()
        snap_result = self._resolve_snap_result(hover.point)
        elapsed_ms = (perf_counter() - started) * 1000.0
        self._apply_interaction_transition(
            self._plot_interaction.hover(
                snap_result, shift=hover.modifiers.shift, control=hover.modifiers.control
            )
        )
        self._viewport.log_hover_snap(hover.point, snap_result, elapsed_ms, log=logger)

    def _submit_file_backed_hover(
        self, raw_point: Point2D, *, modifiers: Qt.KeyboardModifiers = Qt.NoModifier
    ) -> None:
        threshold = self._snap_distance()
        if threshold is None:
            self._apply_interaction_transition(self._plot_interaction.hover(None))
            return
        pointer = self._viewport.pointer_modifiers(modifiers)
        self._apply_snap_transition(
            self._snap_coordinator.submit_hover(
                SnapHoverIntent(
                    raw_point, pointer.shift, pointer.control, self._plot_interaction.generation
                ),
                radius=threshold,
            )
        )

    def _resolve_snap_result(self, raw_point: Point2D) -> SnapResult:
        free_result = SnapResult(point=raw_point, mode="free", distance=0.0)
        if self._presentation.document is None or not self._snap_coordinator.enabled:
            return free_result
        threshold = self._snap_distance()
        return self._snap_coordinator.resolve_local(
            raw_point,
            radius=0.0 if threshold is None else threshold,
            geometry_result=(
                None
                if threshold is None
                else self._presentation.geometry_snap_result(
                    raw_point, max_distance=threshold
                )
            ),
        )

    def _snap_distance(self) -> float | None:
        if self._viewport is None:
            return None
        return self._viewport.snap_distance(self.SNAP_RADIUS_PX)

    def _publish_hover_snap(
        self, snap_result: SnapResult | None, *, shift: bool = False, control: bool = False
    ) -> None:
        self._apply_presentation(self._presentation.set_hover, snap_result)
        self.hover_snap_changed.emit(snap_result)
        self.tool_hover_snap_changed.emit(snap_result, bool(shift), bool(control))

    def _apply_presentation(self, mutation, *args, **kwargs):
        if self._renderer is None:
            return mutation(*args, **kwargs)
        return self._renderer.apply(mutation, *args, **kwargs)

    def shutdown(self) -> None:
        """Detach KLayout workers from the Qt creator thread."""

        if self._shutdown:
            return
        self._shutdown = True
        if hasattr(self, "_hover_timer"):
            self._hover_timer.stop()
        if self._renderer is not None:
            self._renderer.shutdown()
        self._apply_snap_transition(self._snap_coordinator.close())
        self._snap_runtime.close()
        if self._raster_controller is not None:
            self._raster_controller.shutdown()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.shutdown()
        super().closeEvent(event)


def _euclidean_distance(first: Point2D, second: Point2D) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


__all__ = ["_DesignPlotPane"]
