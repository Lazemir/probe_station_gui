"""Interactive joystick window for jogging the stage via serial commands."""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import serial
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.settings_manager import CONTROL_ACTIONS, KeyBinding


class JoystickWindow(QWidget):
    """Widget that provides directional jogging controls."""

    JOG_DISTANCE_MM = 10.0
    ROTATE_DISTANCE_DEG = 5.0
    FEED_RATES = ["30", "60", "90", "120", "180", "Custom..."]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

        self.serial_connection: Optional[serial.Serial] = None
        self._active_axes: Optional[tuple[tuple[str, int], ...]] = None
        self._key_stack: list[Tuple[str, object]] = []
        self._key_bindings: Dict[tuple, tuple[str, int]] = {}
        self.apply_control_bindings({})

        root_layout = QVBoxLayout(self)
        self.status_label = QLabel("Disconnected", self)
        root_layout.addWidget(self.status_label)

        feed_layout = QHBoxLayout()
        feed_layout.addWidget(QLabel("Feed rate (mm/min):", self))
        self.feedrate_combo = QComboBox(self)
        self.feedrate_combo.addItems(self.FEED_RATES)
        self.feedrate_combo.currentIndexChanged.connect(self._on_feedrate_changed)
        feed_layout.addWidget(self.feedrate_combo)

        self.custom_feedrate_edit = QLineEdit(self)
        self.custom_feedrate_edit.setPlaceholderText("Enter custom rate")
        self.custom_feedrate_edit.setValidator(QDoubleValidator(0.1, 10000.0, 2, self))
        self.custom_feedrate_edit.setVisible(False)
        feed_layout.addWidget(self.custom_feedrate_edit)

        root_layout.addLayout(feed_layout)

        grid_layout = QGridLayout()
        self.up_button = QPushButton("↑", self)
        self.left_button = QPushButton("←", self)
        self.right_button = QPushButton("→", self)
        self.down_button = QPushButton("↓", self)

        grid_layout.addWidget(self.up_button, 0, 1)
        grid_layout.addWidget(self.left_button, 1, 0)
        grid_layout.addWidget(self.right_button, 1, 2)
        grid_layout.addWidget(self.down_button, 2, 1)

        root_layout.addLayout(grid_layout)

        rotate_layout = QHBoxLayout()
        rotate_layout.addStretch(1)
        self.rotate_ccw_button = QPushButton("⟲", self)
        self.rotate_cw_button = QPushButton("⟳", self)
        rotate_layout.addWidget(QLabel("Rotate B:", self))
        rotate_layout.addWidget(self.rotate_ccw_button)
        rotate_layout.addWidget(self.rotate_cw_button)
        rotate_layout.addStretch(1)
        root_layout.addLayout(rotate_layout)

        self.up_button.pressed.connect(lambda: self.start_jog("Y", 1))
        self.up_button.released.connect(self.stop_jog)
        self.down_button.pressed.connect(lambda: self.start_jog("Y", -1))
        self.down_button.released.connect(self.stop_jog)
        self.left_button.pressed.connect(lambda: self.start_jog("X", -1))
        self.left_button.released.connect(self.stop_jog)
        self.right_button.pressed.connect(lambda: self.start_jog("X", 1))
        self.right_button.released.connect(self.stop_jog)
        self.rotate_ccw_button.pressed.connect(lambda: self.start_jog("B", -1))
        self.rotate_ccw_button.released.connect(self.stop_jog)
        self.rotate_cw_button.pressed.connect(lambda: self.start_jog("B", 1))
        self.rotate_cw_button.released.connect(self.stop_jog)

        home_layout = QHBoxLayout()
        self.home_all_button = QPushButton("Home All", self)
        self.home_xy_button = QPushButton("Home XY", self)
        self.home_z_button = QPushButton("Home Z", self)
        home_layout.addWidget(self.home_all_button)
        home_layout.addWidget(self.home_xy_button)
        home_layout.addWidget(self.home_z_button)
        root_layout.addLayout(home_layout)

        safety_layout = QHBoxLayout()
        self.unlock_button = QPushButton("Unlock", self)
        self.reset_button = QPushButton("Reset", self)
        safety_layout.addWidget(self.unlock_button)
        safety_layout.addWidget(self.reset_button)
        root_layout.addLayout(safety_layout)

        self.home_all_button.clicked.connect(lambda: self.send_command("$H\n"))
        self.home_xy_button.clicked.connect(self._home_xy)
        self.home_z_button.clicked.connect(lambda: self.send_command("$HZ\n"))
        self.unlock_button.clicked.connect(lambda: self.send_command("$X\n"))
        self.reset_button.clicked.connect(self._send_reset)

        root_layout.addStretch(1)
        self._update_enabled_state()

    def _on_feedrate_changed(self, index: int) -> None:
        is_custom = self.feedrate_combo.itemText(index) == "Custom..."
        self.custom_feedrate_edit.setVisible(is_custom)
        if is_custom:
            self.custom_feedrate_edit.setFocus()

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Assign the serial connection used for jogging commands."""

        if self.serial_connection and self.serial_connection.is_open:
            self.stop_jog()
        self.serial_connection = serial_connection
        if not serial_connection or not serial_connection.is_open:
            self._active_axes = None
            self._key_stack.clear()
        if serial_connection and serial_connection.is_open:
            self.status_label.setText(
                f"Connected to {serial_connection.port} @ {serial_connection.baudrate}"
            )
        else:
            self.status_label.setText("Disconnected")
        self._update_enabled_state()

    def _update_enabled_state(self) -> None:
        enabled = bool(self.serial_connection and self.serial_connection.is_open)
        for widget in (
            self.feedrate_combo,
            self.custom_feedrate_edit,
            self.up_button,
            self.down_button,
            self.left_button,
            self.right_button,
            self.rotate_ccw_button,
            self.rotate_cw_button,
            self.home_all_button,
            self.home_xy_button,
            self.home_z_button,
            self.unlock_button,
            self.reset_button,
        ):
            widget.setEnabled(enabled)

    def get_feedrate(self) -> Optional[float]:
        text = self.feedrate_combo.currentText()
        if text == "Custom...":
            text = self.custom_feedrate_edit.text().strip()
            if not text:
                self._show_warning("Please enter a custom feed rate.")
                return None
        try:
            value = float(text)
            if value <= 0:
                raise ValueError
            return value
        except ValueError:
            self._show_warning("Feed rate must be a positive number.")
            return None

    def start_jog(self, axis: str, direction: int) -> None:
        self._apply_axes(((axis, direction),))

    def stop_jog(self) -> None:
        if not self.serial_connection or not self.serial_connection.is_open:
            self._active_axes = None
            return
        if self._active_axes is None:
            return
        self._active_axes = None
        self.send_command(b"\x85")

    def _apply_axes(self, axes: tuple[tuple[str, int], ...]) -> None:
        axes_sorted = tuple(sorted(axes, key=lambda item: item[0]))
        if not axes_sorted:
            self.stop_jog()
            return
        if self._active_axes == axes_sorted:
            return
        if not self.serial_connection or not self.serial_connection.is_open:
            self._active_axes = None
            return
        feedrate = self.get_feedrate()
        if feedrate is None:
            self.stop_jog()
            return
        if self._active_axes is not None:
            self.stop_jog()
        parts: list[str] = []
        for axis, direction in axes_sorted:
            distance = direction * self._distance_for_axis(axis)
            parts.append(f"{axis}{distance:.3f}")
        command = f"$J=G91 G21 {' '.join(parts)} F{feedrate}\n"
        self.send_command(command)
        self._active_axes = axes_sorted

    def _distance_for_axis(self, axis: str) -> float:
        if axis == "B":
            return self.ROTATE_DISTANCE_DEG
        return self.JOG_DISTANCE_MM

    def _update_active_jog(self) -> None:
        unique_axes: dict[str, int] = {}
        for identifier in self._key_stack:
            mapping = self._mapping_from_identifier(identifier)
            if mapping is None:
                continue
            axis, direction = mapping
            unique_axes[axis] = direction
        axes = tuple(unique_axes.items())
        self._apply_axes(axes)

    def _home_xy(self) -> None:
        self.send_command("$HX\n")
        self.send_command("$HY\n")

    def _send_reset(self) -> None:
        self.send_command(b"\x18")

    def send_command(self, command: str | bytes) -> None:
        if not self.serial_connection or not self.serial_connection.is_open:
            return
        try:
            data = command if isinstance(command, bytes) else command.encode("ascii")
            self.serial_connection.write(data)
            self.serial_connection.flush()
        except serial.SerialException as error:  # pragma: no cover - best effort guard
            self._show_warning(f"Serial communication error: {error}")
            self.set_serial(None)

    def _show_warning(self, message: str) -> None:
        QMessageBox.warning(self, "Joystick", message)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.isAutoRepeat():
            event.ignore()
            return
        identifier, mapping = self._mapping_from_event(event)
        if identifier and mapping:
            if identifier not in self._key_stack:
                self._key_stack.append(identifier)
                self._update_active_jog()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.isAutoRepeat():
            event.ignore()
            return
        identifier, mapping = self._mapping_from_event(event)
        if identifier and mapping:
            if identifier in self._key_stack:
                self._key_stack.remove(identifier)
                self._update_active_jog()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        self._key_stack.clear()
        self.stop_jog()
        super().focusOutEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._key_stack.clear()
        self.stop_jog()
        super().closeEvent(event)

    def _mapping_from_event(
        self, event
    ) -> tuple[Optional[Tuple[str, object]], Optional[tuple[str, int]]]:
        key = event.key()
        modifiers = int(event.modifiers())
        mapping = self._key_bindings.get(("key", key, modifiers))
        if mapping:
            return ("key", (key, modifiers)), mapping

        text = event.text()
        if text:
            normalized = text.casefold()
            mapping = self._key_bindings.get(("text", normalized))
            if mapping:
                return ("text", normalized), mapping

        for identifier, mapping in self._key_bindings.items():
            if identifier[0] == "key" and identifier[1] == key:
                return ("key", (identifier[1], identifier[2])), mapping

        return (None, None)

    def _mapping_from_identifier(self, identifier: Tuple[str, object]) -> Optional[tuple[str, int]]:
        kind, value = identifier
        if kind == "key":
            key, modifiers = value  # type: ignore[misc]
            return self._key_bindings.get(("key", key, modifiers))
        if kind == "text":
            return self._key_bindings.get(("text", value))
        return None

    def apply_control_bindings(self, bindings: Dict[str, list[KeyBinding]]) -> None:
        """Update the joystick key map based on the provided settings."""

        mapping: Dict[tuple, tuple[str, int]] = {}
        for action in CONTROL_ACTIONS:
            for binding in bindings.get(action.key, []):
                mapping[("key", binding.qt_key, binding.modifiers)] = (
                    action.axis,
                    action.direction,
                )
                if binding.text:
                    mapping[("text", binding.text.casefold())] = (
                        action.axis,
                        action.direction,
                    )
        self._key_bindings = mapping
