from __future__ import annotations

import os
import threading
import time

import numpy as np
import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog

from probe_station_gui.dialogs.settings import axis_settings as axis_settings_module
from probe_station_gui.dialogs.settings.axis_settings import AxisSettingsWidget
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.settings.axis_calibration_config import (
    CALIBRATION_AXES,
    AxisCalibrationSettings,
)
from probe_station_gui.settings.axis_calibration_npz import ImportedAxisCalibration
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.precision_approach import PrecisionApproachProfile


class _PositionSource(QObject):
    stage_position_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.machine_position: tuple[float, ...] | None = None

    def latest_machine_position(self) -> tuple[float, ...] | None:
        return self.machine_position


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


def _curve(axis: str, *, enabled: bool = True) -> AxisCalibrationSettings:
    return AxisCalibrationSettings(
        enabled=enabled,
        calibration_file=f"C:/{axis.lower()}.npz",
        controller_points=[0.0, 1.0, 2.0],
        physical_points=[10.0, 11.5, 14.0],
    )


def _widget(
    settings: Settings,
    *,
    position_source: object | None = None,
) -> AxisSettingsWidget:
    return AxisSettingsWidget(
        settings.axis_calibrations,
        settings.precision_approach,
        position_source=position_source,
    )


def _wait_for_import(widget: AxisSettingsWidget) -> None:
    deadline = time.monotonic() + 5.0
    while widget._calibration_tasks_running and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert widget._calibration_tasks_running == 0


def test_every_axis_has_precision_and_calibration_controls(qtbot) -> None:
    widget = _widget(Settings())
    qtbot.addWidget(widget)

    assert tuple(widget._rows) == CALIBRATION_AXES
    assert tuple(widget._calibration_checkboxes) == CALIBRATION_AXES
    assert tuple(widget._calibration_file_edits) == CALIBRATION_AXES


@pytest.mark.parametrize("axis", CALIBRATION_AXES)
def test_axis_selection_shows_requested_page(qtbot, axis: str) -> None:
    widget = _widget(Settings())
    qtbot.addWidget(widget)

    widget.select_axis(axis)

    assert widget.selected_axis() == axis
    assert widget._stack.currentWidget() is widget._pages[axis]


def test_precision_settings_remain_independent_from_calibration(qtbot) -> None:
    settings = Settings()
    widget = _widget(settings)
    qtbot.addWidget(widget)
    row = widget._rows["X"]
    row.enabled_checkbox.setChecked(True)
    row.backlash_spin.setValue(0.125)
    row.direction_combo.setCurrentIndex(row.direction_combo.findData(-1))

    widget.to_settings(settings)

    assert settings.precision_approach.profiles["X"] == PrecisionApproachProfile(
        enabled=True,
        backlash=0.125,
        final_direction=-1,
    )
    assert settings.axis_calibrations["X"] == AxisCalibrationSettings()


def test_valid_saved_snapshot_creates_preview_for_only_that_axis(qtbot) -> None:
    settings = Settings()
    settings.axis_calibrations["B"] = _curve("B")

    widget = _widget(settings)
    qtbot.addWidget(widget)

    assert tuple(widget._calibration_previews) == ("B",)
    assert widget._calibration_previews["B"].unit == "deg"


def test_disabled_snapshot_keeps_curve_but_hides_current_marker(qtbot) -> None:
    settings = Settings()
    settings.axis_calibrations["Z"] = _curve("Z", enabled=False)
    source = _PositionSource()
    source.machine_position = (0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    widget = _widget(settings, position_source=source)
    qtbot.addWidget(widget)

    preview = widget._calibration_previews["Z"]
    assert preview.curve_item.isVisible()
    assert not preview.marker_item.isVisible()
    assert not preview.position_line.isVisible()


def test_live_status_updates_marker_from_cached_machine_coordinate(qtbot) -> None:
    settings = Settings()
    settings.axis_calibrations["Z"] = _curve("Z")
    source = _PositionSource()
    source.machine_position = (0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    widget = _widget(settings, position_source=source)
    qtbot.addWidget(widget)

    source.stage_position_changed.emit(source.machine_position)

    preview = widget._calibration_previews["Z"]
    assert preview.marker_item.isVisible()
    assert preview.current_position == pytest.approx((1.0, 11.5))


def test_current_position_outside_curve_hides_marker_and_reports_range(qtbot) -> None:
    settings = Settings()
    settings.axis_calibrations["X"] = _curve("X")
    source = _PositionSource()
    source.machine_position = (3.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    widget = _widget(settings, position_source=source)
    qtbot.addWidget(widget)

    source.stage_position_changed.emit(source.machine_position)

    preview = widget._calibration_previews["X"]
    assert not preview.marker_item.isVisible()
    assert preview.range_status.text() == "Current position is outside the calibration range"


def test_import_uses_new_schema_and_replaces_selected_axis(qtbot, tmp_path) -> None:
    path = tmp_path / "y.npz"
    np.savez(path, axis=np.array("Y"), controller=[0.0, 1.0], physical=[2.0, 4.0])
    settings = Settings()
    widget = _widget(settings)
    qtbot.addWidget(widget)

    widget._start_calibration_import("Y", str(path))
    _wait_for_import(widget)
    widget.to_settings(settings)

    saved = settings.axis_calibrations["Y"]
    assert saved.enabled
    assert saved.calibration_file == str(path.resolve())
    assert saved.controller_points == [0.0, 1.0]
    assert saved.physical_points == [2.0, 4.0]
    assert "Y" in widget._calibration_previews


def test_invalid_import_preserves_previous_snapshot_and_preview(qtbot, tmp_path) -> None:
    path = tmp_path / "invalid.npz"
    np.savez(path, axis=np.array("Z"), controller=[0.0, 1.0], physical=[1.0, 0.0])
    settings = Settings()
    settings.axis_calibrations["Z"] = _curve("Z")
    widget = _widget(settings)
    qtbot.addWidget(widget)
    preview = widget._calibration_previews["Z"]

    widget._start_calibration_import("Z", str(path))
    _wait_for_import(widget)
    widget.to_settings(settings)

    assert settings.axis_calibrations["Z"] == _curve("Z")
    assert widget._calibration_previews["Z"] is preview
    assert "strictly increasing" in widget._calibration_status_labels["Z"].text()


def test_reset_clears_snapshot_file_and_preview(qtbot) -> None:
    settings = Settings()
    settings.axis_calibrations["A"] = _curve("A")
    widget = _widget(settings)
    qtbot.addWidget(widget)

    widget._calibration_reset_buttons["A"].click()
    widget.to_settings(settings)

    assert settings.axis_calibrations["A"] == AxisCalibrationSettings()
    assert widget._calibration_file_edits["A"].text() == ""
    assert "A" not in widget._calibration_previews


def test_cancelled_file_dialog_changes_nothing(qtbot, monkeypatch) -> None:
    settings = Settings()
    settings.axis_calibrations["A"] = _curve("A")
    widget = _widget(settings)
    qtbot.addWidget(widget)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: ("", ""))

    widget._calibration_browse_buttons["A"].click()
    widget.to_settings(settings)

    assert settings.axis_calibrations["A"] == _curve("A")


def test_settings_dialog_passes_explicit_position_source(qtbot) -> None:
    source = _PositionSource()
    dialog = SettingsDialog(Settings(), axis_position_source=source, initial_tab="Axes")
    qtbot.addWidget(dialog)

    assert dialog._axes_tab._position_source is source
    assert dialog._tabs.currentWidget() is dialog._axes_tab


def test_settings_dialog_blocks_apply_while_import_is_active(qtbot, monkeypatch) -> None:
    settings = Settings()
    started = threading.Event()
    release = threading.Event()

    def controlled_import(path, *, expected_axis):
        started.set()
        release.wait(timeout=5.0)
        return ImportedAxisCalibration(
            axis=expected_axis,
            calibration_file=path,
            controller_points=(0.0, 1.0),
            physical_points=(2.0, 3.0),
        )

    monkeypatch.setattr(axis_settings_module, "load_axis_calibration_npz", controlled_import)
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)
    save = dialog._button_box.button(QDialogButtonBox.Save)
    apply = dialog._button_box.button(QDialogButtonBox.Apply)

    dialog._axes_tab._start_calibration_import("C", "C:/c.npz")
    assert started.wait(timeout=1.0)
    assert not save.isEnabled()
    assert not apply.isEnabled()

    release.set()
    _wait_for_import(dialog._axes_tab)
    assert save.isEnabled()
    assert apply.isEnabled()
