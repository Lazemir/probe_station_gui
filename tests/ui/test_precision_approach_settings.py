from __future__ import annotations

import os
import threading
import time

import numpy as np
import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFileDialog, QFormLayout

from probe_station_gui.dialogs.settings.axis_settings import AxisSettingsWidget
from probe_station_gui.dialogs.settings import axis_settings as axis_settings_module
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.settings.axis_calibration_npz import ImportedAxisCalibration
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.precision_approach import PrecisionApproachProfile


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _widget(settings: Settings) -> AxisSettingsWidget:
    _qt_app()
    return AxisSettingsWidget(
        settings.axis_a_calibration,
        settings.axis_z_calibration,
        settings.precision_approach,
    )


def _wait_for_calibration_import(widget: AxisSettingsWidget) -> None:
    deadline = time.monotonic() + 5.0
    while widget._calibration_tasks_running and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert widget._calibration_tasks_running == 0


def _form_labels(widget: AxisSettingsWidget) -> set[str]:
    labels: set[str] = set()
    for layout in widget.findChildren(QFormLayout):
        for row in range(layout.rowCount()):
            item = layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item is not None and item.widget() is not None:
                labels.add(item.widget().text())
    return labels


def test_axis_settings_selector_and_per_axis_content() -> None:
    settings = Settings()
    widget = _widget(settings)

    assert [widget._axis_list.item(index).text() for index in range(6)] == [
        "X",
        "Y",
        "Z",
        "A",
        "B",
        "C",
    ]
    assert widget.selected_axis() == "Z"
    assert tuple(widget._pages) == ("X", "Y", "Z", "A", "B", "C")
    assert widget._rows["A"].backlash_spin.suffix() == " mm"
    assert widget._rows["B"].backlash_spin.suffix() == " °"
    assert widget._rows["Z"].enabled_checkbox.isChecked()
    assert widget._calibration_messages["X"].text() == (
        "No calibration curve for this axis."
    )
    assert tuple(widget._calibration_checkboxes) == ("Z", "A")
    assert tuple(widget._calibration_file_edits) == ("Z", "A")
    assert widget._calibration_file_edits["Z"].isReadOnly()
    assert widget._calibration_browse_buttons["A"].text() == "Browse"
    assert widget._calibration_reset_buttons["Z"].text() == "Reset"
    assert {"Path", "Curve source", "Fit error"}.isdisjoint(_form_labels(widget))

    widget.deleteLater()


def test_axis_switch_preserves_unapplied_precision_values() -> None:
    settings = Settings()
    widget = _widget(settings)
    b_row = widget._rows["B"]

    b_row.enabled_checkbox.setChecked(True)
    b_row.backlash_spin.setValue(1.25)
    b_row.direction_combo.setCurrentIndex(b_row.direction_combo.findData(-1))
    widget.select_axis("X")
    widget.select_axis("B")

    widget.to_settings(settings)
    assert settings.precision_approach.profiles["B"] == PrecisionApproachProfile(
        True,
        1.25,
        -1,
    )

    widget.deleteLater()


def test_axis_calibration_controls_show_persisted_interpolation_snapshot() -> None:
    settings = Settings()
    calibration = settings.axis_z_calibration
    calibration.configured = True
    calibration.model = "linear_interpolation"
    calibration.calibration_file = r"C:\calibration\z-axis.npz"
    calibration.interpolation_gcode_mm = [0.0, 1.0, 2.0]
    calibration.interpolation_display_mm = [0.1, 1.1, 2.1]
    calibration.interpolation_direction = 1
    widget = _widget(settings)

    assert widget._calibration_file_edits["Z"].text() == calibration.calibration_file
    assert widget._calibration_status_labels["Z"].text() == (
        "3 points \N{MIDDLE DOT} 0.000\N{EN DASH}2.000 mm"
    )

    widget.to_settings(settings)
    assert settings.axis_z_calibration.configured is True
    assert settings.axis_z_calibration.source == ""

    widget.deleteLater()


def test_successful_npz_browse_replaces_calibration_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings()
    path = tmp_path / "z-axis.npz"
    np.savez(path, gcode=[2.0, 0.0, 1.0], indicator=[2.1, 0.1, 1.1])
    widget = _widget(settings)
    selected: dict[str, str] = {}

    def choose_file(*args, **kwargs):
        selected["filter"] = args[3]
        return str(path), args[3]

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose_file)

    widget._calibration_browse_buttons["Z"].click()
    _wait_for_calibration_import(widget)
    widget.to_settings(settings)

    assert selected["filter"] == "NumPy calibration (*.npz)"
    assert settings.axis_z_calibration.model == "linear_interpolation"
    assert settings.axis_z_calibration.calibration_file == str(path.resolve())
    assert settings.axis_z_calibration.interpolation_gcode_mm == [0.0, 1.0, 2.0]
    assert settings.axis_z_calibration.interpolation_display_mm == [0.1, 1.1, 2.1]
    assert settings.axis_z_calibration.interpolation_direction is None
    assert settings.axis_z_calibration.configured is True
    assert settings.axis_z_calibration.source == ""
    assert widget._calibration_status_labels["Z"].text() == (
        "3 points \N{MIDDLE DOT} 0.000\N{EN DASH}2.000 mm"
    )

    widget.deleteLater()


def test_invalid_npz_import_keeps_previous_calibration(tmp_path) -> None:
    settings = Settings()
    valid_path = tmp_path / "valid.npz"
    invalid_path = tmp_path / "invalid.npz"
    np.savez(valid_path, gcode=[0.0, 1.0], indicator=[0.0, 1.0])
    np.savez(invalid_path, gcode=[0.0], indicator=[0.0])
    widget = _widget(settings)

    widget._start_calibration_import("Z", str(valid_path))
    _wait_for_calibration_import(widget)
    widget.to_settings(settings)
    accepted = settings.axis_z_calibration.clone()

    widget._start_calibration_import("Z", str(invalid_path))
    _wait_for_calibration_import(widget)
    widget.to_settings(settings)

    assert settings.axis_z_calibration == accepted
    assert widget._calibration_file_edits["Z"].text() == str(valid_path.resolve())
    assert "at least two" in widget._calibration_status_labels["Z"].text()
    assert widget._calibration_browse_buttons["Z"].isEnabled()
    assert widget._calibration_reset_buttons["Z"].isEnabled()

    widget.deleteLater()


def test_direction_change_during_import_does_not_enable_wrong_branch(
    monkeypatch,
) -> None:
    settings = Settings()
    settings.axis_a_calibration.configured = True
    widget = _widget(settings)
    started = threading.Event()
    release = threading.Event()

    def controlled_import(path, *, axis, final_direction):
        started.set()
        release.wait(timeout=5.0)
        return ImportedAxisCalibration(
            calibration_file=path,
            gcode_points_mm=(0.0, 1.0),
            display_points_mm=(0.0, 1.0),
            branch_direction=final_direction,
        )

    monkeypatch.setattr(
        axis_settings_module,
        "load_axis_calibration_npz",
        controlled_import,
    )

    widget._start_calibration_import("A", r"C:\calibration\a-axis.npz")
    assert started.wait(timeout=1.0)
    widget._rows["A"].direction_combo.setCurrentIndex(
        widget._rows["A"].direction_combo.findData(1)
    )
    release.set()
    _wait_for_calibration_import(widget)

    assert widget._calibration_checkboxes["A"].isChecked() is False
    assert widget._calibration_status_labels["A"].text() == (
        "Choose a curve for this direction."
    )

    widget.to_settings(settings)
    assert settings.axis_a_calibration.configured is False
    assert settings.axis_a_calibration.interpolation_direction == -1

    widget.deleteLater()


def test_cancelled_calibration_file_dialog_is_a_no_op(monkeypatch) -> None:
    settings = Settings()
    settings.axis_a_calibration.configured = True
    original = settings.axis_a_calibration.clone()
    widget = _widget(settings)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: ("", ""),
    )

    widget._calibration_browse_buttons["A"].click()
    widget.to_settings(settings)

    assert widget._calibration_tasks_running == 0
    assert settings.axis_a_calibration == original

    widget.deleteLater()


def test_reset_clears_imported_snapshot_and_disables_calibration() -> None:
    settings = Settings()
    calibration = settings.axis_z_calibration
    calibration.configured = True
    calibration.model = "linear_interpolation"
    calibration.calibration_file = r"C:\calibration\z-axis.npz"
    calibration.interpolation_gcode_mm = [0.0, 1.0]
    calibration.interpolation_display_mm = [0.1, 1.1]
    calibration.interpolation_direction = 1
    calibration.source = r"C:\legacy\z-curve.png"
    original_profile = settings.precision_approach.profiles["Z"]
    widget = _widget(settings)

    widget._calibration_reset_buttons["Z"].click()
    widget.to_settings(settings)

    result = settings.axis_z_calibration
    assert result.configured is False
    assert result.calibration_file == ""
    assert result.interpolation_gcode_mm == []
    assert result.interpolation_display_mm == []
    assert result.interpolation_direction is None
    assert result.source == ""
    assert widget._calibration_file_edits["Z"].text() == ""
    assert widget._calibration_status_labels["Z"].text() == ""
    assert settings.precision_approach.profiles["Z"] == original_profile

    widget.deleteLater()


def test_direction_mismatch_disables_imported_calibration(tmp_path) -> None:
    settings = Settings()
    path = tmp_path / "a-axis.npz"
    np.savez(
        path,
        gcode=[0.0, 1.0, 2.0],
        indicator=[0.0, -1.0, -2.0],
        direction=[-1, -1, -1],
    )
    widget = _widget(settings)

    widget._start_calibration_import("A", str(path))
    _wait_for_calibration_import(widget)
    assert widget._calibration_checkboxes["A"].isChecked()

    row = widget._rows["A"]
    row.direction_combo.setCurrentIndex(row.direction_combo.findData(1))
    widget.to_settings(settings)

    assert widget._calibration_checkboxes["A"].isChecked() is False
    assert settings.axis_a_calibration.configured is False
    assert widget._calibration_status_labels["A"].text() == (
        "Choose a curve for this direction."
    )

    widget.deleteLater()


def test_interpolation_checkbox_cannot_enable_missing_snapshot() -> None:
    settings = Settings()
    settings.axis_z_calibration.model = "linear_interpolation"
    settings.axis_z_calibration.configured = False
    widget = _widget(settings)

    widget._calibration_checkboxes["Z"].setChecked(True)
    widget.to_settings(settings)

    assert widget._calibration_checkboxes["Z"].isChecked() is False
    assert settings.axis_z_calibration.configured is False

    widget.deleteLater()


def test_interpolation_checkbox_cannot_enable_non_monotonic_snapshot() -> None:
    settings = Settings()
    calibration = settings.axis_z_calibration
    calibration.model = "linear_interpolation"
    calibration.configured = False
    calibration.interpolation_gcode_mm = [0.0, 1.0]
    calibration.interpolation_display_mm = [1.0, 0.0]
    widget = _widget(settings)

    widget._calibration_checkboxes["Z"].setChecked(True)
    widget.to_settings(settings)

    assert widget._calibration_checkboxes["Z"].isChecked() is False
    assert settings.axis_z_calibration.configured is False

    widget.deleteLater()


def test_legacy_calibration_ignores_stale_import_direction() -> None:
    settings = Settings()
    calibration = settings.axis_a_calibration
    calibration.configured = True
    calibration.interpolation_direction = 1
    widget = _widget(settings)

    assert widget._rows["A"].direction_combo.currentData() == -1
    assert widget._calibration_checkboxes["A"].isChecked() is True

    widget.to_settings(settings)
    assert settings.axis_a_calibration.configured is True

    widget.deleteLater()


@pytest.mark.parametrize(
    "initial_tab",
    ["Axes", "Axis Calibration", "Precision approach"],
)
def test_settings_dialog_uses_one_axes_tab_and_legacy_aliases(
    initial_tab: str,
) -> None:
    _qt_app()
    dialog = SettingsDialog(Settings(), initial_tab=initial_tab)

    labels = [dialog._tabs.tabText(index) for index in range(dialog._tabs.count())]
    assert "Coordinates" in labels
    assert "Axes" in labels
    assert "Axis Calibration" not in labels
    assert "Precision approach" not in labels
    assert dialog._tabs.currentWidget() is dialog._axes_tab

    dialog.reject()


def test_settings_dialog_collects_axis_settings_and_clears_legacy_source() -> None:
    _qt_app()
    settings = Settings()
    settings.axis_a_calibration.configured = True
    original_a = settings.axis_a_calibration.clone()
    dialog = SettingsDialog(settings)
    x_row = dialog._axes_tab._rows["X"]

    x_row.enabled_checkbox.setChecked(True)
    x_row.backlash_spin.setValue(0.125)
    x_row.direction_combo.setCurrentIndex(x_row.direction_combo.findData(-1))
    dialog._axes_tab._calibration_checkboxes["A"].setChecked(False)
    dialog._collect_settings()

    result = dialog.result_settings()
    assert result.precision_approach.profiles["X"] == PrecisionApproachProfile(
        enabled=True,
        backlash=0.125,
        final_direction=-1,
    )
    assert result.axis_a_calibration.configured is False
    assert result.axis_a_calibration.amplitude_mm == original_a.amplitude_mm
    assert result.axis_a_calibration.source == ""

    dialog.reject()
