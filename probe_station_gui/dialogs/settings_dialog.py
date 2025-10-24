"""Dialog for configuring application settings."""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, cast

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.qt_compat import keyboard_modifiers_to_int
from probe_station_gui.settings_manager import (
    CONTROL_ACTIONS,
    FeedrateGroup,
    FeedrateSettings,
    KeyBinding,
    LoggingSettings,
    Settings,
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
            return sequence_text
        if binding.text:
            return binding.text
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

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        group_box = QGroupBox(title, self)
        outer_layout.addWidget(group_box)
        layout = QVBoxLayout(group_box)

        layout.addWidget(QLabel(f"Preset feed rates for {units} (positive values):", self))

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
    """Tab that lets users manage linear and rotary feed rates."""

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

        self._rotary_editor = FeedrateGroupEditor(
            "Rotary feed rates",
            "deg/min",
            feedrates.rotary,
            self.DEFAULT_PRESETS,
            self.DEFAULT_VALUE,
            self,
        )
        layout.addWidget(self._rotary_editor)
        layout.addStretch(1)

    def to_settings(self, settings: Settings) -> None:
        """Write the configured presets back to the settings container."""

        settings.feedrates = FeedrateSettings(
            linear=self._linear_editor.group(),
            rotary=self._rotary_editor.group(),
        )

class SettingsDialog(QDialog):
    """Main settings dialog with tabbed sections."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._settings = settings.clone()

        root_layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        root_layout.addWidget(self._tabs)

        self._controls_tab = ControlsSettingsWidget(self._settings, self)
        self._logging_tab = LoggingSettingsWidget(self._settings.logging, self)
        self._feedrate_tab = FeedrateSettingsWidget(self._settings.feedrates, self)
        self._tabs.addTab(self._controls_tab, "Controls")
        self._tabs.addTab(self._feedrate_tab, "Feedrates")
        self._tabs.addTab(self._logging_tab, "Logging")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root_layout.addWidget(buttons)

    def accept(self) -> None:  # type: ignore[override]
        self._controls_tab.to_settings(self._settings)
        self._feedrate_tab.to_settings(self._settings)
        self._logging_tab.to_settings(self._settings.logging)
        super().accept()

    def result_settings(self) -> Settings:
        """Return a clone of the adjusted settings."""

        return self._settings.clone()

