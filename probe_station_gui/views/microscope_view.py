"""Microscope frame widget with thin Qt interaction entrypoints."""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

from probe_station_gui.design.klayout_workers import KLayoutRenderWorker
from probe_station_gui.design.model import DesignDocument, MeasurementTarget
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.views.microscope_interaction import (
    ClickMoveBindings,
    ClickMoveConfig,
    ImageGeometry,
    MicroscopeInteraction,
    PointerDispatch,
)
from probe_station_gui.views.microscope_minimap import MicroscopeMinimap
from probe_station_gui.views.microscope_overlay_rendering import (
    draw_axis_triad,
    draw_scale_bar,
)


logger = logging.getLogger(__name__)


class MicroscopeView(QWidget):
    """Render camera frames while delegating interaction state to a deep module."""

    clicked: Signal = Signal(float, float, float, float)
    hovered: Signal = Signal(float, float, float, float)
    hover_left: Signal = Signal()
    design_minimap_clicked: Signal = Signal(float, float)
    design_minimap_double_clicked: Signal = Signal()
    measure_mode_exited: Signal = Signal()

    def __init__(
        self,
        *,
        click_move_bindings: ClickMoveBindings | None = None,
        click_move_config: ClickMoveConfig | None = None,
        minimap_render_worker_factory: Callable[
            [], KLayoutRenderWorker
        ] = KLayoutRenderWorker,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Microscope Qt")
        self.setMinimumSize(640, 480)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor("black"))
        self.setPalette(palette)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self._pix: QPixmap | None = None
        self._display_rect: QRect | None = None
        self._scale_mm_per_pixel_x: float | None = None
        self._scale_mm_per_pixel_y: float | None = None
        bindings = click_move_bindings or _inactive_bindings(self.update)
        config = click_move_config or ClickMoveConfig(pending_timeout_s=lambda: 5.0)
        self.interaction = MicroscopeInteraction(bindings, config)
        self.interaction.setParent(self)
        if click_move_bindings is not None:
            self.clicked.connect(self.interaction.handle_click)
        self._minimap = MicroscopeMinimap(
            worker_factory=minimap_render_worker_factory,
            device_pixel_ratio=self.devicePixelRatioF,
            parent=self,
        )
        self._minimap.changed.connect(self.update)
        self._minimap.clicked.connect(self.design_minimap_clicked.emit)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def set_frame(self, qimg: QImage) -> None:
        self._pix = QPixmap.fromImage(qimg)
        self.update()

    def set_scale(self, mm_per_pixel_x: float, mm_per_pixel_y: float) -> None:
        self._scale_mm_per_pixel_x = mm_per_pixel_x if mm_per_pixel_x > 0 else None
        self._scale_mm_per_pixel_y = mm_per_pixel_y if mm_per_pixel_y > 0 else None
        self.update()

    def set_measure_mode(self, mode: str | None) -> None:
        self.interaction.set_measure_mode(mode)
        self.setCursor(Qt.CrossCursor if mode else Qt.ArrowCursor)

    def set_design_minimap_data(
        self,
        *,
        document: DesignDocument | None,
        targets: list[MeasurementTarget],
        selected_target_id: str | None,
        probe_route: MeasurementRoute | None,
        selected_route_point_index: int,
        selected_design_point: tuple[float, float] | None,
        current_design_position: tuple[float, float] | None,
        fov_design_size: tuple[float, float] | None,
        source_design_marks: list[tuple[float, float]],
        check_design_marks: list[tuple[float, float]],
    ) -> None:
        bounds = document.bounds if document is not None else (0.0, 0.0, 1.0, 1.0)
        self._minimap.configure(
            document,
            bounds,
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

    def shutdown(self) -> None:
        self.interaction.shutdown()
        self._minimap.shutdown()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.shutdown()
        super().closeEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        pixmap = self._pix
        if pixmap is None:
            painter.end()
            return
        scaled = pixmap.scaled(self.size(), Qt.KeepAspectRatio)
        pos_x = (self.width() - scaled.width()) // 2
        pos_y = (self.height() - scaled.height()) // 2
        painter.drawPixmap(pos_x, pos_y, scaled)
        display_rect = QRect(pos_x, pos_y, scaled.width(), scaled.height())
        self._display_rect = display_rect
        painter.setRenderHint(QPainter.Antialiasing)
        self._draw_crosshair(painter, display_rect)
        scale_x = display_rect.width() / pixmap.width()
        scale_y = display_rect.height() / pixmap.height()
        self.interaction.draw(
            painter,
            display_rect,
            scale_x,
            scale_y,
            canvas_width=self.width(),
            mm_per_pixel_x=self._scale_mm_per_pixel_x,
            mm_per_pixel_y=self._scale_mm_per_pixel_y,
        )
        self._minimap.draw(painter, display_rect)
        draw_scale_bar(
            painter,
            display_rect,
            scale_x,
            self._scale_mm_per_pixel_x,
        )
        draw_axis_triad(painter, display_rect)
        painter.end()

    def _draw_crosshair(self, painter: QPainter, display_rect: QRect) -> None:
        center = display_rect.center()
        painter.setPen(QPen(QColor("cyan"), 1))
        painter.drawLine(display_rect.left(), center.y(), display_rect.right(), center.y())
        painter.drawLine(center.x(), display_rect.top(), center.x(), display_rect.bottom())
        painter.drawEllipse(QPoint(center.x(), center.y()), 4, 4)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton or self._pix is None or self._display_rect is None:
            return
        point = event.position().toPoint()
        if self._minimap.contains(point, self._display_rect):
            self._minimap.queue_click(point, self._display_rect)
            event.accept()
            return
        dispatch = self.interaction.press(event.position(), self._image_geometry())
        self._dispatch_pointer(event, dispatch)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        dispatch = self.interaction.release(event.position(), self._image_geometry())
        if not self._dispatch_pointer(event, dispatch):
            super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        dispatch = self.interaction.move(
            event.position(),
            self._image_geometry(),
            left_button=bool(event.buttons() & Qt.LeftButton),
            modifiers=event.modifiers(),
        )
        self._dispatch_pointer(event, dispatch)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._emit_dispatch(self.interaction.leave())
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        point = event.position().toPoint()
        if (
            event.button() == Qt.LeftButton
            and self._display_rect is not None
            and self._minimap.contains(point, self._display_rect)
        ):
            self._minimap.cancel_click()
            self.design_minimap_double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _dispatch_pointer(self, event: QMouseEvent, dispatch: PointerDispatch) -> bool:
        self._emit_dispatch(dispatch)
        if dispatch.accepted:
            event.accept()
            return True
        return False

    def _emit_dispatch(self, dispatch: PointerDispatch) -> None:
        if dispatch.click is not None:
            self.clicked.emit(*dispatch.click)
        if dispatch.hover is not None:
            self.hovered.emit(*dispatch.hover)
        if dispatch.hover_left:
            self.hover_left.emit()

    def _image_geometry(self) -> ImageGeometry:
        display_rect = self._display_rect or QRect()
        image_size = self._pix.size() if self._pix is not None else QRect().size()
        return ImageGeometry(display_rect, image_size)

def _inactive_bindings(repaint: Callable[[], None]) -> ClickMoveBindings:
    return ClickMoveBindings(
        request_move=lambda _dx, _dy: False,
        stage_connected=lambda: False,
        motion_blocked=lambda: True,
        mark_motion_axes=lambda _axes: None,
        show_status=lambda _message, _timeout_ms=0: None,
        repaint=repaint,
        preview_hover=lambda _dx, _dy: None,
        present_coordinates=lambda **_coordinates: None,
        manual_alignment_active=lambda: False,
        capture_manual_alignment=lambda _dx, _dy: None,
        pending_state_changed=lambda _pending: None,
    )


__all__ = ["MicroscopeView"]
