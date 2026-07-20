"""Embedded PyQtGraph preview for one measured axis curve."""

from __future__ import annotations

from collections.abc import Sequence

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from probe_station_gui.settings.axis_calibration_config import axis_unit


class AxisCalibrationPreview(QWidget):
    """Persistent curve items with constant-size live position updates."""

    def __init__(self, axis: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.axis = str(axis).upper()
        self.unit = axis_unit(self.axis)
        self.current_position: tuple[float, float] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.plot_widget = pg.PlotWidget(self)
        self.plot_widget.setMinimumHeight(240)
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
        self.plot_widget.showGrid(x=True, y=True, alpha=0.18)
        self.plot_widget.setMenuEnabled(True)
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
        self.marker_item.hide()
        self.position_line.hide()

        self.range_status = QLabel("", self)
        self.range_status.setWordWrap(True)
        self.range_status.hide()
        layout.addWidget(self.range_status)

        self.samples_item.sigHovered.connect(self._show_hover_tooltip)

    def set_curve(
        self,
        controller: Sequence[float],
        physical: Sequence[float],
    ) -> None:
        """Replace curve data and auto-range exactly once for the new curve."""

        self.curve_item.setData(controller, physical)
        self.samples_item.setData(controller, physical)
        self.plot_widget.enableAutoRange()
        self.plot_widget.autoRange()
        self.range_status.clear()
        self.range_status.hide()

    def clear_curve(self) -> None:
        self.curve_item.setData([], [])
        self.samples_item.setData([], [])
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

    def _show_hover_tooltip(self, _item, points, _event) -> None:
        if not points:
            self.plot_widget.setToolTip("")
            return
        position = points[0].pos()
        self.plot_widget.setToolTip(
            f"Controller: {position.x():.6g} {self.unit}\n"
            f"Physical: {position.y():.6g} {self.unit}"
        )


__all__ = ["AxisCalibrationPreview"]

