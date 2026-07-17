from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.settings.precision_approach import (
    PrecisionApproachSettingsWidget,
)
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.precision_approach import PrecisionApproachProfile


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_precision_approach_widget_shows_axis_units_and_profile_state() -> None:
    _qt_app()
    settings = Settings()
    widget = PrecisionApproachSettingsWidget(settings.precision_approach)

    assert tuple(widget._rows) == ("X", "Y", "Z", "A", "B", "C")
    assert widget._rows["A"].backlash_spin.suffix() == " mm"
    assert widget._rows["B"].backlash_spin.suffix() == " °"
    assert widget._rows["Z"].enabled_checkbox.isChecked()
    assert widget._rows["Z"].backlash_spin.isEnabled()
    assert not widget._rows["A"].enabled_checkbox.isChecked()
    assert not widget._rows["A"].backlash_spin.isEnabled()
    assert widget._rows["Z"].preview_label.text() == (
        "Target − 0.030 mm → target"
    )

    widget.deleteLater()


def test_precision_approach_widget_keeps_disabled_values_and_saves_direction() -> None:
    _qt_app()
    settings = Settings()
    widget = PrecisionApproachSettingsWidget(settings.precision_approach)
    row = widget._rows["B"]

    row.enabled_checkbox.setChecked(True)
    row.backlash_spin.setValue(1.25)
    negative_index = row.direction_combo.findData(-1)
    assert negative_index >= 0
    row.direction_combo.setCurrentIndex(negative_index)
    assert row.preview_label.text() == "Target + 1.250 ° → target"
    row.enabled_checkbox.setChecked(False)

    widget.to_settings(settings)

    assert settings.precision_approach.profiles["B"] == PrecisionApproachProfile(
        False,
        1.25,
        -1,
    )
    widget.deleteLater()


def test_settings_dialog_contains_and_collects_precision_approach_tab() -> None:
    _qt_app()
    dialog = SettingsDialog(Settings(), initial_tab="Precision approach")

    labels = [dialog._tabs.tabText(index) for index in range(dialog._tabs.count())]
    assert "Precision approach" in labels
    assert dialog._tabs.currentWidget() is dialog._precision_approach_tab

    row = dialog._precision_approach_tab._rows["X"]
    row.enabled_checkbox.setChecked(True)
    row.backlash_spin.setValue(0.4)
    dialog._collect_settings()

    assert dialog.result_settings().precision_approach.profiles["X"] == (
        PrecisionApproachProfile(True, 0.4, 1)
    )
    dialog.deleteLater()
