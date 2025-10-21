"""Interactive joystick window for jogging the stage via serial commands."""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import serial
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QDoubleValidator, QKeyEvent
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
    RELEASE_SETTLE_MS = 120

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
        self._active_inputs: Dict[str, Tuple[str, int]] = {}
        self._current_axes: Dict[str, int] = {}
        self._last_feedrate: Optional[float] = None
        self._release_timer = QTimer(self)
        self._release_timer.setSingleShot(True)
        self._release_timer.timeout.connect(self._flush_motion_update)
        self._update_pending = False

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

        for button in (
            self.up_button,
            self.left_button,
            self.right_button,
            self.down_button,
        ):
            button.setFocusPolicy(Qt.NoFocus)

        grid_layout.addWidget(self.up_button, 0, 1)
        grid_layout.addWidget(self.left_button, 1, 0)
        grid_layout.addWidget(self.right_button, 1, 2)
        grid_layout.addWidget(self.down_button, 2, 1)

        root_layout.addLayout(grid_layout)

        self._button_lookup = {
            "button_up": self.up_button,
            "button_down": self.down_button,
            "button_left": self.left_button,
            "button_right": self.right_button,
        }

        self.up_button.pressed.connect(lambda: self._handle_press("button_up", "Y", 1))
        self.up_button.released.connect(lambda: self._handle_release("button_up"))
        self.down_button.pressed.connect(lambda: self._handle_press("button_down", "Y", -1))
        self.down_button.released.connect(lambda: self._handle_release("button_down"))
        self.left_button.pressed.connect(lambda: self._handle_press("button_left", "X", -1))
        self.left_button.released.connect(lambda: self._handle_release("button_left"))
        self.right_button.pressed.connect(lambda: self._handle_press("button_right", "X", 1))
        self.right_button.released.connect(lambda: self._handle_release("button_right"))

        home_layout = QHBoxLayout()
        self.home_all_button = QPushButton("Home All", self)
        self.home_xy_button = QPushButton("Home XY", self)
        self.home_z_button = QPushButton("Home Z", self)
        home_layout.addWidget(self.home_all_button)
        home_layout.addWidget(self.home_xy_button)
        home_layout.addWidget(self.home_z_button)
        root_layout.addLayout(home_layout)

        utility_layout = QHBoxLayout()
        self.unlock_button = QPushButton("Unlock", self)
        self.reset_button = QPushButton("Reset", self)
        utility_layout.addWidget(self.unlock_button)
        utility_layout.addWidget(self.reset_button)
        root_layout.addLayout(utility_layout)

        self.home_all_button.clicked.connect(lambda: self.send_command("$H\n"))
        self.home_xy_button.clicked.connect(self._home_xy)
        self.home_z_button.clicked.connect(lambda: self.send_command("$HZ\n"))
        self.unlock_button.clicked.connect(lambda: self.send_command("$X\n"))
        self.reset_button.clicked.connect(lambda: self.send_command(b"\x18"))

        root_layout.addStretch(1)
        self._update_enabled_state()

    def _on_feedrate_changed(self, index: int) -> None:
        is_custom = self.feedrate_combo.itemText(index) == "Custom..."
        self.custom_feedrate_edit.setVisible(is_custom)
        if is_custom:
            self.custom_feedrate_edit.setFocus()

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Assign the serial connection used for jogging commands."""

        previous_connection = self.serial_connection
        previous_active = bool(self._current_axes)

        self.serial_connection = serial_connection

        if (not serial_connection or not serial_connection.is_open) and previous_connection:
            if previous_connection.is_open and previous_active:
                try:
                    previous_connection.write(b"\x85")
                    previous_connection.flush()
                except serial.SerialException:
                    pass
            self._active_inputs.clear()
            self._current_axes.clear()

        if serial_connection and serial_connection.is_open:
            self.status_label.setText(
                f"Connected to {serial_connection.port} @ {serial_connection.baudrate}"
            )
        else:
            self.status_label.setText("Disconnected")
            self._last_feedrate = None

        self._update_enabled_state()
        self._update_jog_motion()

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

    def _handle_press(
        self,
        identifier: str,
        axis: str,
        direction: int,
        *,
        auto_repeat: bool = False,
    ) -> None:
        if auto_repeat:
            return
        self._active_inputs[identifier] = (axis, direction)
        if not self.serial_connection or not self.serial_connection.is_open:
            return
        if self._release_timer.isActive():
            self._release_timer.stop()
            self._update_pending = False
        feedrate = self.get_feedrate()
        if feedrate is None:
            return
        self._last_feedrate = feedrate
        self._update_jog_motion()

    def _handle_release(
        self,
        identifier: Optional[str],
        mapping: Optional[Tuple[str, int]] = None,
    ) -> None:
        target = identifier
        if target is None and mapping is not None:
            for key, value in self._active_inputs.items():
                if value == mapping:
                    target = key
                    break
        if target is None:
            return
        removed = self._active_inputs.pop(target, None)
        if removed is None:
            return
        if self._release_timer.isActive():
            self._release_timer.stop()
            self._update_pending = False
        if self._current_axes:
            self._stop_current_motion()
        if self._active_inputs:
            self._schedule_motion_update()
        else:
            self._current_axes.clear()

    def _schedule_motion_update(self) -> None:
        if self._release_timer.isActive():
            self._release_timer.stop()
        self._update_pending = True
        self._release_timer.start(self.RELEASE_SETTLE_MS)

    def _flush_motion_update(self) -> None:
        self._update_pending = False
        self._update_jog_motion()

    def _stop_current_motion(self) -> None:
        if not self.serial_connection or not self.serial_connection.is_open:
            return
        self.send_command(b"\x85")

    def _calculate_axes(self) -> Dict[str, int]:
        self._prune_button_inputs()
        axes: Dict[str, set[int]] = {}
        for axis, direction in self._active_inputs.values():
            axes.setdefault(axis, set()).add(direction)
        resolved: Dict[str, int] = {}
        for axis, directions in axes.items():
            if len(directions) == 1:
                resolved[axis] = next(iter(directions))
        return resolved

    def _update_jog_motion(self) -> None:
        if not self.serial_connection or not self.serial_connection.is_open:
            if self._current_axes:
                self._stop_current_motion()
                self._current_axes.clear()
            if self._release_timer.isActive():
                self._release_timer.stop()
                self._update_pending = False
            return

        axes = self._calculate_axes()
        if axes == self._current_axes:
            return

        if self._current_axes:
            self._stop_current_motion()

        if not axes:
            self._current_axes.clear()
            return

        feedrate = self._last_feedrate
        if feedrate is None:
            feedrate = self.get_feedrate()
            if feedrate is None:
                return
            self._last_feedrate = feedrate

        distance_mm = self.JOG_DISTANCE_MM
        parts = [
            f"{axis}{direction * distance_mm:.3f}"
            for axis, direction in sorted(axes.items())
        ]
        command = f"$J=G91 G21 {' '.join(parts)} F{feedrate}\n"
        self.send_command(command)
        self._current_axes = axes

    def _prune_button_inputs(self) -> None:
        removed = False
        for key in list(self._active_inputs.keys()):
            if key.startswith("button_"):
                button = self._button_lookup.get(key)
                if button is not None and not button.isDown():
                    self._active_inputs.pop(key, None)
                    removed = True
        if removed and not self._active_inputs and self._current_axes:
            self._stop_current_motion()
            self._current_axes.clear()

    def _home_xy(self) -> None:
        self.send_command("$HX\n")
        self.send_command("$HY\n")

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

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        mapping = self._mapping_from_event(event)
        if mapping:
            identifier, (axis, direction) = mapping
            self._handle_press(identifier, axis, direction, auto_repeat=event.isAutoRepeat())
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if event.isAutoRepeat():
            event.ignore()
            return
        mapping = self._mapping_from_event(event)
        if mapping:
            identifier, pair = mapping
            self._handle_release(identifier, pair)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        if self._active_inputs:
            self._active_inputs.clear()
            self._stop_current_motion()
            self._current_axes.clear()
        super().focusOutEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        if self._active_inputs:
            self._active_inputs.clear()
            self._stop_current_motion()
            self._current_axes.clear()
        super().closeEvent(event)

    def _mapping_from_event(
        self, event: QKeyEvent
    ) -> Optional[Tuple[str, Tuple[str, int]]]:
        key = event.key()
        mapping = self.KEY_DIRECTION_MAP.get(key)
        if mapping:
            return (f"key_{key}", mapping)

        text = event.text()
        if text:
            normalized = text.casefold()
            mapping = self.CHAR_DIRECTION_MAP.get(normalized)
            if mapping:
                return (f"char_{normalized}", mapping)

        if 0 < key <= 0x10FFFF:
            normalized = chr(key).casefold()
            mapping = self.CHAR_DIRECTION_MAP.get(normalized)
            if mapping:
                return (f"char_{normalized}", mapping)

        return None
