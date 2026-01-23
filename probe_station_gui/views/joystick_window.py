"""Interactive joystick window for jogging the stage via serial commands."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import serial
from PySide6.QtCore import QEvent, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QCloseEvent, QDoubleValidator, QPainter, QPen
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

from probe_station_gui.qt_compat import keyboard_modifiers_to_int
from probe_station_gui.settings_manager import CONTROL_ACTIONS, KeyBinding


logger = logging.getLogger(__name__)


class _SpinnerOverlay(QWidget):
    """Lightweight spinner overlay drawn with QPainter."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._angle = 0
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_angle(self, angle: int) -> None:
        self._angle = angle % 360
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        size = min(self.width(), self.height())
        if size <= 8:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
        bg_pen = QPen(QColor("#bdbdbd"), 2)
        bg_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(bg_pen)
        painter.drawEllipse(rect)
        pen = QPen(QColor("#1565c0"), 2)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, int(self._angle * 16), int(120 * 16))


class JoystickWindow(QWidget):
    """Widget that provides directional jogging controls."""

    autofocus_requested = Signal()
    reset_requested = Signal()
    home_axis_requested = Signal(str)
    home_all_requested = Signal()

    JOG_DISTANCE_MM = 10.0
    ROTATE_DISTANCE_DEG = 5.0
    DEFAULT_LINEAR_FEEDRATE_PRESETS: tuple[float, ...] = (
        1.0,
        3.0,
        10.0,
        30.0,
        100.0,
        300.0,
    )
    DEFAULT_ROTARY_FEEDRATE_PRESETS: tuple[float, ...] = (
        1.0,
        3.0,
        10.0,
        30.0,
        90.0,
        360.0,
    )
    CUSTOM_FEED_LABEL = "Custom..."
    LINEAR_AXES = {"X", "Y", "Z"}
    ROTATIONAL_AXES = {"A", "B", "C"}
    HOMING_AXES = ("X", "Y", "Z", "A")
    HOMED_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #1565c0; color: #f5f5f5; }"
        "QPushButton:pressed { background: #0d47a1; }"
        "QPushButton:checked { background: #1565c0; }"
        "QPushButton[homing=\"true\"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }"
        "QPushButton[homing=\"true\"]:pressed { background: #e0e0e0; }"
        "QPushButton:disabled { color: #9e9e9e; }"
    )
    NOT_HOMED_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #f0b429; color: #1f1f1f; }"
        "QPushButton:pressed { background: #d89b19; }"
        "QPushButton:checked { background: #f0b429; }"
        "QPushButton[homing=\"true\"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }"
        "QPushButton[homing=\"true\"]:pressed { background: #e0e0e0; }"
        "QPushButton:disabled { color: #9e9e9e; }"
    )
    ALL_HOMED_STYLE = HOMED_STYLE

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

        self.serial_connection: Optional[serial.Serial] = None
        self._active_axes: Optional[tuple[tuple[str, int], ...]] = None
        self._key_stack: list[Tuple[str, object]] = []
        self._key_bindings: Dict[tuple, tuple[str, int]] = {}
        self._linear_presets: List[float] = list(self.DEFAULT_LINEAR_FEEDRATE_PRESETS)
        self._rotary_presets: List[float] = list(self.DEFAULT_ROTARY_FEEDRATE_PRESETS)
        self._linear_default: float = 1.0
        self._rotary_default: float = 1.0
        self._homing_buttons: dict[str, QPushButton] = {}
        self._homing_targets: dict[str, QPushButton] = {}
        self._homing_text: dict[str, str] = {}
        self._homing_overlays: dict[str, _SpinnerOverlay] = {}
        self._homing_spinner_angle = 0
        self._homing_animation_timer = QTimer(self)
        self._homing_animation_timer.setInterval(90)
        self._homing_animation_timer.timeout.connect(self._advance_homing_spinner)
        self.apply_control_bindings({})
        self._event_filter_installed = False
        self._event_filter_retry_scheduled = False
        self._install_event_filter()

        root_layout = QVBoxLayout(self)
        self.status_label = QLabel("Disconnected", self)
        root_layout.addWidget(self.status_label)

        feed_container = QVBoxLayout()

        linear_feed_layout = QHBoxLayout()
        linear_feed_layout.addWidget(QLabel("Linear feed (mm/min):", self))
        self.linear_feedrate_combo = QComboBox(self)
        self.linear_feedrate_combo.currentIndexChanged.connect(
            self._on_linear_feedrate_changed
        )
        linear_feed_layout.addWidget(self.linear_feedrate_combo)

        self.linear_custom_feedrate_edit = QLineEdit(self)
        self.linear_custom_feedrate_edit.setPlaceholderText("Enter custom rate")
        self.linear_custom_feedrate_edit.setValidator(
            QDoubleValidator(0.000001, 1000000.0, 6, self)
        )
        self.linear_custom_feedrate_edit.setVisible(False)
        linear_feed_layout.addWidget(self.linear_custom_feedrate_edit)

        feed_container.addLayout(linear_feed_layout)

        rotary_feed_layout = QHBoxLayout()
        rotary_feed_layout.addWidget(QLabel("Rotary feed (deg/min):", self))
        self.rotary_feedrate_combo = QComboBox(self)
        self.rotary_feedrate_combo.currentIndexChanged.connect(
            self._on_rotary_feedrate_changed
        )
        rotary_feed_layout.addWidget(self.rotary_feedrate_combo)

        self.rotary_custom_feedrate_edit = QLineEdit(self)
        self.rotary_custom_feedrate_edit.setPlaceholderText("Enter custom rate")
        self.rotary_custom_feedrate_edit.setValidator(
            QDoubleValidator(0.000001, 1000000.0, 6, self)
        )
        self.rotary_custom_feedrate_edit.setVisible(False)
        rotary_feed_layout.addWidget(self.rotary_custom_feedrate_edit)

        feed_container.addLayout(rotary_feed_layout)

        root_layout.addLayout(feed_container)

        self._refresh_feedrate_combos(force_defaults=True)

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

        focus_layout = QHBoxLayout()
        focus_layout.addStretch(1)
        focus_layout.addWidget(QLabel("Focus (Z):", self))
        self.focus_down_button = QPushButton("Z-", self)
        self.focus_up_button = QPushButton("Z+", self)
        self.focus_down_button.setToolTip("Focus down (Z-)")
        self.focus_up_button.setToolTip("Focus up (Z+)")
        focus_layout.addWidget(self.focus_down_button)
        focus_layout.addWidget(self.focus_up_button)
        focus_layout.addStretch(1)
        root_layout.addLayout(focus_layout)

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
        self.focus_down_button.pressed.connect(lambda: self.start_jog("Z", -1))
        self.focus_down_button.released.connect(self.stop_jog)
        self.focus_up_button.pressed.connect(lambda: self.start_jog("Z", 1))
        self.focus_up_button.released.connect(self.stop_jog)

        homing_layout = QHBoxLayout()
        homing_layout.addWidget(QLabel("Homing:", self))
        for axis in self.HOMING_AXES:
            button = QPushButton(axis, self)
            button.setFixedSize(28, 28)
            button.setCheckable(True)
            button.setToolTip(f"Home {axis}")
            button.clicked.connect(lambda checked=False, axis=axis: self._home_axis(axis))
            self._set_homing_button_state(button, False, all_homed=False)
            homing_layout.addWidget(button)
            self._homing_buttons[axis] = button
        self.home_all_button = QPushButton("ALL", self)
        self.home_all_button.setFixedSize(42, 28)
        self.home_all_button.setCheckable(True)
        self.home_all_button.setToolTip("Home all axes")
        self.home_all_button.clicked.connect(self._home_all)
        self._set_homing_button_state(self.home_all_button, False, all_homed=False)
        homing_layout.addWidget(self.home_all_button)
        root_layout.addLayout(homing_layout)

        safety_layout = QHBoxLayout()
        self.unlock_button = QPushButton("Unlock", self)
        self.reset_button = QPushButton("Reset", self)
        safety_layout.addWidget(self.unlock_button)
        safety_layout.addWidget(self.reset_button)
        root_layout.addLayout(safety_layout)

        self.autofocus_button = QPushButton("Autofocus", self)
        self.autofocus_button.setToolTip("Run Z-axis autofocus sweep")
        self.autofocus_button.clicked.connect(self.autofocus_requested.emit)
        root_layout.addWidget(self.autofocus_button)

        self.unlock_button.clicked.connect(lambda: self.send_command("$X\n"))
        self.reset_button.clicked.connect(self._send_reset)

        root_layout.addStretch(1)
        self._update_enabled_state()

    def _install_event_filter(self) -> None:
        if self._event_filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            if not self._event_filter_retry_scheduled:
                self._event_filter_retry_scheduled = True
                QTimer.singleShot(0, self._install_event_filter)
            logger.warning(
                "QApplication instance unavailable; joystick event filter deferred"
            )
            return
        app.installEventFilter(self)
        self._event_filter_installed = True
        self._event_filter_retry_scheduled = False
        logger.debug("Joystick event filter installed")

    def _remove_event_filter(self) -> None:
        if not self._event_filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            return
        app.removeEventFilter(self)
        self._event_filter_installed = False
        self._event_filter_retry_scheduled = False
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
        if index < 0:
            editor.setVisible(False)
            return
        is_custom = combo.itemText(index) == self.CUSTOM_FEED_LABEL
        editor.setVisible(is_custom)
        if is_custom:
            editor.setFocus()

    def _format_feedrate(self, value: float) -> str:
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"

    def _refresh_feedrate_combos(self, *, force_defaults: bool = False) -> None:
        self._refresh_feedrate_combo(
            self.linear_feedrate_combo,
            self.linear_custom_feedrate_edit,
            self._linear_presets,
            self._linear_default,
            force_defaults,
            fallback=self.DEFAULT_LINEAR_FEEDRATE_PRESETS,
        )
        self._refresh_feedrate_combo(
            self.rotary_feedrate_combo,
            self.rotary_custom_feedrate_edit,
            self._rotary_presets,
            self._rotary_default,
            force_defaults,
            fallback=self.DEFAULT_ROTARY_FEEDRATE_PRESETS,
        )

    def _refresh_feedrate_combo(
        self,
        combo: QComboBox,
        editor: QLineEdit,
        presets: List[float],
        default_value: float,
        force_default: bool,
        *,
        fallback: tuple[float, ...],
    ) -> None:
        display_items: List[str] = []
        seen: set[str] = set()
        for preset in presets:
            try:
                text = self._format_feedrate(float(preset))
            except (TypeError, ValueError):
                continue
            if text in seen:
                continue
            display_items.append(text)
            seen.add(text)
        if not display_items:
            display_items = [self._format_feedrate(value) for value in fallback]
            seen = set(display_items)

        default_text = ""
        try:
            if default_value > 0:
                default_text = self._format_feedrate(float(default_value))
        except (TypeError, ValueError):
            default_text = ""

        if default_text and default_text not in seen:
            display_items.insert(0, default_text)
            seen.add(default_text)

        if self.CUSTOM_FEED_LABEL not in display_items:
            display_items.append(self.CUSTOM_FEED_LABEL)

        current_text = combo.currentText()
        custom_text = editor.text()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(display_items)

        if force_default and default_text:
            combo.setCurrentText(default_text)
        elif current_text in display_items:
            combo.setCurrentText(current_text)
        elif current_text == self.CUSTOM_FEED_LABEL or editor.isVisible():
            combo.setCurrentText(self.CUSTOM_FEED_LABEL)
            editor.setText(custom_text)
        else:
            combo.setCurrentIndex(0)

        combo.blockSignals(False)
        self._update_custom_visibility(combo, editor, combo.currentIndex())

    def apply_feedrate_settings(
        self,
        linear_presets: List[float],
        linear_default: float,
        rotary_presets: List[float],
        rotary_default: float,
    ) -> None:
        """Update the selectable feedrate presets and defaults from settings."""

        cleaned_linear = self._clean_presets(
            linear_presets, self.DEFAULT_LINEAR_FEEDRATE_PRESETS
        )
        cleaned_rotary = self._clean_presets(
            rotary_presets, self.DEFAULT_ROTARY_FEEDRATE_PRESETS
        )
        default_linear = self._resolve_default(
            linear_default, cleaned_linear, self.DEFAULT_LINEAR_FEEDRATE_PRESETS
        )
        default_rotary = self._resolve_default(
            rotary_default, cleaned_rotary, self.DEFAULT_ROTARY_FEEDRATE_PRESETS
        )

        if (
            cleaned_linear == self._linear_presets
            and cleaned_rotary == self._rotary_presets
            and abs(default_linear - self._linear_default) <= 1e-9
            and abs(default_rotary - self._rotary_default) <= 1e-9
        ):
            return

        self._linear_presets = cleaned_linear
        self._rotary_presets = cleaned_rotary
        self._linear_default = default_linear
        self._rotary_default = default_rotary
        self._refresh_feedrate_combos(force_defaults=True)
        logger.info(
            "Joystick feedrate settings updated: linear=%s (default=%s) rotary=%s (default=%s)",
            cleaned_linear,
            default_linear,
            cleaned_rotary,
            default_rotary,
        )

    def _clean_presets(
        self, presets: List[float], fallback: tuple[float, ...]
    ) -> List[float]:
        cleaned: List[float] = []
        seen: set[float] = set()
        for value in presets:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            key = round(number, 9)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(number)
        if not cleaned:
            cleaned = list(fallback)
        cleaned.sort()
        return cleaned

    def _resolve_default(
        self, default_value: float, presets: List[float], fallback: tuple[float, ...]
    ) -> float:
        if not presets:
            return fallback[0]
        try:
            candidate = float(default_value)
        except (TypeError, ValueError):
            candidate = presets[0]
        if candidate <= 0:
            candidate = presets[0]
        for value in presets:
            if abs(value - candidate) <= 1e-9:
                return value
        return presets[0]

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
            self._stop_homing_animation("ALL")
            for axis in self.HOMING_AXES:
                self._stop_homing_animation(axis)
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
            self.focus_down_button,
            self.focus_up_button,
            self.home_all_button,
            self.unlock_button,
            self.reset_button,
            self.autofocus_button,
        ):
            widget.setEnabled(enabled)
        for button in self._homing_buttons.values():
            button.setEnabled(enabled)

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

        linear_feed: Optional[float] = None
        rotary_feed: Optional[float] = None

        if has_linear:
            linear_feed = self._read_feedrate(
                self.linear_feedrate_combo,
                self.linear_custom_feedrate_edit,
                "millimetres per minute",
            )
            if linear_feed is None:
                return None

        if has_rotary:
            rotary_feed = self._read_feedrate(
                self.rotary_feedrate_combo,
                self.rotary_custom_feedrate_edit,
                "degrees per minute",
            )
            if rotary_feed is None:
                return None

        if has_linear and has_rotary:
            feed_candidates: list[float] = []

            if linear_feed is not None:
                feed_candidates.append(linear_feed)

            max_linear_distance = max(
                (self._distance_for_axis(axis) for axis, _ in axes if axis in self.LINEAR_AXES),
                default=self.JOG_DISTANCE_MM,
            )
            for axis, _ in axes:
                if axis not in self.ROTATIONAL_AXES or rotary_feed is None:
                    continue
                axis_distance = self._distance_for_axis(axis)
                if axis_distance <= 0:
                    continue
                equivalent_linear = rotary_feed * (max_linear_distance / axis_distance)
                feed_candidates.append(equivalent_linear)

            if not feed_candidates:
                return linear_feed or rotary_feed

            chosen_feed = min(feed_candidates)
            logger.debug(
                "Mixed jog feed resolved: candidates=%s chosen=%s", feed_candidates, chosen_feed
            )
            return chosen_feed

        if has_linear:
            return linear_feed

        if has_rotary:
            return rotary_feed

        return None

    def _read_feedrate(
        self, combo: QComboBox, editor: QLineEdit, units: str
    ) -> Optional[float]:
        text = combo.currentText()
        if text == self.CUSTOM_FEED_LABEL:
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

    def set_homing_status(self, homed_axes: set[str]) -> None:
        active_axes = set(homed_axes).intersection(self.HOMING_AXES)
        all_homed = active_axes == set(self.HOMING_AXES)
        for axis, button in self._homing_buttons.items():
            if axis in active_axes:
                self._stop_homing_animation(axis)
                self._set_homing_button_state(button, True, all_homed=all_homed)
            elif axis not in self._homing_targets:
                self._set_homing_button_state(button, False, all_homed=all_homed)
        if all_homed:
            self._stop_homing_animation("ALL")
            self._set_homing_button_state(self.home_all_button, True, all_homed=True)
        elif "ALL" not in self._homing_targets:
            self._set_homing_button_state(self.home_all_button, False, all_homed=False)

    def _set_homing_button_state(
        self, button: QPushButton, homed: bool, *, all_homed: bool = False
    ) -> None:
        button.setProperty("homing", False)
        button.setProperty("all_homed", all_homed)
        button.setChecked(False)
        if all_homed:
            button.setStyleSheet(self.ALL_HOMED_STYLE)
        else:
            button.setStyleSheet(self.HOMED_STYLE if homed else self.NOT_HOMED_STYLE)

    def _home_all(self) -> None:
        self._start_homing_animation("ALL", self.home_all_button)
        self.home_all_requested.emit()

    def _home_axis(self, axis: str) -> None:
        button = self._homing_buttons.get(axis)
        if button is not None:
            self._start_homing_animation(axis, button)
        self.home_axis_requested.emit(axis)

    def _start_homing_animation(self, key: str, button: QPushButton) -> None:
        if key in self._homing_targets:
            return
        base_text = button.text()
        self._homing_targets[key] = button
        self._homing_text[key] = base_text
        overlay = _SpinnerOverlay(button)
        overlay.setGeometry(button.rect())
        overlay.show()
        overlay.raise_()
        self._homing_overlays[key] = overlay
        button.setProperty("homing", True)
        button.setChecked(True)
        button.setEnabled(False)
        button.setStyleSheet(button.styleSheet())
        self._advance_homing_spinner()
        if not self._homing_animation_timer.isActive():
            self._homing_animation_timer.start()

    def _stop_homing_animation(self, key: str) -> None:
        button = self._homing_targets.pop(key, None)
        base_text = self._homing_text.pop(key, None)
        overlay = self._homing_overlays.pop(key, None)
        if button is None:
            return
        button.setProperty("homing", False)
        button.setChecked(False)
        if not self._homing_targets:
            button.setEnabled(True)
        if base_text is not None:
            button.setText(base_text)
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()
        button.setStyleSheet(button.styleSheet())
        button.setEnabled(True)
        if not self._homing_targets:
            self._homing_animation_timer.stop()
            self._homing_spinner_angle = 0

    def _advance_homing_spinner(self) -> None:
        if not self._homing_targets:
            return
        self._homing_spinner_angle = (self._homing_spinner_angle + 30) % 360
        for key in self._homing_targets:
            overlay = self._homing_overlays.get(key)
            if overlay is not None:
                overlay.set_angle(self._homing_spinner_angle)

    def _send_reset(self) -> None:
        self.reset_requested.emit()
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

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._install_event_filter()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._key_stack.clear()
        self.stop_jog()
        self._remove_event_filter()
        super().closeEvent(event)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if event.type() in (QEvent.KeyPress, QEvent.KeyRelease, QEvent.ShortcutOverride):
            event_type_name = {
                QEvent.KeyPress: "KeyPress",
                QEvent.KeyRelease: "KeyRelease",
                QEvent.ShortcutOverride: "ShortcutOverride",
            }.get(event.type(), str(int(event.type())))
            key_value = event.key() if hasattr(event, "key") else None
            text_value = event.text() if hasattr(event, "text") else ""
            modifiers_value = (
                keyboard_modifiers_to_int(event.modifiers())
                if hasattr(event, "modifiers")
                else 0
            )
            source_name = (
                obj.objectName()
                if hasattr(obj, "objectName") and obj.objectName()
                else obj.__class__.__name__ if hasattr(obj, "__class__") else str(obj)
            )
            logger.debug(
                "Global key event: type=%s key=%s text=%r modifiers=%s source=%s",
                event_type_name,
                key_value,
                text_value,
                modifiers_value,
                source_name,
            )
        if event.type() in (QEvent.KeyPress, QEvent.ShortcutOverride):
            if self._should_process_global_event(obj) and self._handle_key_press_event(event):
                event.accept()
                return True
        elif event.type() == QEvent.KeyRelease:
            if self._should_process_global_event(obj) and self._handle_key_release_event(event):
                event.accept()
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
                keyboard_modifiers_to_int(event.modifiers()),
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
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            return True
        logger.debug(
            "No mapping for key press: key=%s text=%s modifiers=%s",
            event.key(),
            event.text(),
            keyboard_modifiers_to_int(event.modifiers()),
        )
        return False

    def _handle_key_release_event(self, event) -> bool:
        if event.isAutoRepeat():
            event.ignore()
            logger.debug(
                "Ignored auto-repeat key release: key=%s text=%s modifiers=%s",
                event.key(),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
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
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            return True
        logger.debug(
            "No mapping for key release: key=%s text=%s modifiers=%s",
            event.key(),
            event.text(),
            keyboard_modifiers_to_int(event.modifiers()),
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
        modifiers = keyboard_modifiers_to_int(event.modifiers())
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
