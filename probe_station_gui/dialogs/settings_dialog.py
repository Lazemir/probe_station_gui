"""Dialog for configuring application settings."""

from __future__ import annotations

from typing import Dict, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.settings_manager import CONTROL_ACTIONS, KeyBinding, Settings


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

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        key = event.key()
        if key in (Qt.Key_Escape, Qt.Key_Cancel):
            self.reject()
            return
        self._binding = KeyBinding(
            qt_key=int(key),
            modifiers=int(event.modifiers()),
            text=event.text(),
        )
        self.accept()

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
        self._tabs.addTab(self._controls_tab, "Controls")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root_layout.addWidget(buttons)

    def accept(self) -> None:  # type: ignore[override]
        self._controls_tab.to_settings(self._settings)
        super().accept()

    def result_settings(self) -> Settings:
        """Return a clone of the adjusted settings."""

        return self._settings.clone()

