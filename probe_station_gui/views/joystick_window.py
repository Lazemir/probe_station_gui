"""Interactive joystick window for jogging the stage via serial commands."""

from __future__ import annotations

from typing import Optional, Tuple

import serial
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class JoystickWindow(QMainWindow):
    """Floating window that provides directional jogging controls."""

    JOG_DISTANCE_MM = 10.0
    FEED_RATES = ["30", "60", "90", "120", "180", "Custom..."]

    KEY_DIRECTION_MAP = {
        Qt.Key_Up: ("Y", 1),
        Qt.Key_W: ("Y", 1),
        Qt.Key_Down: ("Y", -1),
        Qt.Key_S: ("Y", -1),
        Qt.Key_Left: ("X", -1),
        Qt.Key_A: ("X", -1),
        Qt.Key_Right: ("X", 1),
        Qt.Key_D: ("X", 1),
    }

    CHAR_DIRECTION_MAP = {
        "w": ("Y", 1),
        "s": ("Y", -1),
        "a": ("X", -1),
        "d": ("X", 1),
        "ц": ("Y", 1),
        "ы": ("Y", -1),
        "ф": ("X", -1),
        "в": ("X", 1),
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Joystick Controls")
        self.setFocusPolicy(Qt.StrongFocus)

        self.serial_connection: Optional[serial.Serial] = None
        self._active_direction: Optional[tuple[str, int]] = None
        self._key_stack: list[Tuple[str, object]] = []

        central = QWidget(self)
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
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

        self.up_button.pressed.connect(lambda: self.start_jog("Y", 1))
        self.up_button.released.connect(self.stop_jog)
        self.down_button.pressed.connect(lambda: self.start_jog("Y", -1))
        self.down_button.released.connect(self.stop_jog)
        self.left_button.pressed.connect(lambda: self.start_jog("X", -1))
        self.left_button.released.connect(self.stop_jog)
        self.right_button.pressed.connect(lambda: self.start_jog("X", 1))
        self.right_button.released.connect(self.stop_jog)

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

        self.serial_connection = serial_connection
        if not serial_connection or not serial_connection.is_open:
            self._active_direction = None
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
        if not self.serial_connection or not self.serial_connection.is_open:
            return
        feedrate = self.get_feedrate()
        if feedrate is None:
            return
        if self._active_direction == (axis, direction):
            return
        self._active_direction = (axis, direction)
        distance = direction * self.JOG_DISTANCE_MM
        command = f"$J=G91 G21 {axis}{distance:.3f} F{feedrate}\n"
        self.send_command(command)

    def stop_jog(self) -> None:
        if not self.serial_connection or not self.serial_connection.is_open:
            self._active_direction = None
            return
        if self._active_direction is None:
            return
        self._active_direction = None
        self.send_command(b"\x85")

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
                self.start_jog(*mapping)
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
            self.stop_jog()
            if self._key_stack:
                next_identifier = self._key_stack[-1]
                next_mapping = self._mapping_from_identifier(next_identifier)
                if next_mapping:
                    self.start_jog(*next_mapping)
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
        mapping = self.KEY_DIRECTION_MAP.get(key)
        if mapping:
            return ("key", key), mapping

        text = event.text()
        if text:
            normalized = text.casefold()
            mapping = self.CHAR_DIRECTION_MAP.get(normalized)
            if mapping:
                return ("char", normalized), mapping

        if 0 < key <= 0x10FFFF:
            normalized = chr(key).casefold()
            mapping = self.CHAR_DIRECTION_MAP.get(normalized)
            if mapping:
                return ("charcode", normalized), mapping

        return (None, None)

    def _mapping_from_identifier(self, identifier: Tuple[str, object]) -> Optional[tuple[str, int]]:
        kind, value = identifier
        if kind == "key":
            return self.KEY_DIRECTION_MAP.get(value)  # type: ignore[arg-type]
        if kind in {"char", "charcode"}:
            return self.CHAR_DIRECTION_MAP.get(value)  # type: ignore[arg-type]
        return None
