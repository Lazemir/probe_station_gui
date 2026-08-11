"""Public controller for the microscope design minimap."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_render_worker import KLayoutRenderWorker
from probe_station_gui.design.model import DesignDocument, MeasurementTarget
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.views.microscope_minimap_background import (
    LegacyRenderer,
    MinimapBackground,
)
from probe_station_gui.views.microscope_minimap_rendering import (
    MinimapRendering,
    MinimapScene,
)


class MicroscopeMinimap(QObject):
    """Coordinate minimap snapshots, painting, interaction, and shutdown."""

    changed = Signal()
    clicked = Signal(float, float)

    _MINIMAP_CLICK_DELAY_PADDING_MS = 50

    def __init__(
        self,
        *,
        renderer: LegacyRenderer | None = None,
        worker_factory: Callable[[], KLayoutRenderWorker] = KLayoutRenderWorker,
        device_pixel_ratio: Callable[[], float] = lambda: 1.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._background = MinimapBackground(
            renderer=renderer,
            worker_factory=worker_factory,
            device_pixel_ratio=device_pixel_ratio,
            parent=self,
        )
        self._rendering = MinimapRendering()
        self._background.changed.connect(self.changed.emit)
        self._pending_click_point: QPoint | None = None
        self._pending_click_fallback_rect: QRect | None = None
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._emit_pending_click)

    def configure(
        self,
        document: DesignDocument | object | None,
        bounds: tuple[float, float, float, float],
        *,
        targets: tuple[MeasurementTarget, ...] | list[MeasurementTarget] = (),
        selected_target_id: str | None = None,
        probe_route: MeasurementRoute | None = None,
        selected_route_point_index: int = -1,
        selected_design_point: tuple[float, float] | None = None,
        current_design_position: tuple[float, float] | None = None,
        fov_design_size: tuple[float, float] | None = None,
        source_design_marks: tuple[tuple[float, float], ...]
        | list[tuple[float, float]] = (),
        check_design_marks: tuple[tuple[float, float], ...]
        | list[tuple[float, float]] = (),
    ) -> int:
        """Replace the immutable scene and invalidate obsolete rendering work."""

        valid_document = document if isinstance(document, DesignDocument) else None
        generation = self._background.configure(valid_document)
        changed = self._rendering.configure(
            MinimapScene(
                document=valid_document,
                bounds=tuple(float(value) for value in bounds),
                targets=tuple(targets),
                selected_target_id=selected_target_id,
                probe_route=probe_route,
                selected_route_point_index=selected_route_point_index,
                selected_design_point=selected_design_point,
                current_design_position=current_design_position,
                fov_design_size=fov_design_size,
                source_design_marks=tuple(source_design_marks),
                check_design_marks=tuple(check_design_marks),
            )
        )
        if changed:
            self.changed.emit()
        return generation

    def draw(self, painter: QPainter, display_rect: QRect) -> None:
        """Draw the current minimap using the latest accepted background."""

        size = self._rendering.content_size(display_rect)
        background = self._background.background_for_size(size)
        self._rendering.draw(painter, display_rect, background)

    def map_click(
        self, point: QPoint | QPointF, display_rect: QRect
    ) -> tuple[float, float] | None:
        """Map a point inside the minimap to design coordinates."""

        return self._rendering.map_click(point, display_rect)

    def contains(self, point: QPoint | QPointF, display_rect: QRect) -> bool:
        """Return whether a point is inside the current minimap."""

        return self._rendering.contains(point, display_rect)

    def queue_click(self, point: QPoint, display_rect: QRect) -> None:
        """Delay a click so a double click can supersede it."""

        self._pending_click_point = QPoint(point)
        self._pending_click_fallback_rect = QRect(display_rect)
        self._click_timer.start(self.click_delay_ms())

    def cancel_click(self) -> None:
        """Cancel the delayed single-click action."""

        self._click_timer.stop()
        self._pending_click_point = None
        self._pending_click_fallback_rect = None

    def shutdown(self) -> None:
        """Invalidate rendering and retire workers without blocking the GUI."""

        self.cancel_click()
        self._background.shutdown()

    def _emit_pending_click(self) -> None:
        point = self._pending_click_point
        display_rect = self._rendering.last_display_rect
        if display_rect is None:
            display_rect = self._pending_click_fallback_rect
        self._pending_click_point = None
        self._pending_click_fallback_rect = None
        if point is None or display_rect is None:
            return
        design_point = self._rendering.map_click(
            point,
            display_rect,
            require_inside=False,
        )
        if design_point is not None:
            self.clicked.emit(*design_point)

    @classmethod
    def click_delay_ms(cls) -> int:
        """Return the platform double-click interval plus the parity padding."""

        return max(
            1,
            int(QApplication.doubleClickInterval())
            + int(cls._MINIMAP_CLICK_DELAY_PADDING_MS),
        )


__all__ = ["MicroscopeMinimap"]
