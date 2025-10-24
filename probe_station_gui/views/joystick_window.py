"""Interactive joystick window for jogging the stage via serial commands."""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import serial
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QCloseEvent, QDoubleValidator
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.settings_manager import CONTROL_ACTIONS, KeyBinding


logger = logging.getLogger(__name__)


class JoystickWindow(QWidget):
    """Widget that provides directional jogging controls."""

    JOG_DISTANCE_MM = 10.0
    ROTATE_DISTANCE_DEG = 5.0
    FEED_RATES = ["30", "60", "90", "120", "180", "Custom..."]
    LINEAR_AXES = {"X", "Y", "Z"}
    ROTATIONAL_AXES = {"A", "B", "C"}

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

        self.serial_connection: Optional[serial.Serial] = None
        self._active_axes: Optional[tuple[tuple[str, int], ...]] = None
        self._key_stack: list[Tuple[str, object]] = []
        self._key_bindings: Dict[tuple, tuple[str, int]] = {}
        self.apply_control_bindings({})
        self._event_filter_installed = False
        self._install_event_filter()

        root_layout = QVBoxLayout(self)
        self.status_label = QLabel("Disconnected", self)
        root_layout.addWidget(self.status_label)

        feed_container = QVBoxLayout()

        linear_feed_layout = QHBoxLayout()
        linear_feed_layout.addWidget(QLabel("Linear feed (mm/min):", self))
        self.linear_feedrate_combo = QComboBox(self)
        self.linear_feedrate_combo.addItems(self.FEED_RATES)
        self.linear_feedrate_combo.currentIndexChanged.connect(
            self._on_linear_feedrate_changed
        )
        linear_feed_layout.addWidget(self.linear_feedrate_combo)

        self.linear_custom_feedrate_edit = QLineEdit(self)
        self.linear_custom_feedrate_edit.setPlaceholderText("Enter custom rate")
        self.linear_custom_feedrate_edit.setValidator(
            QDoubleValidator(0.1, 10000.0, 2, self)
        )
        self.linear_custom_feedrate_edit.setVisible(False)
        linear_feed_layout.addWidget(self.linear_custom_feedrate_edit)

        feed_container.addLayout(linear_feed_layout)

        rotary_feed_layout = QHBoxLayout()
        rotary_feed_layout.addWidget(QLabel("Rotary feed (deg/min):", self))
        self.rotary_feedrate_combo = QComboBox(self)
        self.rotary_feedrate_combo.addItems(self.FEED_RATES)
        self.rotary_feedrate_combo.currentIndexChanged.connect(
            self._on_rotary_feedrate_changed
        )
        rotary_feed_layout.addWidget(self.rotary_feedrate_combo)

        self.rotary_custom_feedrate_edit = QLineEdit(self)
        self.rotary_custom_feedrate_edit.setPlaceholderText("Enter custom rate")
        self.rotary_custom_feedrate_edit.setValidator(
            QDoubleValidator(0.1, 10000.0, 2, self)
        )
        self.rotary_custom_feedrate_edit.setVisible(False)
        rotary_feed_layout.addWidget(self.rotary_custom_feedrate_edit)

        feed_container.addLayout(rotary_feed_layout)

        root_layout.addLayout(feed_container)

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
        rotate_layout.addWidget(QLabel("Rotate B:", self))
        self.rotate_negative_button = QPushButton("↻", self)
        self.rotate_positive_button = QPushButton("↺", self)
        self.rotate_negative_button.setToolTip("Rotate clockwise (B-)")
        self.rotate_positive_button.setToolTip("Rotate counter-clockwise (B+)")
        rotate_layout.addWidget(self.rotate_negative_button)
        rotate_layout.addWidget(self.rotate_positive_button)
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
        self.rotate_negative_button.pressed.connect(lambda: self.start_jog("B", -1))
        self.rotate_negative_button.released.connect(self.stop_jog)
        self.rotate_positive_button.pressed.connect(lambda: self.start_jog("B", 1))
        self.rotate_positive_button.released.connect(self.stop_jog)

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

    def _install_event_filter(self) -> None:
        if self._event_filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            logger.warning("QApplication instance unavailable; joystick event filter deferred")
            return
        app.installEventFilter(self)
        self._event_filter_installed = True
        logger.debug("Joystick event filter installed")

    def _remove_event_filter(self) -> None:
        if not self._event_filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            return
        app.removeEventFilter(self)
        self._event_filter_installed = False
        logger.debug("Joystick event filter removed")

    def _on_linear_feedrate_changed(self, index: int) -> None:
        self._update_custom_visibility(
            self.linear_feedrate_combo,
            self.linear_custom_feedrate_edit,
            index,
        )

    def _on_rotary_feedrate_changed(self, index: int) -> None:
        self._update_custom_visibility(
            self.rotary_feedrate_combo,
            self.rotary_custom_feedrate_edit,
            index,
        )

    def _update_custom_visibility(
        self, combo: QComboBox, editor: QLineEdit, index: int
    ) -> None:
        is_custom = combo.itemText(index) == "Custom..."
        editor.setVisible(is_custom)
        if is_custom:
            editor.setFocus()

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Assign the serial connection used for jogging commands."""

        if self.serial_connection and self.serial_connection.is_open:
            self.stop_jog()
        self.serial_connection = serial_connection
        if not serial_connection or not serial_connection.is_open:
            self._active_axes = None
            self._key_stack.clear()
            logger.debug("Joystick serial detached")
        if serial_connection and serial_connection.is_open:
            self.status_label.setText(
                f"Connected to {serial_connection.port} @ {serial_connection.baudrate}"
            )
            logger.info(
                "Joystick connected to %s @ %s baud",
                serial_connection.port,
                serial_connection.baudrate,
            )
        else:
            self.status_label.setText("Disconnected")
            logger.info("Joystick disconnected from serial link")
        self._update_enabled_state()

    def _update_enabled_state(self) -> None:
        enabled = bool(self.serial_connection and self.serial_connection.is_open)
        for widget in (
            self.linear_feedrate_combo,
            self.linear_custom_feedrate_edit,
            self.rotary_feedrate_combo,
            self.rotary_custom_feedrate_edit,
            self.up_button,
            self.down_button,
            self.left_button,
            self.right_button,
            self.rotate_negative_button,
            self.rotate_positive_button,
            self.home_all_button,
            self.home_xy_button,
            self.home_z_button,
            self.unlock_button,
            self.reset_button,
        ):
            widget.setEnabled(enabled)

    def start_jog(self, axis: str, direction: int) -> None:
        logger.debug("Start jog requested: axis=%s direction=%s", axis, direction)
        self._apply_axes(((axis, direction),))

    def stop_jog(self) -> None:
        if not self.serial_connection or not self.serial_connection.is_open:
            self._active_axes = None
            return
        if self._active_axes is None:
            return
        self._active_axes = None
        self.send_command(b"\x85")
        logger.debug("Stop jog command issued")

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
        feedrate = self._feedrate_for_axes(axes_sorted)
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
        logger.debug("Jog command sent: %s", command.strip())

    def _distance_for_axis(self, axis: str) -> float:
        if axis == "B":
            return self.ROTATE_DISTANCE_DEG
        return self.JOG_DISTANCE_MM

    def _feedrate_for_axes(
        self, axes: tuple[tuple[str, int], ...]
    ) -> Optional[float]:
        has_rotary = any(axis in self.ROTATIONAL_AXES for axis, _ in axes)
        has_linear = any(axis in self.LINEAR_AXES for axis, _ in axes)
        if has_rotary and has_linear:
            self._show_warning(
                "Cannot jog rotary and linear axes at the same time. Release one of the keys first."
            )
            logger.warning("Rejected mixed jog request: axes=%s", axes)
            return None
        if has_rotary:
            return self._read_feedrate(
                self.rotary_feedrate_combo,
                self.rotary_custom_feedrate_edit,
                "degrees per minute",
            )
        return self._read_feedrate(
            self.linear_feedrate_combo,
            self.linear_custom_feedrate_edit,
            "millimetres per minute",
        )

    def _read_feedrate(
        self, combo: QComboBox, editor: QLineEdit, units: str
    ) -> Optional[float]:
        text = combo.currentText()
        if text == "Custom...":
            text = editor.text().strip()
            if not text:
                self._show_warning(
                    f"Please enter a custom feed rate ({units})."
                )
                return None
        try:
            value = float(text)
            if value <= 0:
                raise ValueError
            return value
        except ValueError:
            self._show_warning(
                f"Feed rate must be a positive number ({units})."
            )
            logger.warning("Invalid feed rate '%s' for %s jog", text, units)
            return None

    def _update_active_jog(self) -> None:
        unique_axes: dict[str, int] = {}
        for identifier in self._key_stack:
            mapping = self._mapping_from_identifier(identifier)
            if mapping is None:
                continue
            axis, direction = mapping
            unique_axes[axis] = direction
        axes = tuple(unique_axes.items())
        logger.debug("Active keys mapped to axes: %s", axes)
        self._apply_axes(axes)

    def _home_xy(self) -> None:
        self.send_command("$HX\n")
        self.send_command("$HY\n")

    def _send_reset(self) -> None:
        self.send_command(b"\x18")

    def send_command(self, command: str | bytes) -> None:
        if not self.serial_connection or not self.serial_connection.is_open:
            logger.debug("Discarded command because serial is closed: %s", command)
            return
        try:
            data = command if isinstance(command, bytes) else command.encode("ascii")
            self.serial_connection.write(data)
            self.serial_connection.flush()
            if isinstance(command, bytes):
                logger.debug("Command written to serial (bytes): %s", command.hex())
            else:
                logger.debug("Command written to serial: %s", command.strip())
        except serial.SerialException as error:  # pragma: no cover - best effort guard
            self._show_warning(f"Serial communication error: {error}")
            self.set_serial(None)
            logger.exception("Serial communication error: %s", error)

    def _show_warning(self, message: str) -> None:
        QMessageBox.warning(self, "Joystick", message)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if self._handle_key_press_event(event):
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._handle_key_release_event(event):
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        self._key_stack.clear()
        self.stop_jog()
        super().focusOutEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._key_stack.clear()
        self.stop_jog()
        self._remove_event_filter()
        super().closeEvent(event)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if event.type() == QEvent.KeyPress:
            if self._should_process_global_event(obj) and self._handle_key_press_event(event):
                return True
        elif event.type() == QEvent.KeyRelease:
            if self._should_process_global_event(obj) and self._handle_key_release_event(event):
                return True
        return super().eventFilter(obj, event)

    def _should_process_global_event(self, obj) -> bool:
        if not self.isVisible():
            logger.debug("Ignoring global key event because joystick is hidden")
            return False
        window = self.window()
        if window is None or not window.isActiveWindow():
            logger.debug("Ignoring global key event because joystick window is not active")
            return False
        focus_widget = window.focusWidget()
        if focus_widget is not None and self._is_text_entry_widget(focus_widget):
            logger.debug(
                "Ignoring global key event because focus widget %s expects text",
                focus_widget.objectName() or focus_widget.__class__.__name__,
            )
            return False
        if isinstance(obj, QWidget) and self._is_text_entry_widget(obj):
            logger.debug(
                "Ignoring global key event originating from text widget %s",
                obj.objectName() or obj.__class__.__name__,
            )
            return False
        return True

    def _handle_key_press_event(self, event) -> bool:
        if event.isAutoRepeat():
            event.ignore()
            logger.debug(
                "Ignored auto-repeat key press: key=%s text=%s modifiers=%s",
                event.key(),
                event.text(),
                int(event.modifiers()),
            )
            return True
        identifier, mapping = self._mapping_from_event(event)
        if identifier and mapping:
            if identifier not in self._key_stack:
                self._key_stack.append(identifier)
                self._update_active_jog()
            event.accept()
            logger.debug(
                "Processed key press: key=%s text=%s modifiers=%s -> %s",
                event.key(),
                event.text(),
                int(event.modifiers()),
                mapping,
            )
            return True
        logger.debug(
            "No mapping for key press: key=%s text=%s modifiers=%s",
            event.key(),
            event.text(),
            int(event.modifiers()),
        )
        return False

    def _handle_key_release_event(self, event) -> bool:
        if event.isAutoRepeat():
            event.ignore()
            logger.debug(
                "Ignored auto-repeat key release: key=%s text=%s modifiers=%s",
                event.key(),
                event.text(),
                int(event.modifiers()),
            )
            return True
        identifier, mapping = self._mapping_from_event(event)
        if identifier and mapping:
            if identifier in self._key_stack:
                self._key_stack.remove(identifier)
                self._update_active_jog()
            event.accept()
            logger.debug(
                "Processed key release: key=%s text=%s modifiers=%s -> %s",
                event.key(),
                event.text(),
                int(event.modifiers()),
                mapping,
            )
            return True
        logger.debug(
            "No mapping for key release: key=%s text=%s modifiers=%s",
            event.key(),
            event.text(),
            int(event.modifiers()),
        )
        return False

    @staticmethod
    def _is_text_entry_widget(widget: Optional[QWidget]) -> bool:
        if widget is None:
            return False
        if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
            return True
        parent = widget.parentWidget()
        if parent is not None and parent is not widget:
            return JoystickWindow._is_text_entry_widget(parent)
        return False

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
        logger.info("Joystick key bindings updated: %d entries", len(self._key_bindings))
