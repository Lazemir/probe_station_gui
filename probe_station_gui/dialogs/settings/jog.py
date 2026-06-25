"""Jog settings widgets for the settings dialog."""

from __future__ import annotations

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QCheckBox, QFormLayout, QLabel, QWidget

from probe_station_gui.settings.manager import JogSettings, Settings
from probe_station_gui.shared.wheel_guard import GuardedDoubleSpinBox as QDoubleSpinBox


class JogSettingsWidget(QWidget):
    """Tab that exposes joystick jog distances."""

    def __init__(
        self, jog_settings: JogSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._linear_distance_spin = QDoubleSpinBox(self)
        self._linear_distance_spin.setLocale(QLocale.c())
        self._linear_distance_spin.setDecimals(3)
        self._linear_distance_spin.setRange(0.001, 1000.0)
        self._linear_distance_spin.setSingleStep(1.0)
        self._linear_distance_spin.setSuffix(" mm")
        self._linear_distance_spin.setValue(jog_settings.linear_distance_mm)
        layout.addRow(QLabel("Linear jog distance", self), self._linear_distance_spin)

        self._motion_safety_checkbox = QCheckBox("Disable motion safety", self)
        self._motion_safety_checkbox.setChecked(jog_settings.motion_safety_disabled)
        self._motion_safety_checkbox.setToolTip(
            "Allows joystick movement when needle state is unknown/down and skips axis limit checks."
        )
        layout.addRow(self._motion_safety_checkbox)

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        jog = settings.jog.clone()
        jog.linear_distance_mm = self._linear_distance_spin.value()
        jog.motion_safety_disabled = self._motion_safety_checkbox.isChecked()
        settings.jog = jog
