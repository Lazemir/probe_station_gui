"""Objective profile settings widgets for the settings dialog."""

from __future__ import annotations

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QCheckBox, QFormLayout, QFrame, QLabel, QLineEdit, QWidget

from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
    ordered_objective_names,
)
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox as QDoubleSpinBox,
)


_CALIBRATION_STATUS = {
    True: "Configured",
    False: "Not configured",
}


class ObjectivesSettingsWidget(QWidget):
    """Tab that exposes objective selection, offsets, and autofocus parameters."""

    def __init__(
        self,
        objectives: ObjectivesSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._objectives = objectives.clone()
        self._active_editor_name = self._objectives.active_name

        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._active_combo = QComboBox(self)
        self._profile_combo = QComboBox(self)
        for name in ordered_objective_names(self._objectives.objectives):
            self._active_combo.addItem(name, name)
            self._profile_combo.addItem(name, name)
        active_index = self._active_combo.findData(self._objectives.active_name)
        if active_index >= 0:
            self._active_combo.setCurrentIndex(active_index)
        profile_index = self._profile_combo.findData(self._active_editor_name)
        if profile_index >= 0:
            self._profile_combo.setCurrentIndex(profile_index)

        self._apply_offsets_checkbox = QCheckBox(
            "Apply saved offset when objective changes",
            self,
        )
        self._apply_offsets_checkbox.setChecked(
            self._objectives.apply_offsets_on_change
        )
        layout.addRow(QLabel("Active objective", self), self._active_combo)
        layout.addRow(self._apply_offsets_checkbox)

        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        layout.addRow(separator)
        layout.addRow(QLabel("Edit objective", self), self._profile_combo)

        self._xy_configured_checkbox = QCheckBox("Use X/Y offset", self)
        self._z_configured_checkbox = QCheckBox("Use Z offset", self)
        self._magnification_spin = self._positive_spin(" x", decimals=2)
        self._x_offset_spin = self._offset_spin(" mm")
        self._y_offset_spin = self._offset_spin(" mm")
        self._z_offset_spin = self._offset_spin(" mm")
        self._autofocus_range_spin = self._positive_spin(" mm", decimals=4)
        self._autofocus_fine_spin = self._positive_spin(" mm", decimals=4)
        self._xy_calibration_status = QLineEdit(self)
        self._xy_calibration_status.setReadOnly(True)
        self._distortion_status = QLineEdit(self)
        self._distortion_status.setReadOnly(True)

        layout.addRow(QLabel("Magnification", self), self._magnification_spin)
        layout.addRow(self._xy_configured_checkbox)
        layout.addRow(QLabel("X correction", self), self._x_offset_spin)
        layout.addRow(QLabel("Y correction", self), self._y_offset_spin)
        layout.addRow(self._z_configured_checkbox)
        layout.addRow(QLabel("Z correction", self), self._z_offset_spin)
        layout.addRow(QLabel("AF range", self), self._autofocus_range_spin)
        layout.addRow(QLabel("AF fine step", self), self._autofocus_fine_spin)
        layout.addRow(QLabel("Click calibration", self), self._xy_calibration_status)
        layout.addRow(QLabel("Lens correction", self), self._distortion_status)

        self._profile_combo.currentIndexChanged.connect(
            lambda _index: self._on_profile_changed()
        )
        self._load_profile(self._active_editor_name)

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        self._save_active_profile_edits()
        settings.objectives = ObjectivesSettings(
            active_name=str(self._active_combo.currentData() or "X5"),
            apply_offsets_on_change=self._apply_offsets_checkbox.isChecked(),
            objectives={
                key: value.clone() for key, value in self._objectives.objectives.items()
            },
        )

    def set_objectives(self, objectives: ObjectivesSettings) -> None:
        """Reload the effective objective settings after an apply."""

        editor_name = str(
            self._profile_combo.currentData() or self._active_editor_name
        )
        self._objectives = objectives.clone()
        names = ordered_objective_names(self._objectives.objectives)
        active_name = self._objectives.active_name
        if active_name not in names:
            active_name = names[0]
        if editor_name not in names:
            editor_name = active_name
        for combo, selected_name in (
            (self._active_combo, active_name),
            (self._profile_combo, editor_name),
        ):
            combo.blockSignals(True)
            combo.clear()
            for name in names:
                combo.addItem(name, name)
            combo.setCurrentIndex(combo.findData(selected_name))
            combo.blockSignals(False)
        self._apply_offsets_checkbox.setChecked(
            self._objectives.apply_offsets_on_change
        )
        self._active_editor_name = editor_name
        self._load_profile(editor_name)

    def _on_profile_changed(self) -> None:
        self._save_active_profile_edits()
        self._active_editor_name = str(self._profile_combo.currentData() or "X5")
        self._load_profile(self._active_editor_name)

    def _load_profile(self, name: str) -> None:
        profile = self._objectives.objectives.setdefault(
            name, ObjectiveCalibrationSettings(name=name)
        )
        self._xy_configured_checkbox.setChecked(profile.xy_offset_configured)
        self._z_configured_checkbox.setChecked(profile.z_offset_configured)
        self._magnification_spin.setValue(profile.magnification)
        self._x_offset_spin.setValue(profile.xy_offset_x_mm)
        self._y_offset_spin.setValue(profile.xy_offset_y_mm)
        self._z_offset_spin.setValue(profile.z_offset_mm)
        self._autofocus_range_spin.setValue(profile.autofocus_range_mm)
        self._autofocus_fine_spin.setValue(profile.autofocus_fine_step_mm)
        self._xy_calibration_status.setText(
            _CALIBRATION_STATUS[profile.xy_calibration_configured]
        )
        self._distortion_status.setText(
            _CALIBRATION_STATUS[profile.distortion_correction_configured]
        )

    def _save_active_profile_edits(self) -> None:
        name = self._active_editor_name
        profile = self._objectives.objectives.setdefault(
            name, ObjectiveCalibrationSettings(name=name)
        )
        updated = profile.clone()
        updated.name = name
        updated.magnification = self._magnification_spin.value()
        updated.xy_offset_configured = self._xy_configured_checkbox.isChecked()
        updated.z_offset_configured = self._z_configured_checkbox.isChecked()
        updated.xy_offset_x_mm = self._x_offset_spin.value()
        updated.xy_offset_y_mm = self._y_offset_spin.value()
        updated.z_offset_mm = self._z_offset_spin.value()
        updated.autofocus_range_mm = self._autofocus_range_spin.value()
        updated.autofocus_fine_step_mm = self._autofocus_fine_spin.value()
        self._objectives.objectives[name] = updated

    def _offset_spin(self, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setLocale(QLocale.c())
        spin.setDecimals(4)
        spin.setRange(-100.0, 100.0)
        spin.setSingleStep(0.01)
        spin.setSuffix(suffix)
        return spin

    def _positive_spin(self, suffix: str, *, decimals: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setLocale(QLocale.c())
        spin.setDecimals(decimals)
        spin.setRange(0.0001, 10000.0)
        spin.setSingleStep(0.01)
        spin.setSuffix(suffix)
        return spin


__all__ = ["ObjectivesSettingsWidget"]
