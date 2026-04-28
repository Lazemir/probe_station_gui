"""Dialog for configuring application settings."""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, cast

from PySide6.QtCore import QEvent, QLocale, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QFrame,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.qt_compat import keyboard_modifiers_to_int, native_scan_code_to_int
from probe_station_gui.settings_manager import (
    CONTROL_ACTIONS,
    CoordinateSystemSettings,
    FeedrateGroup,
    FeedrateSettings,
    JogSettings,
    KeyBinding,
    LCR_APERTURE_RATES,
    LCR_LEVEL_MODES,
    LCR_MEASUREMENT_FUNCTIONS,
    LCR_MONITOR_PARAMETERS,
    LCR_RANGE_MODES,
    LCR_SOURCE_RESISTANCES_OHM,
    LCR_TRIGGER_SOURCES,
    LoggingSettings,
    NeedleCalibrationSettings,
    Settings,
    WORK_COORDINATE_SYSTEMS,
)


class KeyCaptureDialog(QDialog):
    """Modal dialog that captures a single key press."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Capture Key")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Press a key to assign it to the action."))
        layout.addWidget(
            QLabel(
                "Press Escape to cancel. Modifier keys such as Shift or Ctrl can be held while pressing the key."
            )
        )
        self._binding: KeyBinding | None = None

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.ShortcutOverride:
            event.accept()
            return True

        if event.type() == QEvent.KeyPress:
            key_event = cast(QKeyEvent, event)
            key = key_event.key()
            if key in (Qt.Key_Escape, Qt.Key_Cancel):
                self.reject()
                return True
            if key in (
                Qt.Key_Shift,
                Qt.Key_Control,
                Qt.Key_Meta,
                Qt.Key_Alt,
                Qt.Key_AltGr,
                Qt.Key_Super_L,
                Qt.Key_Super_R,
            ):
                return True
            if key == Qt.Key_unknown:
                return True
            self._binding = KeyBinding(
                qt_key=int(key),
                modifiers=keyboard_modifiers_to_int(key_event.modifiers()),
                native_scan_code=native_scan_code_to_int(key_event.nativeScanCode()),
                text=key_event.text(),
            )
            self.accept()
            return True

        return super().event(event)

    def reject(self) -> None:  # type: ignore[override]
        self._binding = None
        super().reject()

    def binding(self) -> KeyBinding | None:
        """Return the captured binding if one was recorded."""

        return self._binding


class KeyBindingListEditor(QWidget):
    """Widget that manages a list of key bindings for a single action."""

    bindings_changed = Signal()

    def __init__(self, bindings: List[KeyBinding], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bindings: List[KeyBinding] = list(bindings)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._list = QListWidget(self)
        layout.addWidget(self._list)

        button_row = QHBoxLayout()
        self._add_button = QPushButton("Add", self)
        self._remove_button = QPushButton("Remove", self)
        button_row.addWidget(self._add_button)
        button_row.addWidget(self._remove_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self._add_button.clicked.connect(self._add_binding)
        self._remove_button.clicked.connect(self._remove_selected)
        self._list.itemSelectionChanged.connect(self._update_buttons)

        self._refresh()

    def bindings(self) -> List[KeyBinding]:
        """Return the list of configured bindings."""

        return list(self._bindings)

    def _refresh(self) -> None:
        self._list.clear()
        for binding in self._bindings:
            self._list.addItem(QListWidgetItem(self._binding_text(binding)))
        self._update_buttons()

    def _update_buttons(self) -> None:
        self._remove_button.setEnabled(bool(self._list.selectedItems()))

    def _add_binding(self) -> None:
        dialog = KeyCaptureDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        binding = dialog.binding()
        if not binding:
            return
        if binding in self._bindings:
            return
        self._bindings.append(binding)
        self._refresh()
        self.bindings_changed.emit()

    def _remove_selected(self) -> None:
        selected = self._list.selectedIndexes()
        if not selected:
            return
        index = selected[0].row()
        if 0 <= index < len(self._bindings):
            del self._bindings[index]
            self._refresh()
            self.bindings_changed.emit()

    def _binding_text(self, binding: KeyBinding) -> str:
        if binding.modifiers:
            sequence = QKeySequence(binding.qt_key | binding.modifiers)
        else:
            sequence = QKeySequence(binding.qt_key)
        sequence_text = sequence.toString(QKeySequence.NativeText)
        if sequence_text:
            if binding.native_scan_code:
                return f"{sequence_text} [physical]"
            return sequence_text
        if binding.text:
            if binding.native_scan_code:
                return f"{binding.text} [physical]"
            return binding.text
        if binding.native_scan_code:
            return f"Scan {binding.native_scan_code} [physical]"
        return f"Key {binding.qt_key}"


class ControlsSettingsWidget(QWidget):
    """Tab that exposes control bindings similar to game key bindings."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self._editors: Dict[str, KeyBindingListEditor] = {}
        for action in CONTROL_ACTIONS:
            bindings = settings.controls.get(action.key, [])
            editor = KeyBindingListEditor(bindings, self)
            self._editors[action.key] = editor
            layout.addRow(QLabel(action.label, self), editor)

    def to_settings(self, settings: Settings) -> None:
        """Write the user changes back into the provided settings container."""

        controls: Dict[str, List[KeyBinding]] = {}
        for key, editor in self._editors.items():
            controls[key] = editor.bindings()
        settings.controls = controls


class LoggingSettingsWidget(QWidget):
    """Tab that exposes logging configuration."""

    LEVEL_OPTIONS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    def __init__(self, logging_settings: LoggingSettings, parent: QWidget | None = None) -> None:
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


class FeedrateGroupEditor(QWidget):
    """Editor for a single feedrate group including presets and default selection."""

    def __init__(
        self,
        title: str,
        units: str,
        group: FeedrateGroup,
        fallback_presets: Sequence[float],
        fallback_default: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._fallback_presets = [float(value) for value in fallback_presets]
        self._fallback_presets.sort()
        self._fallback_default = float(fallback_default)
        self._presets: List[float] = sorted(group.presets) if group.presets else list(self._fallback_presets)
        if not self._presets:
            self._presets = list(self._fallback_presets)
        self._default_value: float = group.default
        if not self._presets:
            self._default_value = self._fallback_default

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        title_label = QLabel(title, self)
        title_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(title_label)

        units_label = QLabel(f"Preset feed rates for {units} (positive values):", self)
        units_label.setWordWrap(True)
        layout.addWidget(units_label)

        self._list = QListWidget(self)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(self._list)

        input_row = QHBoxLayout()
        self._value_edit = QLineEdit(self)
        self._value_edit.setPlaceholderText("Enter feed rate (e.g. 0.5)")
        validator = QDoubleValidator(0.000001, 1000000.0, 6, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._value_edit.setValidator(validator)
        input_row.addWidget(self._value_edit)

        self._add_button = QPushButton("Add", self)
        input_row.addWidget(self._add_button)
        layout.addLayout(input_row)

        action_row = QHBoxLayout()
        self._remove_button = QPushButton("Remove Selected", self)
        action_row.addWidget(self._remove_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("Default preset:", self))
        self._default_combo = QComboBox(self)
        default_row.addWidget(self._default_combo)
        default_row.addStretch(1)
        layout.addLayout(default_row)

        self._add_button.clicked.connect(self._add_value)
        self._remove_button.clicked.connect(self._remove_selected)
        self._list.itemSelectionChanged.connect(self._update_buttons)
        self._default_combo.currentIndexChanged.connect(self._on_default_changed)

        self._refresh_list()
        self._update_buttons()

    def group(self) -> FeedrateGroup:
        """Return the configured feedrate group."""

        presets = list(self._presets)
        if not presets:
            presets = list(self._fallback_presets)
        default_value = self._default_value
        if default_value <= 0 or all(not math.isclose(default_value, value, rel_tol=1e-9, abs_tol=1e-9) for value in presets):
            default_value = presets[0] if presets else self._fallback_default
        return FeedrateGroup(presets=presets, default=default_value)

    def _refresh_list(self) -> None:
        self._presets.sort()
        self._list.clear()
        for value in self._presets:
            self._list.addItem(self._format_value(value))
        if not any(math.isclose(self._default_value, value, rel_tol=1e-9, abs_tol=1e-9) for value in self._presets):
            if self._presets:
                self._default_value = self._presets[0]
            else:
                self._default_value = self._fallback_default
        self._refresh_default_options()

    def _refresh_default_options(self) -> None:
        values = list(self._presets) if self._presets else list(self._fallback_presets)
        if not values:
            values = [self._fallback_default]
        texts = [self._format_value(value) for value in values]
        desired_text = self._format_value(self._default_value)

        self._default_choices = values
        self._default_combo.blockSignals(True)
        self._default_combo.clear()
        self._default_combo.addItems(texts)
        if desired_text in texts:
            self._default_combo.setCurrentText(desired_text)
        else:
            self._default_combo.setCurrentIndex(0)
            self._default_value = values[0]
        self._default_combo.blockSignals(False)

    def _update_buttons(self) -> None:
        self._remove_button.setEnabled(bool(self._list.selectedItems()))

    def _add_value(self) -> None:
        text = self._value_edit.text().strip()
        if not text:
            return
        try:
            value = float(text)
        except ValueError:
            return
        if value <= 0:
            return
        if any(math.isclose(value, existing, rel_tol=1e-9, abs_tol=1e-9) for existing in self._presets):
            return
        insert_index = len(self._presets)
        for index, existing in enumerate(self._presets):
            if value < existing:
                insert_index = index
                break
        self._presets.insert(insert_index, value)
        self._value_edit.clear()
        self._refresh_list()

    def _remove_selected(self) -> None:
        selected_indexes = self._list.selectedIndexes()
        if not selected_indexes:
            return
        for index in sorted((idx.row() for idx in selected_indexes), reverse=True):
            if 0 <= index < len(self._presets):
                del self._presets[index]
        self._refresh_list()

    def _on_default_changed(self) -> None:
        index = self._default_combo.currentIndex()
        if 0 <= index < len(self._default_choices):
            self._default_value = self._default_choices[index]

    @staticmethod
    def _format_value(value: float) -> str:
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"


class FeedrateSettingsWidget(QWidget):
    """Tab that lets users manage linear feed rates."""

    DEFAULT_PRESETS = (0.01, 0.1, 1.0, 10.0, 100.0)
    DEFAULT_VALUE = 1.0

    def __init__(self, feedrates: FeedrateSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._linear_editor = FeedrateGroupEditor(
            "Linear feed rates",
            "mm/min",
            feedrates.linear,
            self.DEFAULT_PRESETS,
            self.DEFAULT_VALUE,
            self,
        )
        layout.addWidget(self._linear_editor)

        layout.addStretch(1)

    def to_settings(self, settings: Settings) -> None:
        """Write the configured presets back to the settings container."""

        settings.feedrates = FeedrateSettings(
            linear=self._linear_editor.group(),
            rotary=settings.feedrates.rotary,
        )


class JogSettingsWidget(QWidget):
    """Tab that exposes joystick jog distances."""

    def __init__(self, jog_settings: JogSettings, parent: QWidget | None = None) -> None:
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

        self._show_axis_a_checkbox = QCheckBox("Show A-axis controls", self)
        self._show_axis_a_checkbox.setChecked(jog_settings.show_axis_a_controls)
        layout.addRow(self._show_axis_a_checkbox)

        self._show_axis_b_checkbox = QCheckBox("Show B-axis controls", self)
        self._show_axis_b_checkbox.setChecked(jog_settings.show_axis_b_controls)
        layout.addRow(self._show_axis_b_checkbox)

        self._manual_axis_controls_checkbox = QCheckBox(
            "Show manual axis move controls",
            self,
        )
        self._manual_axis_controls_checkbox.setChecked(
            jog_settings.manual_axis_controls_enabled
        )
        layout.addRow(self._manual_axis_controls_checkbox)

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        jog = settings.jog.clone()
        jog.linear_distance_mm = self._linear_distance_spin.value()
        jog.motion_safety_disabled = self._motion_safety_checkbox.isChecked()
        jog.show_axis_a_controls = self._show_axis_a_checkbox.isChecked()
        jog.show_axis_b_controls = self._show_axis_b_checkbox.isChecked()
        jog.manual_axis_controls_enabled = (
            self._manual_axis_controls_checkbox.isChecked()
        )
        settings.jog = jog


class NeedleCalibrationSettingsWidget(QWidget):
    """Tab that exposes LCR and needle calibration settings."""

    def __init__(
        self,
        calibration_settings: NeedleCalibrationSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._chip_position = calibration_settings.chip_position.clone()
        self._stone_position = calibration_settings.stone_position.clone()
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._visa_resource_edit = QLineEdit(self)
        self._visa_resource_edit.setPlaceholderText("COM4 or ASRL4::INSTR")
        self._visa_resource_edit.setText(calibration_settings.visa_resource)
        layout.addRow(QLabel("LCR resource", self), self._visa_resource_edit)

        self._function_combo = QComboBox(self)
        self._function_combo.addItems(LCR_MEASUREMENT_FUNCTIONS)
        self._function_combo.setCurrentText(calibration_settings.measurement_function)
        layout.addRow(QLabel("Measurement function", self), self._function_combo)

        self._range_mode_combo = QComboBox(self)
        for mode in LCR_RANGE_MODES:
            label = "Fixed range (HOLD)" if mode == "HOLD" else "Auto range"
            self._range_mode_combo.addItem(label, mode)
        range_index = self._range_mode_combo.findData(calibration_settings.range_mode)
        if range_index >= 0:
            self._range_mode_combo.setCurrentIndex(range_index)
        layout.addRow(QLabel("Range mode", self), self._range_mode_combo)

        self._impedance_range_spin = QSpinBox(self)
        self._impedance_range_spin.setRange(0, 8)
        self._impedance_range_spin.setSingleStep(1)
        self._impedance_range_spin.setValue(calibration_settings.impedance_range)
        layout.addRow(QLabel("Impedance range", self), self._impedance_range_spin)

        self._dcr_range_spin = QSpinBox(self)
        self._dcr_range_spin.setRange(0, 8)
        self._dcr_range_spin.setSingleStep(1)
        self._dcr_range_spin.setValue(calibration_settings.dcr_range)
        layout.addRow(QLabel("DCR range", self), self._dcr_range_spin)

        self._frequency_spin = QDoubleSpinBox(self)
        self._frequency_spin.setDecimals(3)
        self._frequency_spin.setRange(10.0, 300_000.0)
        self._frequency_spin.setSingleStep(100.0)
        self._frequency_spin.setSuffix(" Hz")
        self._frequency_spin.setValue(calibration_settings.frequency_hz)
        layout.addRow(QLabel("AC frequency", self), self._frequency_spin)

        self._level_mode_combo = QComboBox(self)
        self._level_mode_combo.addItem("Voltage", "VOLTAGE")
        self._level_mode_combo.addItem("Current", "CURRENT")
        level_mode_index = self._level_mode_combo.findData(
            calibration_settings.level_mode
        )
        if level_mode_index >= 0:
            self._level_mode_combo.setCurrentIndex(level_mode_index)
        layout.addRow(QLabel("AC level mode", self), self._level_mode_combo)

        self._voltage_level_spin = QDoubleSpinBox(self)
        self._voltage_level_spin.setDecimals(4)
        self._voltage_level_spin.setRange(0.01, 2.0)
        self._voltage_level_spin.setSingleStep(0.01)
        self._voltage_level_spin.setSuffix(" V")
        self._voltage_level_spin.setValue(calibration_settings.voltage_level_v)
        layout.addRow(QLabel("AC voltage level", self), self._voltage_level_spin)

        self._current_level_spin = QDoubleSpinBox(self)
        self._current_level_spin.setDecimals(6)
        self._current_level_spin.setRange(0.0001, 0.02)
        self._current_level_spin.setSingleStep(0.0001)
        self._current_level_spin.setSuffix(" A")
        self._current_level_spin.setValue(calibration_settings.current_level_a)
        layout.addRow(QLabel("AC current level", self), self._current_level_spin)

        self._source_resistance_combo = QComboBox(self)
        for resistance_ohm in LCR_SOURCE_RESISTANCES_OHM:
            self._source_resistance_combo.addItem(f"{resistance_ohm} ohm", resistance_ohm)
        source_index = self._source_resistance_combo.findData(
            calibration_settings.source_resistance_ohm
        )
        if source_index >= 0:
            self._source_resistance_combo.setCurrentIndex(source_index)
        layout.addRow(QLabel("Source resistance", self), self._source_resistance_combo)

        self._aperture_combo = QComboBox(self)
        self._aperture_combo.addItems(LCR_APERTURE_RATES)
        self._aperture_combo.setCurrentText(calibration_settings.aperture_rate)
        layout.addRow(QLabel("Measurement speed", self), self._aperture_combo)

        self._averages_spin = QSpinBox(self)
        self._averages_spin.setRange(1, 256)
        self._averages_spin.setSingleStep(1)
        self._averages_spin.setValue(calibration_settings.aperture_averages)
        layout.addRow(QLabel("Averaging factor", self), self._averages_spin)

        self._trigger_source_combo = QComboBox(self)
        self._trigger_source_combo.addItems(LCR_TRIGGER_SOURCES)
        self._trigger_source_combo.setCurrentText(calibration_settings.trigger_source)
        layout.addRow(QLabel("Trigger source", self), self._trigger_source_combo)

        self._trigger_delay_spin = QDoubleSpinBox(self)
        self._trigger_delay_spin.setDecimals(3)
        self._trigger_delay_spin.setRange(0.0, 60.0)
        self._trigger_delay_spin.setSingleStep(0.01)
        self._trigger_delay_spin.setSuffix(" s")
        self._trigger_delay_spin.setValue(calibration_settings.trigger_delay_s)
        layout.addRow(QLabel("Trigger delay", self), self._trigger_delay_spin)

        self._bias_checkbox = QCheckBox("Enable DC bias", self)
        self._bias_checkbox.setChecked(calibration_settings.bias_enabled)
        layout.addRow(self._bias_checkbox)

        self._bias_level_spin = QDoubleSpinBox(self)
        self._bias_level_spin.setDecimals(3)
        self._bias_level_spin.setRange(-2.5, 2.5)
        self._bias_level_spin.setSingleStep(0.01)
        self._bias_level_spin.setSuffix(" V")
        self._bias_level_spin.setValue(calibration_settings.bias_level_v)
        layout.addRow(QLabel("DC bias level", self), self._bias_level_spin)

        self._monitor1_combo = QComboBox(self)
        self._monitor1_combo.addItems(LCR_MONITOR_PARAMETERS)
        self._monitor1_combo.setCurrentText(calibration_settings.monitor1)
        layout.addRow(QLabel("Monitor 1", self), self._monitor1_combo)

        self._monitor2_combo = QComboBox(self)
        self._monitor2_combo.addItems(LCR_MONITOR_PARAMETERS)
        self._monitor2_combo.setCurrentText(calibration_settings.monitor2)
        layout.addRow(QLabel("Monitor 2", self), self._monitor2_combo)

        self._alc_checkbox = QCheckBox("Enable ALC", self)
        self._alc_checkbox.setChecked(calibration_settings.alc_enabled)
        layout.addRow(self._alc_checkbox)

        self._short_threshold_spin = QDoubleSpinBox(self)
        self._short_threshold_spin.setDecimals(3)
        self._short_threshold_spin.setRange(0.0, 1_000_000.0)
        self._short_threshold_spin.setSingleStep(0.5)
        self._short_threshold_spin.setSuffix(" ohm")
        self._short_threshold_spin.setValue(calibration_settings.short_threshold_ohm)
        layout.addRow(QLabel("Short threshold", self), self._short_threshold_spin)

        self._poll_interval_spin = QDoubleSpinBox(self)
        self._poll_interval_spin.setDecimals(0)
        self._poll_interval_spin.setRange(50, 10_000)
        self._poll_interval_spin.setSingleStep(50)
        self._poll_interval_spin.setSuffix(" ms")
        self._poll_interval_spin.setValue(calibration_settings.poll_interval_ms)
        layout.addRow(QLabel("Polling interval", self), self._poll_interval_spin)

        self._configured_checkbox = QCheckBox("Calibrated down height is configured", self)
        self._configured_checkbox.setChecked(
            calibration_settings.down_position_configured
        )
        layout.addRow(self._configured_checkbox)

        self._down_position_spin = QDoubleSpinBox(self)
        self._down_position_spin.setDecimals(4)
        self._down_position_spin.setRange(-1000.0, 1000.0)
        self._down_position_spin.setSingleStep(0.01)
        self._down_position_spin.setSuffix(" mm")
        self._down_position_spin.setValue(calibration_settings.down_position_mm)
        layout.addRow(QLabel("Calibrated down A position", self), self._down_position_spin)

        self._function_combo.currentTextChanged.connect(
            lambda _text: self._update_lcr_control_state()
        )
        self._range_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_lcr_control_state()
        )
        self._level_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_lcr_control_state()
        )
        self._bias_checkbox.toggled.connect(
            lambda _checked: self._update_lcr_control_state()
        )
        self._update_lcr_control_state()

    def _update_lcr_control_state(self) -> None:
        function = self._function_combo.currentText()
        range_mode = str(self._range_mode_combo.currentData() or "HOLD")
        level_mode = str(self._level_mode_combo.currentData() or "VOLTAGE")
        fixed_range = range_mode == "HOLD"
        dcr_mode = function == "DCR"
        self._dcr_range_spin.setEnabled(fixed_range and dcr_mode)
        self._impedance_range_spin.setEnabled(fixed_range and not dcr_mode)
        self._frequency_spin.setEnabled(not dcr_mode)
        self._level_mode_combo.setEnabled(not dcr_mode)
        self._voltage_level_spin.setEnabled(not dcr_mode and level_mode == "VOLTAGE")
        self._current_level_spin.setEnabled(not dcr_mode and level_mode == "CURRENT")
        self._source_resistance_combo.setEnabled(not dcr_mode)
        self._bias_checkbox.setEnabled(not dcr_mode)
        self._bias_level_spin.setEnabled(not dcr_mode and self._bias_checkbox.isChecked())
        self._monitor1_combo.setEnabled(not dcr_mode)
        self._monitor2_combo.setEnabled(not dcr_mode)
        self._alc_checkbox.setEnabled(not dcr_mode)

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        settings.needle_calibration = NeedleCalibrationSettings(
            visa_resource=self._visa_resource_edit.text().strip(),
            measurement_function=self._function_combo.currentText(),
            range_mode=str(self._range_mode_combo.currentData() or "HOLD"),
            auto_range_enabled=str(self._range_mode_combo.currentData() or "") == "AUTO",
            impedance_range=int(self._impedance_range_spin.value()),
            dcr_range=int(self._dcr_range_spin.value()),
            frequency_hz=self._frequency_spin.value(),
            level_mode=str(self._level_mode_combo.currentData() or "VOLTAGE"),
            voltage_level_v=self._voltage_level_spin.value(),
            current_level_a=self._current_level_spin.value(),
            source_resistance_ohm=int(
                self._source_resistance_combo.currentData() or 30
            ),
            aperture_rate=self._aperture_combo.currentText(),
            aperture_averages=int(self._averages_spin.value()),
            trigger_source=self._trigger_source_combo.currentText(),
            trigger_delay_s=self._trigger_delay_spin.value(),
            bias_enabled=self._bias_checkbox.isChecked(),
            bias_level_v=self._bias_level_spin.value(),
            monitor1=self._monitor1_combo.currentText(),
            monitor2=self._monitor2_combo.currentText(),
            alc_enabled=self._alc_checkbox.isChecked(),
            short_threshold_ohm=self._short_threshold_spin.value(),
            poll_interval_ms=int(self._poll_interval_spin.value()),
            down_position_mm=self._down_position_spin.value(),
            down_position_configured=self._configured_checkbox.isChecked(),
            chip_position=self._chip_position.clone(),
            stone_position=self._stone_position.clone(),
        )


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
        self._startup_mode_combo.addItem(
            "Force selected system on connect", "fixed"
        )
        mode_index = self._startup_mode_combo.findData(
            coordinate_settings.startup_mode
        )
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
        fixed_mode = str(self._startup_mode_combo.currentData() or "") == "fixed"
        machine_mode = str(self._position_mode_combo.currentData() or "") == "machine"
        self._preferred_system_combo.setEnabled(not machine_mode)
        if machine_mode:
            self._preferred_system_combo.setToolTip(
                "Unused in absolute machine-coordinate mode."
            )
            return
        if fixed_mode:
            self._preferred_system_combo.setToolTip(
                "This WCS will be sent to the controller on connect."
            )
            return
        self._preferred_system_combo.setToolTip(
            "Controller-selected WCS will be used."
        )


class SettingsDialog(QDialog):
    """Main settings dialog with tabbed sections."""

    settings_applied = Signal(object)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._settings = settings.clone()
        self._applied_once = False

        root_layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        root_layout.addWidget(self._tabs)

        self._controls_tab = ControlsSettingsWidget(self._settings, self)
        self._logging_tab = LoggingSettingsWidget(self._settings.logging, self)
        self._jog_tab = JogSettingsWidget(self._settings.jog, self)
        self._needle_calibration_tab = NeedleCalibrationSettingsWidget(
            self._settings.needle_calibration, self
        )
        self._coordinate_system_tab = CoordinateSystemSettingsWidget(
            self._settings.coordinate_system, self
        )
        self._tabs.addTab(self._controls_tab, "Controls")
        self._tabs.addTab(self._jog_tab, "Jog")
        self._tabs.addTab(self._coordinate_system_tab, "Coordinates")
        self._tabs.addTab(self._needle_calibration_tab, "Needles")
        self._tabs.addTab(self._logging_tab, "Logging")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Apply | QDialogButtonBox.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        apply_button = buttons.button(QDialogButtonBox.Apply)
        if apply_button is not None:
            apply_button.clicked.connect(self._apply_without_closing)
        root_layout.addWidget(buttons)

    def accept(self) -> None:  # type: ignore[override]
        self._collect_settings()
        self._applied_once = True
        self.settings_applied.emit(self._settings.clone())
        super().accept()

    def _apply_without_closing(self) -> None:
        self._collect_settings()
        self._applied_once = True
        self.settings_applied.emit(self._settings.clone())

    def _collect_settings(self) -> None:
        self._controls_tab.to_settings(self._settings)
        self._jog_tab.to_settings(self._settings)
        self._coordinate_system_tab.to_settings(self._settings)
        self._needle_calibration_tab.to_settings(self._settings)
        self._logging_tab.to_settings(self._settings.logging)

    def result_settings(self) -> Settings:
        """Return a clone of the adjusted settings."""

        return self._settings.clone()

    def was_applied(self) -> bool:
        """Return True when settings were applied at least once."""

        return self._applied_once

