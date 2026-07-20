from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.settings.axis_calibration_preview import (
    AxisCalibrationPreview,
)


class _QtBot:
    def __init__(self) -> None:
        self.widgets = []

    def addWidget(self, widget) -> None:
        self.widgets.append(widget)


@pytest.fixture
def qtbot():
    app = QApplication.instance() or QApplication([])
    bot = _QtBot()
    yield bot
    for widget in bot.widgets:
        widget.deleteLater()
    app.processEvents()


def test_curve_and_samples_are_populated_and_auto_ranged(qtbot) -> None:
    preview = AxisCalibrationPreview("X")
    qtbot.addWidget(preview)

    preview.set_curve((0.0, 1.0, 2.0), (10.0, 11.5, 14.0))

    assert preview.curve_item.getData()[0].tolist() == [0.0, 1.0, 2.0]
    assert len(preview.samples_item.points()) == 3
    assert preview.unit == "mm"


def test_rotary_axis_labels_use_degrees(qtbot) -> None:
    preview = AxisCalibrationPreview("B")
    qtbot.addWidget(preview)

    assert "deg" in preview.plot_widget.getAxis("bottom").labelString()
    assert "deg" in preview.plot_widget.getAxis("left").labelString()


def test_marker_update_does_not_replace_curve_or_reset_view(qtbot) -> None:
    preview = AxisCalibrationPreview("Z")
    qtbot.addWidget(preview)
    preview.set_curve((0.0, 1.0, 2.0), (0.0, 2.0, 5.0))
    curve = preview.curve_item
    preview.plot_widget.setXRange(0.2, 0.4, padding=0)
    preview.plot_widget.setYRange(0.5, 1.5, padding=0)
    before = preview.plot_widget.viewRange()

    preview.set_current_position(0.3, 0.6, visible=True)

    assert preview.curve_item is curve
    assert preview.current_position == pytest.approx((0.3, 0.6))
    assert preview.plot_widget.viewRange() == before
    assert preview.marker_item.isVisible()
    assert preview.position_line.isVisible()


def test_hiding_current_position_keeps_curve_visible(qtbot) -> None:
    preview = AxisCalibrationPreview("A")
    qtbot.addWidget(preview)
    preview.set_curve((-2.0, 0.0), (-3.0, 0.0))
    preview.set_current_position(-1.0, -1.5, visible=True)

    preview.set_current_position(None, None, visible=False)

    assert preview.curve_item.isVisible()
    assert not preview.marker_item.isVisible()
    assert not preview.position_line.isVisible()


def test_clear_curve_removes_curve_and_live_items(qtbot) -> None:
    preview = AxisCalibrationPreview("C")
    qtbot.addWidget(preview)
    preview.set_curve((0.0, 1.0), (0.0, 2.0))
    preview.set_current_position(0.5, 1.0, visible=True)

    preview.clear_curve()

    curve_x = preview.curve_item.getData()[0]
    assert curve_x is None or curve_x.size == 0
    assert len(preview.samples_item.points()) == 0
    assert not preview.marker_item.isVisible()
