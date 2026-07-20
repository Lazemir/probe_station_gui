"""Embedded PyQtGraph preview for one measured axis curve."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from probe_station_gui.settings.axis_calibration_config import axis_unit


CURVE_VIEW_PADDING_FRACTION = 0.05


def _curve_arrays(
    controller: Sequence[float] | np.ndarray,
    physical: Sequence[float] | np.ndarray,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return finite, ordered one-dimensional curve arrays when usable."""

    try:
        controller_values = np.array(controller, dtype=float, copy=True)
        physical_values = np.array(physical, dtype=float, copy=True)
    except (TypeError, ValueError):
        return None
    if (
        controller_values.ndim != 1
        or physical_values.ndim != 1
        or controller_values.size == 0
        or controller_values.size != physical_values.size
        or not np.isfinite(controller_values).all()
        or not np.isfinite(physical_values).all()
        or np.any(np.diff(controller_values) <= 0)
        or np.any(np.diff(physical_values) <= 0)
    ):
        return None
    return controller_values, physical_values


def interpolated_curve_position(
    controller: Sequence[float] | np.ndarray,
    physical: Sequence[float] | np.ndarray,
    x: float,
) -> tuple[float, float] | None:
    """Return the curve position at *x*, or ``None`` outside a usable curve."""

    arrays = _curve_arrays(controller, physical)
    if arrays is None:
        return None
    controller_values, physical_values = arrays
    try:
        controller_value = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(controller_value) or not (
        controller_values[0] <= controller_value <= controller_values[-1]
    ):
        return None
    return controller_value, float(
        np.interp(controller_value, controller_values, physical_values)
    )


class AxisCalibrationPreview(QWidget):
    """Persistent curve items with constant-size live position updates."""

    def __init__(self, axis: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.axis = str(axis).upper()
        self.unit = axis_unit(self.axis)
        self.current_position: tuple[float, float] | None = None
        self.hover_position: tuple[float, float] | None = None
        self.preview_frame: tuple[float, float, float, float] | None = None
        self._controller_values = np.array([], dtype=float)
        self._physical_values = np.array([], dtype=float)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.plot_widget = pg.PlotWidget(self)
        self.plot_widget.setMinimumHeight(240)
        self.plot_widget.setBackground("#ffffff")
        self.plot_widget.setLabel(
            "bottom",
            f"Controller {self.axis}",
            units=self.unit,
        )
        self.plot_widget.setLabel(
            "left",
            f"Physical {self.axis}",
            units=self.unit,
        )
        for axis_item in (
            self.plot_widget.getAxis("bottom"),
            self.plot_widget.getAxis("left"),
        ):
            axis_item.setPen(pg.mkPen("#757575"))
            axis_item.setTextPen(pg.mkPen("#212121"))
        self.plot_widget.getPlotItem().getViewBox().setBorder(pg.mkPen("#757575"))
        self.plot_widget.showGrid(x=True, y=True, alpha=0.12)
        self.plot_widget.setMenuEnabled(True)
        self.plot_widget.viewport().installEventFilter(self)
        layout.addWidget(self.plot_widget)

        self.curve_item = self.plot_widget.plot(
            [],
            [],
            pen=pg.mkPen("#1976d2", width=2),
        )
        self.samples_item = pg.ScatterPlotItem(
            [],
            [],
            size=5,
            pen=pg.mkPen("#1565c0"),
            brush=pg.mkBrush("#64b5f6"),
            hoverable=True,
            hoverSize=9,
        )
        self.plot_widget.addItem(self.samples_item)
        self.marker_item = pg.ScatterPlotItem(
            [],
            [],
            symbol="d",
            size=15,
            pen=pg.mkPen("#212121", width=2),
            brush=pg.mkBrush("#ffca28"),
        )
        self.plot_widget.addItem(self.marker_item)
        self.position_line = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=pg.mkPen("#ef6c00", width=1, style=Qt.DashLine),
        )
        self.plot_widget.addItem(self.position_line)
        hover_pen = pg.mkPen("#455a64", width=1, style=Qt.DashLine)
        self.hover_vertical_line = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=hover_pen,
        )
        self.hover_horizontal_line = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=hover_pen,
        )
        self.hover_label = pg.TextItem(
            color="#212121",
            anchor=(0, 1),
            border=pg.mkPen("#9e9e9e"),
            fill=pg.mkBrush(255, 255, 255, 220),
        )
        self.hover_vertical_line.setZValue(20)
        self.hover_horizontal_line.setZValue(20)
        self.hover_label.setZValue(21)
        self.plot_widget.addItem(self.hover_vertical_line)
        self.plot_widget.addItem(self.hover_horizontal_line)
        self.plot_widget.addItem(self.hover_label)
        self.marker_item.hide()
        self.position_line.hide()
        self._hide_hover()

        self.range_status = QLabel("", self)
        self.range_status.setWordWrap(True)
        self.range_status.hide()
        layout.addWidget(self.range_status)

        self.samples_item.sigHovered.connect(self._show_hover_tooltip)
        self._mouse_proxy = pg.SignalProxy(
            self.plot_widget.scene().sigMouseMoved,
            rateLimit=60,
            slot=self._on_scene_mouse_moved,
        )

    def set_curve(
        self,
        controller: Sequence[float],
        physical: Sequence[float],
    ) -> None:
        """Replace curve data and auto-range exactly once for the new curve."""

        arrays = _curve_arrays(controller, physical)
        if arrays is None:
            self._controller_values = np.array([], dtype=float)
            self._physical_values = np.array([], dtype=float)
        else:
            self._controller_values, self._physical_values = arrays
        self.curve_item.setData(self._controller_values, self._physical_values)
        self.samples_item.setData(self._controller_values, self._physical_values)
        self._hide_hover()
        self._apply_curve_navigation_limits()
        self.plot_widget.enableAutoRange()
        self.plot_widget.autoRange()
        self.range_status.clear()
        self.range_status.hide()

    def _apply_curve_navigation_limits(self) -> None:
        if self._controller_values.size == 0:
            self._clear_curve_navigation_limits()
            return
        x_min = float(self._controller_values[0])
        x_max = float(self._controller_values[-1])
        y_min = float(self._physical_values[0])
        y_max = float(self._physical_values[-1])
        x_padding = (x_max - x_min) * CURVE_VIEW_PADDING_FRACTION
        y_padding = (y_max - y_min) * CURVE_VIEW_PADDING_FRACTION
        self.preview_frame = (
            x_min - x_padding,
            y_min - y_padding,
            x_max + x_padding,
            y_max + y_padding,
        )
        left, bottom, right, top = self.preview_frame
        # TODO(coordinate-system-rework): share a canonical bounded-view policy with Design Window after the coordinate/settings UI rewrite.
        self.plot_widget.getViewBox().setLimits(
            xMin=left,
            xMax=right,
            yMin=bottom,
            yMax=top,
            maxXRange=right - left,
            maxYRange=top - bottom,
        )

    def _clear_curve_navigation_limits(self) -> None:
        self.preview_frame = None
        self.plot_widget.getViewBox().setLimits(
            xMin=None,
            xMax=None,
            yMin=None,
            yMax=None,
            maxXRange=None,
            maxYRange=None,
        )

    def clear_curve(self) -> None:
        self.curve_item.setData([], [])
        self.samples_item.setData([], [])
        self._controller_values = np.array([], dtype=float)
        self._physical_values = np.array([], dtype=float)
        self._hide_hover()
        self._clear_curve_navigation_limits()
        self.set_current_position(None, None, visible=False)
        self.range_status.clear()
        self.range_status.hide()

    def set_current_position(
        self,
        controller: float | None,
        physical: float | None,
        *,
        visible: bool,
    ) -> None:
        shown = bool(visible and controller is not None and physical is not None)
        self.marker_item.setVisible(shown)
        self.position_line.setVisible(shown)
        if not shown:
            self.current_position = None
            return
        controller_value = float(controller)
        physical_value = float(physical)
        self.current_position = (controller_value, physical_value)
        self.marker_item.setData([controller_value], [physical_value])
        self.position_line.setValue(controller_value)
        self.range_status.clear()
        self.range_status.hide()

    def set_outside_range(self, outside: bool) -> None:
        self.set_current_position(None, None, visible=False)
        if outside:
            self.range_status.setText(
                "Current position is outside the calibration range"
            )
            self.range_status.show()
        else:
            self.range_status.clear()
            self.range_status.hide()

    def set_hover_controller_value(self, x: float, visible: bool = True) -> None:
        """Show interpolated curve guides for a controller value without re-ranging."""

        if not visible or self._controller_values.size == 0:
            self._hide_hover()
            return
        try:
            controller_value = float(x)
        except (TypeError, ValueError):
            self._hide_hover()
            return
        if not np.isfinite(controller_value) or not (
            self._controller_values[0]
            <= controller_value
            <= self._controller_values[-1]
        ):
            self._hide_hover()
            return

        physical_value = float(
            np.interp(
                controller_value,
                self._controller_values,
                self._physical_values,
            )
        )
        self.hover_position = (controller_value, physical_value)
        self.hover_vertical_line.setValue(controller_value)
        self.hover_horizontal_line.setValue(physical_value)
        self.hover_label.setText(
            f"X: {controller_value:.6g} {self.unit} · "
            f"Y: {physical_value:.6g} {self.unit}"
        )
        self._place_hover_label(controller_value, physical_value)
        self.hover_vertical_line.show()
        self.hover_horizontal_line.show()
        self.hover_label.show()

    def _hide_hover(self) -> None:
        self.hover_position = None
        self.hover_vertical_line.hide()
        self.hover_horizontal_line.hide()
        self.hover_label.hide()

    def _place_hover_label(self, x: float, y: float) -> None:
        (x_min, x_max), (y_min, y_max) = self.plot_widget.viewRange()
        self.hover_label.setAnchor(
            (
                0 if x <= (x_min + x_max) / 2 else 1,
                1 if y <= (y_min + y_max) / 2 else 0,
            )
        )
        self.hover_label.setPos(
            min(max(x, x_min), x_max),
            min(max(y, y_min), y_max),
        )

    def _on_scene_mouse_moved(self, event) -> None:
        scene_position = event[0]
        plot_item = self.plot_widget.getPlotItem()
        if not plot_item.sceneBoundingRect().contains(scene_position):
            self.set_hover_controller_value(0.0, visible=False)
            return
        view_position = plot_item.getViewBox().mapSceneToView(scene_position)
        self.set_hover_controller_value(view_position.x())

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.plot_widget.viewport()
            and event.type() == QEvent.Type.Leave
        ):
            self.set_hover_controller_value(0.0, visible=False)
        return super().eventFilter(watched, event)

    def _show_hover_tooltip(self, _item, points, _event) -> None:
        if not points:
            self.plot_widget.setToolTip("")
            return
        position = points[0].pos()
        self.plot_widget.setToolTip(
            f"Controller: {position.x():.6g} {self.unit}\n"
            f"Physical: {position.y():.6g} {self.unit}"
        )


__all__ = ["AxisCalibrationPreview", "interpolated_curve_position"]

