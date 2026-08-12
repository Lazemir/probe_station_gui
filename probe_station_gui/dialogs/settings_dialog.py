"""Dialog for configuring application settings."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QLocale, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.api.keys import ApiKeyStore
from probe_station_gui.dialogs.camera_settings_dialog import CameraSettingsWidget
import probe_station_gui.dialogs.settings.api_access as settings_api_access
import probe_station_gui.dialogs.settings.telegram as settings_telegram
from probe_station_gui.dialogs.settings.axis_settings import AxisSettingsWidget
from probe_station_gui.dialogs.settings.controls import (
    ControlsSettingsWidget,
    KeyBindingListEditor,
    KeyCaptureDialog,
)
from probe_station_gui.dialogs.settings.coordinate_system import (
    CoordinateSystemSettingsWidget,
)
from probe_station_gui.dialogs.settings.feedrates import (
    FeedrateGroupEditor,
    FeedrateSettingsWidget,
)
from probe_station_gui.dialogs.settings.jog import JogSettingsWidget
from probe_station_gui.dialogs.settings.measurement import MeasurementSettingsWidget
from probe_station_gui.dialogs.settings.objectives import ObjectivesSettingsWidget
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox as QDoubleSpinBox,
)
from probe_station_gui.settings.manager import (
    LoggingSettings,
    NeedleCalibrationSettings,
    Settings,
)
from probe_station_gui.settings.objective_config import ObjectivesSettings

__all__ = [
    "ControlsSettingsWidget",
    "CoordinateSystemSettingsWidget",
    "FeedrateGroupEditor",
    "FeedrateSettingsWidget",
    "JogSettingsWidget",
    "KeyBindingListEditor",
    "KeyCaptureDialog",
    "MeasurementSettingsWidget",
    "ObjectivesSettingsWidget",
    "SettingsDialog",
]


class LoggingSettingsWidget(QWidget):
    """Tab that exposes logging configuration."""

    LEVEL_OPTIONS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    def __init__(
        self, logging_settings: LoggingSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._level_combo = QComboBox(self)
        self._level_combo.addItems(self.LEVEL_OPTIONS)
        current_level = logging_settings.level.upper()
        if current_level in self.LEVEL_OPTIONS:
            self._level_combo.setCurrentText(current_level)
        layout.addRow(QLabel("Verbosity", self), self._level_combo)

        self._file_edit = QLineEdit(self)
        self._file_edit.setPlaceholderText("Leave blank for the default log file")
        self._file_edit.setText(logging_settings.file)
        layout.addRow(QLabel("Log file", self), self._file_edit)

    def to_settings(self, logging_settings: LoggingSettings) -> None:
        """Persist the widget state into the provided settings object."""

        logging_settings.level = self._level_combo.currentText()
        logging_settings.file = self._file_edit.text().strip()


class NeedleSettingsWidget(QWidget):
    """Tab that exposes needle-only settings."""

    COORDINATE_RANGE_MM = 1000000.0

    def __init__(
        self,
        calibration_settings: NeedleCalibrationSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._contact_zone_spin = QDoubleSpinBox(self)
        self._contact_zone_spin.setDecimals(3)
        self._contact_zone_spin.setRange(0.0, 10.0)
        self._contact_zone_spin.setSingleStep(0.01)
        self._contact_zone_spin.setSuffix(" mm")
        self._contact_zone_spin.setValue(calibration_settings.contact_zone_mm)
        layout.addRow(QLabel("Needle contact zone", self), self._contact_zone_spin)

        self._chip_contact_configured_checkbox = QCheckBox("Configured", self)
        self._chip_contact_configured_checkbox.setChecked(
            calibration_settings.chip_position.configured
        )
        layout.addRow(
            QLabel("Chip contact (machine)", self),
            self._chip_contact_configured_checkbox,
        )

        chip_contact_widget = QWidget(self)
        chip_contact_layout = QHBoxLayout(chip_contact_widget)
        chip_contact_layout.setContentsMargins(0, 0, 0, 0)
        self._chip_contact_x_spin = self._make_coordinate_spin(
            calibration_settings.chip_position.x_mm
        )
        self._chip_contact_y_spin = self._make_coordinate_spin(
            calibration_settings.chip_position.y_mm
        )
        self._chip_contact_z_spin = self._make_coordinate_spin(
            calibration_settings.chip_position.z_mm
        )
        self._chip_contact_reset_button = QPushButton("Reset", self)
        chip_contact_layout.addWidget(QLabel("X", chip_contact_widget))
        chip_contact_layout.addWidget(self._chip_contact_x_spin, 1)
        chip_contact_layout.addWidget(QLabel("Y", chip_contact_widget))
        chip_contact_layout.addWidget(self._chip_contact_y_spin, 1)
        chip_contact_layout.addWidget(QLabel("Z", chip_contact_widget))
        chip_contact_layout.addWidget(self._chip_contact_z_spin, 1)
        chip_contact_layout.addWidget(self._chip_contact_reset_button)
        layout.addRow(QLabel("Contact point", self), chip_contact_widget)

        self._chip_contact_configured_checkbox.toggled.connect(
            self._update_chip_contact_state
        )
        self._chip_contact_reset_button.clicked.connect(self._reset_chip_contact)
        self._update_chip_contact_state()

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        needle_settings = settings.needle_calibration.clone()
        needle_settings.contact_zone_mm = self._contact_zone_spin.value()
        chip_position = needle_settings.chip_position
        chip_position.configured = self._chip_contact_configured_checkbox.isChecked()
        if chip_position.configured:
            chip_position.x_mm = self._chip_contact_x_spin.value()
            chip_position.y_mm = self._chip_contact_y_spin.value()
            chip_position.z_mm = self._chip_contact_z_spin.value()
        else:
            chip_position.x_mm = 0.0
            chip_position.y_mm = 0.0
            chip_position.z_mm = 0.0
        settings.needle_calibration = needle_settings

    def _make_coordinate_spin(self, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setLocale(QLocale.c())
        spin.setDecimals(4)
        spin.setRange(-self.COORDINATE_RANGE_MM, self.COORDINATE_RANGE_MM)
        spin.setSingleStep(0.001)
        spin.setSuffix(" mm")
        spin.setValue(value)
        return spin

    def _update_chip_contact_state(self) -> None:
        enabled = self._chip_contact_configured_checkbox.isChecked()
        self._chip_contact_x_spin.setEnabled(enabled)
        self._chip_contact_y_spin.setEnabled(enabled)
        self._chip_contact_z_spin.setEnabled(enabled)

    def _reset_chip_contact(self) -> None:
        self._chip_contact_configured_checkbox.setChecked(False)
        self._chip_contact_x_spin.setValue(0.0)
        self._chip_contact_y_spin.setValue(0.0)
        self._chip_contact_z_spin.setValue(0.0)


class SettingsDialog(QDialog):
    """Main settings dialog with tabbed sections."""

    settings_applied = Signal(object)

    def __init__(
        self,
        settings: Settings,
        parent: QWidget | None = None,
        *,
        initial_tab: str | None = None,
        camera_settings_source: object | None = None,
        exposure_policy_source: object | None = None,
        axis_position_source: object | None = None,
        api_key_store: ApiKeyStore | None = None,
        physical_pose_source: Callable[[], object | None] | None = None,
        stage_idle_source: Callable[[], bool] | None = None,
        stage_state_signal: object | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._settings = settings.clone()
        self._applied_once = False
        self._calibration_imports_active = False
        self._camera_tab: CameraSettingsWidget | None = None
        self._accept_after_camera_apply = False
        self._collecting_settings = False
        self._deferred_camera_apply_result: bool | None = None
        self._camera_apply_busy = False

        root_layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        root_layout.addWidget(self._tabs)

        self._controls_tab = ControlsSettingsWidget(self._settings, self)
        self._api_tab = settings_api_access.ApiSettingsWidget(
            self._settings.api,
            self,
            api_key_store=api_key_store,
        )
        self._telegram_tab = settings_telegram.TelegramSettingsWidget(
            self._settings.telegram, self
        )
        self._logging_tab = LoggingSettingsWidget(self._settings.logging, self)
        self._jog_tab = JogSettingsWidget(self._settings.jog, self)
        self._measurement_tab = MeasurementSettingsWidget(
            self._settings.needle_calibration, self
        )
        self._needles_tab = NeedleSettingsWidget(
            self._settings.needle_calibration, self
        )
        self._coordinate_system_tab = CoordinateSystemSettingsWidget(
            self._settings.software_coordinates,
            self,
            physical_pose_source=physical_pose_source,
            stage_idle_source=stage_idle_source,
        )
        self._objectives_tab = ObjectivesSettingsWidget(
            self._settings.objectives,
            self,
        )
        self._axes_tab = AxisSettingsWidget(
            self._settings.axis_calibrations,
            self._settings.precision_approach,
            self,
            position_source=axis_position_source,
        )
        if camera_settings_source is not None:
            self._camera_tab = CameraSettingsWidget(
                camera_settings_source,
                self,
                exposure_policy_source=exposure_policy_source,
            )
            self._camera_tab.apply_finished.connect(self._on_camera_apply_finished)
        self._tabs.addTab(self._controls_tab, "Controls")
        self._tabs.addTab(self._api_tab, "API")
        if self._camera_tab is not None:
            self._tabs.addTab(self._camera_tab, "Camera")
        self._tabs.addTab(self._telegram_tab, "Telegram")
        self._tabs.addTab(self._jog_tab, "Jog")
        self._tabs.addTab(self._coordinate_system_tab, "Coordinates")
        self._tabs.addTab(self._axes_tab, "Axes")
        self._tabs.addTab(self._objectives_tab, "Objectives")
        self._tabs.addTab(self._measurement_tab, "Measurement")
        self._tabs.addTab(self._needles_tab, "Needles")
        self._tabs.addTab(self._logging_tab, "Logging")
        requested_tab = (initial_tab or "").strip().lower()
        if requested_tab in {"axis calibration", "precision approach"}:
            requested_tab = "axes"
        if requested_tab:
            for index in range(self._tabs.count()):
                if self._tabs.tabText(index).lower() == requested_tab:
                    self._tabs.setCurrentIndex(index)
                    break
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._refresh_camera_tab_if_current()

        self._button_box = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Apply | QDialogButtonBox.Cancel,
            self,
        )
        self._button_box.accepted.connect(self.accept)
        self._button_box.rejected.connect(self.reject)
        self._save_button = self._button_box.button(QDialogButtonBox.Save)
        self._apply_button = self._button_box.button(QDialogButtonBox.Apply)
        if self._apply_button is not None:
            self._apply_button.clicked.connect(self._apply_without_closing)
        self._axes_tab.calibration_imports_active_changed.connect(
            self._set_calibration_imports_active
        )
        self._coordinate_system_tab.availability_changed.connect(
            self.refresh_coordinate_availability
        )
        connect_stage_state = getattr(stage_state_signal, "connect", None)
        if callable(connect_stage_state):
            connect_stage_state(self.refresh_coordinate_availability)
        root_layout.addWidget(self._button_box)
        self.refresh_coordinate_availability()

    def _on_tab_changed(self, _index: int) -> None:
        self._refresh_camera_tab_if_current()

    def _refresh_camera_tab_if_current(self) -> None:
        if self._camera_tab is None:
            return
        if self._tabs.currentWidget() is not self._camera_tab:
            return
        if not self._camera_tab.has_loaded():
            self._camera_tab.refresh()

    def accept(self) -> None:  # type: ignore[override]
        if not self._settings_apply_available():
            return
        self._accept_after_camera_apply = True
        camera_started = self._collect_settings()
        self._applied_once = True
        self.settings_applied.emit(self._settings.clone())
        if camera_started:
            self._camera_apply_busy = True
            self.refresh_coordinate_availability()
            self._finish_deferred_camera_apply_if_ready()
            return
        self._accept_after_camera_apply = False
        self._telegram_tab.shutdown()
        super().accept()

    def _apply_without_closing(self) -> None:
        if not self._settings_apply_available():
            return
        self._accept_after_camera_apply = False
        camera_started = self._collect_settings()
        self._applied_once = True
        self.settings_applied.emit(self._settings.clone())
        if camera_started:
            self._camera_apply_busy = True
            self.refresh_coordinate_availability()
            self._finish_deferred_camera_apply_if_ready()

    def _set_calibration_imports_active(self, active: bool) -> None:
        self._calibration_imports_active = active
        self.refresh_coordinate_availability()

    def refresh_coordinate_availability(self) -> None:
        """Refresh the buttons after a local draft or stage-state change."""

        available, _message = self._coordinate_system_tab.apply_availability()
        enabled = (
            not self._calibration_imports_active
            and not self._camera_apply_busy
            and available
        )
        if self._save_button is not None:
            self._save_button.setEnabled(enabled)
        if self._apply_button is not None:
            self._apply_button.setEnabled(enabled)

    def _settings_apply_available(self) -> bool:
        if self._calibration_imports_active or self._camera_apply_busy:
            return False
        available, message = self._coordinate_system_tab.apply_availability()
        if not available:
            self._coordinate_system_tab.show_validation_message(message)
            self.refresh_coordinate_availability()
            return False
        return True

    def _collect_settings(self) -> bool:
        self._collecting_settings = True
        self._deferred_camera_apply_result = None
        try:
            self._controls_tab.to_settings(self._settings)
            self._api_tab.to_settings(self._settings)
            self._telegram_tab.to_settings(self._settings)
            self._jog_tab.to_settings(self._settings)
            self._coordinate_system_tab.to_settings(self._settings)
            self._objectives_tab.to_settings(self._settings)
            self._axes_tab.to_settings(self._settings)
            self._measurement_tab.to_settings(self._settings)
            self._needles_tab.to_settings(self._settings)
            self._logging_tab.to_settings(self._settings.logging)
            return bool(
                self._camera_tab is not None
                and self._camera_tab.apply_pending_settings()
            )
        finally:
            self._collecting_settings = False

    def _on_camera_apply_finished(self, success: bool) -> None:
        if self._collecting_settings:
            self._deferred_camera_apply_result = bool(success)
            return
        self._complete_camera_apply(bool(success))

    def _finish_deferred_camera_apply_if_ready(self) -> None:
        result = self._deferred_camera_apply_result
        self._deferred_camera_apply_result = None
        if result is not None:
            self._complete_camera_apply(result)

    def _complete_camera_apply(self, success: bool) -> None:
        self._camera_apply_busy = False
        self.refresh_coordinate_availability()
        if not self._accept_after_camera_apply:
            return
        self._accept_after_camera_apply = False
        if not success:
            return
        self._telegram_tab.shutdown()
        super().accept()

    def result_settings(self) -> Settings:
        """Return a clone of the adjusted settings."""

        return self._settings.clone()

    @property
    def coordinate_system_tab(self) -> CoordinateSystemSettingsWidget:
        """Expose the software-coordinate editor for settings integrations."""

        return self._coordinate_system_tab

    def set_objectives(self, objectives: ObjectivesSettings) -> None:
        """Reload effective objective settings after applying the dialog."""

        self._objectives_tab.set_objectives(objectives)
        self._settings.objectives = objectives.clone()

    def was_applied(self) -> bool:
        """Return True when settings were applied at least once."""

        return self._applied_once

    def reject(self) -> None:  # type: ignore[override]
        self._telegram_tab.shutdown()
        super().reject()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._telegram_tab.shutdown()
        super().closeEvent(event)
