"""Navigation and pixel-metric adapter for the Design plot viewport."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from time import monotonic

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.navigation_bounds import (
    Box2D,
    GDS_FOCUS_PADDING_FRACTION,
    VIEW_RANGE_ABS_TOLERANCE,
    clamp_view_bounds,
    content_bounds,
    fit_bounds_to_aspect,
    navigation_frame,
    pad_bounds,
)
from probe_station_gui.design.model import Point2D
from probe_station_gui.design.plot_interaction import (
    InteractionTransition,
    MouseButton,
    PlotClick,
    PlotInteraction,
    PointerModifiers,
    SnapHoverIntent,
)
from probe_station_gui.design.plot_presentation import PlotPresentation, ViewRange
from probe_station_gui.design.snap_coordinator import (
    ClickPublication,
    HoverPublication,
)
from probe_station_gui.route.model import MeasurementRoute


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ViewportDispatch:
    handled: bool = False
    transition: InteractionTransition | None = None


@dataclass(frozen=True)
class ViewportHover:
    inside: bool
    point: Point2D | None
    modifiers: PointerModifiers


class DesignPlotViewport:
    """Keep viewport limits, cached content and coordinate conversions together."""

    HOVER_SNAP_LOG_INTERVAL_S = 1.0
    HOVER_SNAP_SLOW_MS = 8.0

    def __init__(self, plot) -> None:
        self._plot = plot
        self._document_bounds: Box2D | None = None
        self._content_bounds: Box2D | None = None
        self._frame: Box2D | None = None
        self._ignored_coordinate_count = 0
        self._last_reported_invalid_coordinate_count = 0
        self._last_hover_log_at = 0.0

    @property
    def frame(self) -> Box2D | None:
        return self._frame

    @property
    def content_bounds(self) -> Box2D | None:
        return self._content_bounds

    @property
    def ignored_coordinate_count(self) -> int:
        return self._ignored_coordinate_count

    @property
    def view_box(self):
        return self._view_box()

    def configure_plot(self) -> None:
        self._plot.setBackground("k")
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.setAspectLocked(True)
        self._plot.showGrid(x=False, y=False, alpha=0.0)
        self._view_box().setMouseEnabled(x=True, y=True)
        self._plot.plotItem.hideAxis("bottom")
        self._plot.plotItem.hideAxis("left")

    def set_status_message(self, status_label, owner, message: str) -> None:
        message = str(message or "")
        if message:
            status_label.setText(message)
            status_label.show()
            self._plot.hide()
            return
        status_label.hide()
        self._plot.show()
        layout = owner.layout()
        if layout is not None:
            layout.activate()
        self._plot.resize(owner.contentsRect().size())
        self._plot.plotItem.setGeometry(QRectF(self._plot.rect()))

    def set_tool_cursor(self, tool: str) -> None:
        cursor = (
            Qt.CrossCursor
            if tool in {"move", "point", "guide", "ruler", "array", "align"}
            else Qt.ArrowCursor
        )
        self._plot.setCursor(cursor)

    def clear(self) -> None:
        self._document_bounds = None
        self._content_bounds = None
        self._frame = None
        self._ignored_coordinate_count = 0
        self._last_reported_invalid_coordinate_count = 0
        self._view_box().setLimits(
            xMin=None,
            xMax=None,
            yMin=None,
            yMax=None,
            maxXRange=None,
            maxYRange=None,
        )

    def set_content(
        self,
        document_bounds: Box2D | None,
        route: MeasurementRoute | None,
        markup: MarkupDocument | None,
        *,
        recompute: bool = False,
        focus: bool = False,
    ) -> None:
        if document_bounds is None:
            self.clear()
            return
        self._document_bounds = tuple(float(value) for value in document_bounds)
        if recompute or self._content_bounds is None:
            self._content_bounds, self._ignored_coordinate_count = content_bounds(
                self._document_bounds,
                route,
                markup,
            )
        self._apply_cached_limits(focus=focus)

    def apply_presentation(
        self,
        presentation: PlotPresentation,
        *,
        recompute: bool = False,
        focus: bool = False,
    ) -> None:
        content = presentation.navigation_content()
        self.set_content(
            content.document_bounds,
            content.route,
            content.markup,
            recompute=recompute,
            focus=focus,
        )

    def resize(self) -> None:
        """Refit the cached content frame without rescanning route or markup."""

        if self._document_bounds is None or self._content_bounds is None:
            if self._document_bounds is None:
                self.clear()
            return
        self._apply_cached_limits(focus=False)

    def focus_gds_bounds(self) -> None:
        if self._document_bounds is None:
            return
        focused = fit_bounds_to_aspect(
            pad_bounds(self._document_bounds, GDS_FOCUS_PADDING_FRACTION),
            self.viewport_size(),
        )
        if self._frame is not None:
            focused = clamp_view_bounds(focused, self._frame)
        self.set_view_bounds(focused)

    def viewport_size(self) -> tuple[float, float]:
        rect = self._view_box().sceneBoundingRect()
        return (max(1.0, float(rect.width())), max(1.0, float(rect.height())))

    def current_view_bounds(self) -> Box2D | None:
        try:
            x_range, y_range = self._view_box().viewRange()[:2]
            values: Box2D = (
                float(x_range[0]),
                float(y_range[0]),
                float(x_range[1]),
                float(y_range[1]),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            return None
        return values if all(math.isfinite(value) for value in values) else None

    def set_view_bounds(self, bounds: Box2D) -> None:
        left, bottom, right, top = bounds
        self._view_box().setRange(
            xRange=(left, right),
            yRange=(bottom, top),
            padding=0.0,
        )

    def capture_view_range(self) -> ViewRange | None:
        try:
            x_range, y_range = self._view_box().viewRange()[:2]
            values: ViewRange = (
                (float(x_range[0]), float(x_range[1])),
                (float(y_range[0]), float(y_range[1])),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            return None
        return (
            values
            if all(math.isfinite(value) for axis in values for value in axis)
            else None
        )

    def restore_view_range(self, view_range: ViewRange | None) -> None:
        if view_range is None:
            return
        self._view_box().setRange(
            xRange=view_range[0],
            yRange=view_range[1],
            padding=0.0,
        )

    def scene_to_design(self, position) -> Point2D | None:
        try:
            point = self._view_box().mapSceneToView(position)
            values = (float(point.x()), float(point.y()))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
        return values if all(math.isfinite(value) for value in values) else None

    def scene_contains(self, position) -> bool:
        try:
            return bool(self._plot.sceneBoundingRect().contains(position))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    def hover_input(
        self,
        position,
        modifiers: Qt.KeyboardModifiers,
    ) -> ViewportHover:
        pointer = _pointer_modifiers(modifiers)
        if not self.scene_contains(position):
            return ViewportHover(False, None, pointer)
        return ViewportHover(True, self.scene_to_design(position), pointer)

    @staticmethod
    def complete_hover_publication(
        interaction: PlotInteraction,
        publication: HoverPublication,
    ) -> InteractionTransition:
        if publication.generation is None:
            return interaction.hover(
                publication.result,
                shift=publication.shift,
                control=publication.control,
            )
        return interaction.complete_hover(
            SnapHoverIntent(
                raw_point=publication.raw_point or (0.0, 0.0),
                shift=publication.shift,
                control=publication.control,
                generation=publication.generation,
            ),
            publication.result,
        )

    @staticmethod
    def begin_click_publication(
        interaction: PlotInteraction,
        publication: ClickPublication,
    ) -> InteractionTransition:
        intent = publication.intent
        return interaction.complete_hover(
            SnapHoverIntent(
                intent.raw_point,
                intent.shift,
                intent.control,
                intent.generation,
            ),
            publication.result,
        )

    @staticmethod
    def complete_click_publication(
        interaction: PlotInteraction,
        publication: ClickPublication,
    ) -> InteractionTransition:
        return interaction.complete_click(publication.intent, publication.result)

    @staticmethod
    def selection_request(
        presentation: PlotPresentation,
        point: Point2D,
        modifiers: PointerModifiers,
        *,
        tolerance: float | None,
    ) -> tuple[set[str], str]:
        entity = presentation.hit_entity(point, tolerance=tolerance)
        mode = (
            "invert"
            if modifiers.control
            else "add"
            if modifiers.shift
            else "replace"
        )
        return ({entity.id} if entity is not None else set(), mode)

    def plot_click(self, event) -> PlotClick | None:
        position_getter = getattr(event, "scenePos", None)
        if not callable(position_getter):
            return None
        position = position_getter()
        if not self.scene_contains(position):
            return None
        point = self.scene_to_design(position)
        if point is None:
            return None
        modifiers = getattr(event, "modifiers", lambda: Qt.NoModifier)()
        return PlotClick(
            button=_mouse_button(event.button()),
            point=point,
            modifiers=_pointer_modifiers(modifiers),
            double=_is_double_click_event(event),
        )

    def click_transition(
        self,
        event,
        interaction: PlotInteraction,
        presentation: PlotPresentation,
    ) -> InteractionTransition | None:
        if presentation.document is None or presentation.preview_active:
            return None
        click = self.plot_click(event)
        return None if click is None else interaction.click(click)

    def pointer_modifiers(
        self,
        modifiers: Qt.KeyboardModifiers,
    ) -> PointerModifiers:
        return _pointer_modifiers(modifiers)

    def dispatch_scene_event(
        self,
        event,
        *,
        interaction: PlotInteraction,
        presentation: PlotPresentation,
        drag_threshold: int,
        tolerance: float | None,
    ) -> ViewportDispatch:
        event_type = event.type()
        if interaction.active_tool == "move":
            transition = _move_transition(
                event,
                event_type=event_type,
                interaction=interaction,
                drag_threshold=drag_threshold,
            )
            return ViewportDispatch(False, transition)
        if interaction.active_tool != "select":
            return ViewportDispatch()
        if event_type == QEvent.GraphicsSceneMousePress:
            if event.button() != Qt.LeftButton:
                return ViewportDispatch()
            position = event.scenePos()
            if not self.scene_contains(position):
                return ViewportDispatch()
            point = self.scene_to_design(position)
            if point is None:
                return ViewportDispatch()
            hit = presentation.hit_entity(point, tolerance=tolerance)
            return ViewportDispatch(
                True,
                interaction.selection_press(
                    scene_point=(float(position.x()), float(position.y())),
                    design_point=point,
                    hit_entity_id=hit.id if hit is not None else None,
                    modifiers=_pointer_modifiers(event.modifiers()),
                ),
            )
        if event_type == QEvent.GraphicsSceneMouseMove:
            if not interaction.selection_active:
                return ViewportDispatch()
            position = event.scenePos()
            point = self.scene_to_design(position)
            if point is None:
                return ViewportDispatch()
            return ViewportDispatch(
                True,
                interaction.selection_motion(
                    scene_point=(float(position.x()), float(position.y())),
                    design_point=point,
                    drag_threshold=drag_threshold,
                ),
            )
        if (
            event_type == QEvent.GraphicsSceneMouseRelease
            and event.button() == Qt.LeftButton
            and interaction.selection_active
        ):
            point = self.scene_to_design(event.scenePos())
            if point is not None:
                return ViewportDispatch(
                    True,
                    interaction.selection_release(
                        point,
                        presentation.selectable_entities,
                    ),
                )
        return ViewportDispatch()

    def screen_distance(self, first: Point2D, second: Point2D) -> float:
        try:
            view_box = self._view_box()
            first_scene = view_box.mapViewToScene(QPointF(*first))
            second_scene = view_box.mapViewToScene(QPointF(*second))
            return math.hypot(
                second_scene.x() - first_scene.x(),
                second_scene.y() - first_scene.y(),
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return math.hypot(second[0] - first[0], second[1] - first[1])

    def data_units_per_screen_pixel(self) -> float | None:
        view_box = self._view_box()
        try:
            pixel_size = view_box.viewPixelSize()
        except Exception:
            pixel_size = None
        if (
            isinstance(pixel_size, tuple)
            and len(pixel_size) >= 2
            and all(
                math.isfinite(float(value)) and abs(float(value)) > 0.0
                for value in pixel_size[:2]
            )
        ):
            return max(abs(float(pixel_size[0])), abs(float(pixel_size[1])))
        view_range = view_box.viewRange()
        if not isinstance(view_range, list) or len(view_range) < 2:
            return None
        scene_rect = view_box.sceneBoundingRect()
        if scene_rect.width() <= 0.0 or scene_rect.height() <= 0.0:
            return None
        x_range, y_range = view_range[:2]
        if len(x_range) < 2 or len(y_range) < 2:
            return None
        x_units = abs(float(x_range[1]) - float(x_range[0])) / float(
            scene_rect.width()
        )
        y_units = abs(float(y_range[1]) - float(y_range[0])) / float(
            scene_rect.height()
        )
        return max(x_units, y_units)

    def visible_pixel_size(self) -> float | None:
        try:
            if not self._plot.isVisible():
                return None
        except (AttributeError, RuntimeError):
            return None
        rect = self._view_box().sceneBoundingRect()
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            return None
        pixel_size = self.data_units_per_screen_pixel()
        if pixel_size is None or not math.isfinite(pixel_size) or pixel_size <= 0.0:
            return None
        return pixel_size

    def snap_distance(self, radius_px: float) -> float | None:
        pixel_size = self.data_units_per_screen_pixel()
        if pixel_size is None or not math.isfinite(pixel_size) or pixel_size <= 0.0:
            return None
        return float(pixel_size * radius_px)

    def log_hover_snap(
        self,
        raw_point: Point2D,
        snap_result,
        elapsed_ms: float,
        *,
        log: logging.Logger,
    ) -> None:
        if not log.isEnabledFor(logging.DEBUG):
            return
        now = monotonic()
        if (
            elapsed_ms < self.HOVER_SNAP_SLOW_MS
            and (now - self._last_hover_log_at) < self.HOVER_SNAP_LOG_INTERVAL_S
        ):
            return
        log.debug(
            "DESIGN SNAP hover raw=(%.3f, %.3f) snapped=(%.3f, %.3f) "
            "mode=%s dist=%.4f elapsed_ms=%.2f",
            raw_point[0],
            raw_point[1],
            snap_result.point[0],
            snap_result.point[1],
            snap_result.mode,
            snap_result.distance,
            elapsed_ms,
        )
        self._last_hover_log_at = now

    @staticmethod
    def log_click_snap(publication: ClickPublication, *, log: logging.Logger) -> None:
        if publication.elapsed_ms is None:
            return
        intent = publication.intent
        log.debug(
            "DESIGN SNAP click raw=(%.3f, %.3f) snapped=(%.3f, %.3f) "
            "mode=%s dist=%.4f elapsed_ms=%.2f",
            intent.raw_point[0],
            intent.raw_point[1],
            publication.result.point[0],
            publication.result.point[1],
            publication.result.mode,
            publication.result.distance,
            publication.elapsed_ms,
        )

    def _apply_cached_limits(self, *, focus: bool) -> None:
        if self._content_bounds is None:
            return
        self._frame = navigation_frame(self._content_bounds, self.viewport_size())
        left, bottom, right, top = self._frame
        self._view_box().setLimits(
            xMin=left,
            xMax=right,
            yMin=bottom,
            yMax=top,
            maxXRange=right - left,
            maxYRange=top - bottom,
        )
        if (
            self._ignored_coordinate_count
            and self._ignored_coordinate_count
            != self._last_reported_invalid_coordinate_count
        ):
            logger.warning(
                "Ignored %d invalid design navigation coordinates.",
                self._ignored_coordinate_count,
            )
        self._last_reported_invalid_coordinate_count = self._ignored_coordinate_count
        if focus:
            self.focus_gds_bounds()
            return
        current = self.current_view_bounds()
        if current is None:
            return
        clamped = clamp_view_bounds(current, self._frame)
        if (
            current[0] < self._frame[0]
            or current[1] < self._frame[1]
            or current[2] > self._frame[2]
            or current[3] > self._frame[3]
        ) and clamped == self._frame:
            epsilon = VIEW_RANGE_ABS_TOLERANCE
            clamped = (
                clamped[0] + epsilon,
                clamped[1] + epsilon,
                clamped[2] - epsilon,
                clamped[3] - epsilon,
            )
        if any(
            abs(first - second) > VIEW_RANGE_ABS_TOLERANCE
            for first, second in zip(current, clamped, strict=True)
        ):
            self.set_view_bounds(clamped)

    def _view_box(self):
        return self._plot.getViewBox()


def _move_transition(
    event,
    *,
    event_type,
    interaction: PlotInteraction,
    drag_threshold: int,
) -> InteractionTransition | None:
    if (
        event_type == QEvent.GraphicsSceneMousePress
        and event.button() == Qt.LeftButton
    ):
        position = event.scenePos()
        return interaction.move_press((float(position.x()), float(position.y())))
    if event_type == QEvent.GraphicsSceneMouseMove and interaction.move_active:
        position = event.scenePos()
        return interaction.move_motion(
            (float(position.x()), float(position.y())),
            drag_threshold=drag_threshold,
        )
    if (
        event_type == QEvent.GraphicsSceneMouseRelease
        and event.button() == Qt.LeftButton
    ):
        return interaction.move_release()
    return None


def _pointer_modifiers(modifiers: Qt.KeyboardModifiers) -> PointerModifiers:
    return PointerModifiers(
        shift=bool(modifiers & Qt.ShiftModifier),
        control=bool(modifiers & Qt.ControlModifier),
        other=modifiers
        not in {
            Qt.NoModifier,
            Qt.ShiftModifier,
            Qt.ControlModifier,
            Qt.ShiftModifier | Qt.ControlModifier,
        },
    )


def _is_double_click_event(event) -> bool:
    double = getattr(event, "double", None)
    if not callable(double):
        return False
    try:
        return bool(double())
    except Exception:
        return False


def _mouse_button(button: Qt.MouseButton) -> MouseButton:
    if button == Qt.LeftButton:
        return MouseButton.LEFT
    if button == Qt.RightButton:
        return MouseButton.RIGHT
    return MouseButton.OTHER


__all__ = ["DesignPlotViewport", "ViewportDispatch"]
