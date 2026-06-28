"""Coordinate-system settings widgets for the settings dialog."""

from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QFrame, QLabel, QVBoxLayout, QWidget

from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.sections import (
    WORK_COORDINATE_SYSTEMS,
    CoordinateSystemSettings,
)
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox


def coordinate_system_hint_state(
    *, position_mode: str, startup_mode: str
) -> tuple[bool, str]:
    """Return preferred-WCS enabled state and tooltip for coordinate modes."""

    if position_mode == "machine":
        return False, "Unused in absolute machine-coordinate mode."
    if startup_mode == "fixed":
        return True, "This WCS will be sent to the controller on connect."
    return True, "Controller-selected WCS will be used."


class CoordinateSystemSettingsWidget(QWidget):
    """Tab that exposes WCS startup mode."""

    def __init__(
        self,
        coordinate_settings: CoordinateSystemSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        mode_layout = QFormLayout()
        mode_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._position_mode_combo = QComboBox(self)
        self._position_mode_combo.addItem("Relative WCS coordinates", "work")
        self._position_mode_combo.addItem("Absolute machine coordinates", "machine")
        position_index = self._position_mode_combo.findData(
            coordinate_settings.position_mode
        )
        if position_index >= 0:
            self._position_mode_combo.setCurrentIndex(position_index)
        mode_layout.addRow(QLabel("Position mode", self), self._position_mode_combo)

        self._startup_mode_combo = QComboBox(self)
        self._startup_mode_combo.addItem(
            "Follow controller active system", "controller"
        )
        self._startup_mode_combo.addItem("Force selected system on connect", "fixed")
        mode_index = self._startup_mode_combo.findData(coordinate_settings.startup_mode)
        if mode_index >= 0:
            self._startup_mode_combo.setCurrentIndex(mode_index)
        mode_layout.addRow(QLabel("Coordinate mode", self), self._startup_mode_combo)

        self._preferred_system_combo = QComboBox(self)
        for system in WORK_COORDINATE_SYSTEMS:
            self._preferred_system_combo.addItem(system, system)
        preferred_index = self._preferred_system_combo.findData(
            coordinate_settings.preferred_system
        )
        if preferred_index >= 0:
            self._preferred_system_combo.setCurrentIndex(preferred_index)
        mode_layout.addRow(QLabel("Preferred WCS", self), self._preferred_system_combo)

        mode_widget = QWidget(self)
        mode_widget.setLayout(mode_layout)
        root_layout.addWidget(mode_widget)
        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        root_layout.addWidget(separator)
        root_layout.addStretch(1)
        self._position_mode_combo.currentIndexChanged.connect(
            self._update_mode_hint_state
        )
        self._startup_mode_combo.currentIndexChanged.connect(
            self._update_mode_hint_state
        )
        self._update_mode_hint_state()

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        startup_mode = str(self._startup_mode_combo.currentData() or "controller")
        preferred_system = str(self._preferred_system_combo.currentData() or "G54")
        settings.coordinate_system = CoordinateSystemSettings(
            position_mode=str(self._position_mode_combo.currentData() or "work"),
            startup_mode=startup_mode,
            preferred_system=preferred_system,
        )

    def _update_mode_hint_state(self) -> None:
        enabled, tooltip = coordinate_system_hint_state(
            position_mode=str(self._position_mode_combo.currentData() or ""),
            startup_mode=str(self._startup_mode_combo.currentData() or ""),
        )
        self._preferred_system_combo.setEnabled(enabled)
        self._preferred_system_combo.setToolTip(tooltip)


__all__ = ["CoordinateSystemSettingsWidget", "coordinate_system_hint_state"]
