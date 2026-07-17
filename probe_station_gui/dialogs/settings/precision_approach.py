"""Precision final-approach editor for every stage axis."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QLabel,
    QWidget,
)

from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.precision_approach import (
    PRECISION_APPROACH_AXES,
    PrecisionApproachProfile,
    PrecisionApproachSettings,
)
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox as QDoubleSpinBox,
)


@dataclass(frozen=True)
class _PrecisionApproachRow:
    enabled_checkbox: QCheckBox
    backlash_spin: QDoubleSpinBox
    direction_combo: QComboBox
    preview_label: QLabel


class PrecisionApproachSettingsWidget(QWidget):
    """Edit backlash distance and final loaded side per stage axis."""

    def __init__(
        self,
        approach_settings: PrecisionApproachSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.addWidget(QLabel("Axis", self), 0, 0)
        layout.addWidget(QLabel("Enabled", self), 0, 1)
        layout.addWidget(QLabel("Backlash", self), 0, 2)
        layout.addWidget(QLabel("Final direction", self), 0, 3)
        layout.addWidget(QLabel("Path", self), 0, 4)

        self._rows: dict[str, _PrecisionApproachRow] = {}
        for row_index, axis in enumerate(PRECISION_APPROACH_AXES, start=1):
            profile = approach_settings.profiles[axis]
            axis_label = QLabel(axis, self)
            enabled_checkbox = QCheckBox(self)
            enabled_checkbox.setChecked(profile.enabled)
            backlash_spin = QDoubleSpinBox(self)
            backlash_spin.setLocale(QLocale.c())
            backlash_spin.setDecimals(3)
            backlash_spin.setRange(0.0, 1000000.0)
            backlash_spin.setSingleStep(0.001)
            backlash_spin.setSuffix(f" {_axis_unit(axis)}")
            backlash_spin.setValue(profile.backlash)
            direction_combo = QComboBox(self)
            direction_combo.addItem("From lower coordinates (+)", 1)
            direction_combo.addItem("From higher coordinates (−)", -1)
            direction_index = direction_combo.findData(profile.final_direction)
            direction_combo.setCurrentIndex(max(0, direction_index))
            preview_label = QLabel(self)

            layout.addWidget(axis_label, row_index, 0)
            layout.addWidget(enabled_checkbox, row_index, 1)
            layout.addWidget(backlash_spin, row_index, 2)
            layout.addWidget(direction_combo, row_index, 3)
            layout.addWidget(preview_label, row_index, 4)
            row = _PrecisionApproachRow(
                enabled_checkbox,
                backlash_spin,
                direction_combo,
                preview_label,
            )
            self._rows[axis] = row
            enabled_checkbox.toggled.connect(
                lambda enabled, current=row: self._set_row_enabled(current, enabled)
            )
            backlash_spin.valueChanged.connect(
                lambda _value, current_axis=axis: self._update_preview(current_axis)
            )
            direction_combo.currentIndexChanged.connect(
                lambda _index, current_axis=axis: self._update_preview(current_axis)
            )
            self._set_row_enabled(row, profile.enabled)
            self._update_preview(axis)

        layout.setColumnStretch(4, 1)
        layout.setRowStretch(len(PRECISION_APPROACH_AXES) + 1, 1)

    def to_settings(self, settings: Settings) -> None:
        profiles: dict[str, PrecisionApproachProfile] = {}
        for axis, row in self._rows.items():
            profiles[axis] = PrecisionApproachProfile(
                enabled=row.enabled_checkbox.isChecked(),
                backlash=row.backlash_spin.value(),
                final_direction=int(row.direction_combo.currentData() or 1),
            )
        settings.precision_approach = PrecisionApproachSettings(profiles)

    def _set_row_enabled(self, row: _PrecisionApproachRow, enabled: bool) -> None:
        row.backlash_spin.setEnabled(enabled)
        row.direction_combo.setEnabled(enabled)
        row.preview_label.setEnabled(enabled)

    def _update_preview(self, axis: str) -> None:
        row = self._rows[axis]
        operator = "−" if int(row.direction_combo.currentData() or 1) > 0 else "+"
        row.preview_label.setText(
            f"Target {operator} {row.backlash_spin.value():.3f} "
            f"{_axis_unit(axis)} → target"
        )


def _axis_unit(axis: str) -> str:
    return "°" if axis in {"B", "C"} else "mm"


__all__ = ["PrecisionApproachSettingsWidget"]
