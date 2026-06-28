"""Result visualisation widgets for route measurement data."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.route.measurement_display import (
    axis_tick_decimals as _axis_tick_decimals,
    count_axis_ticks as _count_axis_ticks,
    histogram_counts as _histogram_counts,
    raw_data_rows as _raw_data_rows,
    resistance_axis_unit as _resistance_axis_unit,
    resistance_x_axis_label as _resistance_x_axis_label,
    sample_values as _sample_values,
)


class RouteMeasurementHistogram(QWidget):
    """Small histogram preview for the raw samples of the latest point."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._samples: tuple[object, ...] = ()
        self._mode = "differential"

    def set_samples(self, samples: tuple[object, ...]) -> None:
        self._samples = tuple(samples)
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = "polarity" if mode == "polarity" else "differential"
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        _ = event
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        series = self._series()
        values = [value for _label, _color, data in series for value in data]
        if not values:
            painter.setPen(self.palette().mid().color())
            painter.drawText(self.rect(), Qt.AlignCenter, "No raw data")
            return

        minimum = min(values)
        maximum = max(values)
        if minimum == maximum:
            span = abs(minimum) * 0.1 or 1.0
            minimum -= span
            maximum += span
        bin_count = max(1, min(20, int(math.sqrt(max(1, len(values)))) + 1))
        counts = [
            _histogram_counts(data, bin_count, minimum, maximum)
            for _label, _color, data in series
        ]
        max_count = max((max(item) if item else 0 for item in counts), default=1)
        max_count = max(1, max_count)

        metrics = painter.fontMetrics()
        left_margin = max(64, metrics.horizontalAdvance(str(max_count)) + 34)
        top_margin = 26 if len(series) > 1 else 18
        bottom_margin = 42
        plot = self.rect().adjusted(left_margin, top_margin, -12, -bottom_margin)
        if plot.width() <= 0 or plot.height() <= 0:
            return
        scale, unit = _resistance_axis_unit(values)
        x_ticks = (minimum, minimum + (maximum - minimum) * 0.5, maximum)
        x_decimals = _axis_tick_decimals((maximum - minimum) / scale)
        bar_width = plot.width() / bin_count
        axis_color = self.palette().mid().color()
        grid_color = QColor(axis_color)
        grid_color.setAlpha(90)

        painter.setPen(axis_color)
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        painter.drawLine(plot.bottomLeft(), plot.topLeft())

        for tick in _count_axis_ticks(max_count):
            y = plot.bottom() - plot.height() * (tick / max_count)
            painter.setPen(grid_color if tick > 0 else axis_color)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(axis_color)
            label_rect = QRectF(
                20,
                y - metrics.height() / 2,
                plot.left() - 26,
                metrics.height(),
            )
            painter.drawText(label_rect, Qt.AlignRight | Qt.AlignVCenter, str(tick))

        painter.save()
        painter.setPen(axis_color)
        painter.translate(4, plot.bottom())
        painter.rotate(-90)
        painter.drawText(
            QRectF(0, 0, plot.height(), metrics.height()),
            Qt.AlignCenter,
            "samples",
        )
        painter.restore()

        painter.setPen(axis_color)
        for tick in x_ticks:
            x = plot.left() + plot.width() * ((tick - minimum) / (maximum - minimum))
            painter.drawLine(
                int(round(x)),
                plot.bottom(),
                int(round(x)),
                plot.bottom() + 4,
            )
            label = f"{tick / scale:.{x_decimals}f}"
            label_width = max(56, metrics.horizontalAdvance(label) + 8)
            if tick == minimum:
                label_x = plot.left()
                alignment = Qt.AlignLeft | Qt.AlignVCenter
            elif tick == maximum:
                label_x = plot.right() - label_width
                alignment = Qt.AlignRight | Qt.AlignVCenter
            else:
                label_x = x - label_width / 2
                alignment = Qt.AlignCenter
            painter.drawText(
                QRectF(label_x, plot.bottom() + 5, label_width, metrics.height()),
                alignment,
                label,
            )
        x_axis_title_rect = QRectF(
            plot.left(),
            self.height() - metrics.height() - 2,
            plot.width(),
            metrics.height(),
        )
        painter.drawText(x_axis_title_rect, Qt.AlignCenter, self._x_axis_label(unit))

        if len(series) > 1:
            legend_x = plot.right()
            for label, color, _data in reversed(series):
                text_width = metrics.horizontalAdvance(label)
                legend_x -= text_width + 22
                painter.setPen(QColor(color).darker(125))
                painter.setBrush(QColor(color))
                painter.drawRect(QRectF(legend_x, 6, 10, 10))
                painter.setPen(axis_color)
                painter.drawText(
                    QRectF(legend_x + 14, 3, text_width + 4, metrics.height()),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    label,
                )
                legend_x -= 8

        for series_index, (_label, color, _data) in enumerate(series):
            count_row = counts[series_index]
            color = QColor(color)
            color.setAlpha(150 if len(series) > 1 else 190)
            painter.setPen(color.darker(125))
            painter.setBrush(color)
            for index, count in enumerate(count_row):
                if count <= 0:
                    continue
                height = plot.height() * (count / max_count)
                if len(series) > 1:
                    width = max(1.0, bar_width / len(series))
                    x = plot.left() + index * bar_width + series_index * width
                else:
                    width = max(1.0, bar_width - 2.0)
                    x = plot.left() + index * bar_width + 1.0
                rect = QRectF(
                    x,
                    plot.bottom() - height,
                    width,
                    height,
                )
                painter.drawRect(rect)
        painter.setPen(axis_color)
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        painter.drawLine(plot.bottomLeft(), plot.topLeft())

    def _x_axis_label(self, unit: str) -> str:
        return _resistance_x_axis_label(self._mode, unit)

    def _series(self) -> list[tuple[str, QColor, list[float]]]:
        if self._mode == "polarity":
            return [
                (
                    "negative",
                    QColor(200, 52, 60),
                    _sample_values(self._samples, "negative_resistance_ohm"),
                ),
                (
                    "positive",
                    QColor(36, 100, 210),
                    _sample_values(self._samples, "positive_resistance_ohm"),
                ),
            ]
        return [
            (
                "differential",
                QColor(43, 140, 96),
                _sample_values(self._samples, "differential_resistance_ohm"),
            )
        ]


class RouteMeasurementRawDataDialog(QDialog):
    """Copyable table of raw samples for one route measurement point."""

    HEADERS = (
        "sample",
        "polarity",
        "source_v",
        "measured_v",
        "current_a",
        "v_over_i_ohm",
        "differential_ohm",
        "compliance",
    )

    def __init__(self, samples: tuple[object, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Raw Measurement Data")
        self.resize(840, 420)
        self._rows = _raw_data_rows(samples)

        layout = QVBoxLayout(self)
        self._table = QTableWidget(len(self._rows), len(self.HEADERS), self)
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setSelectionBehavior(QAbstractItemView.SelectItems)
        for row_index, row in enumerate(self._rows):
            for column_index, value in enumerate(row):
                self._table.setItem(
                    row_index,
                    column_index,
                    QTableWidgetItem(value),
                )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(self._table)

        button_row = QHBoxLayout()
        copy_button = QPushButton("Copy All", self)
        close_button = QPushButton("Close", self)
        button_row.addWidget(copy_button)
        button_row.addStretch(1)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        copy_button.clicked.connect(self._copy_all)
        close_button.clicked.connect(self.accept)

    def _copy_all(self) -> None:
        lines = ["\t".join(self.HEADERS)]
        lines.extend("\t".join(row) for row in self._rows)
        clipboard = QApplication.clipboard()
        clipboard.setText("\n".join(lines))
