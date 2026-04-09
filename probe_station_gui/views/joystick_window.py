"""Interactive joystick window for jogging the stage via serial commands."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

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

if TYPE_CHECKING:
    from probe_station_gui.stage_controller import StageController

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
    motion_axis_requested = Signal(str)
    jog_command_changed = Signal(object, float)
    jog_stopped = Signal()
    home_axis_requested = Signal(str)
    home_all_requested = Signal()
    needles_raise_requested = Signal()
    needles_lower_requested = Signal()
    zero_b_requested = Signal()
    reset_calibration_requested = Signal()

    DEFAULT_JOG_DISTANCE_MM = 25.0
    DEFAULT_ROTATE_DISTANCE_DEG = 5.0
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
    KEYBOARD_JOG_SYNC_DEBOUNCE_MS = 30
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
    NEEDLES_UP_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #1565c0; color: #f5f5f5; }"
        "QPushButton:pressed { background: #0d47a1; }"
        "QPushButton:checked { background: #1565c0; }"
        "QPushButton[homing=\"true\"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }"
        "QPushButton[homing=\"true\"]:pressed { background: #e0e0e0; }"
        "QPushButton:disabled { color: #9e9e9e; }"
    )
    NEEDLES_DOWN_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #f0b429; color: #1f1f1f; }"
        "QPushButton:pressed { background: #d89b19; }"
        "QPushButton:checked { background: #f0b429; }"
        "QPushButton[homing=\"true\"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }"
        "QPushButton[homing=\"true\"]:pressed { background: #e0e0e0; }"
        "QPushButton:disabled { color: #9e9e9e; }"
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

        self.serial_connection: Optional[serial.Serial] = None
        self.stage_controller: Optional["StageController"] = None
        self._active_axes: Optional[tuple[tuple[str, int], ...]] = None
        self._key_stack: list[Tuple[str, object]] = []
        self._key_bindings: Dict[tuple, tuple[str, int]] = {}
        self._linear_presets: List[float] = list(self.DEFAULT_LINEAR_FEEDRATE_PRESETS)
        self._rotary_presets: List[float] = list(self.DEFAULT_ROTARY_FEEDRATE_PRESETS)
        self._linear_default: float = 1.0
        self._rotary_default: float = 1.0
        self._linear_jog_distance_mm: float = self.DEFAULT_JOG_DISTANCE_MM
        self._rotary_jog_distance_deg: float = self.DEFAULT_ROTATE_DISTANCE_DEG
        self._homing_buttons: dict[str, QPushButton] = {}
        self._homing_targets: dict[str, QPushButton] = {}
        self._homing_text: dict[str, str] = {}
        self._homing_overlays: dict[str, _SpinnerOverlay] = {}
        self._homing_spinner_angle = 0
        self._axis_a_ready = False
        self._needles_known = False
        self._needles_up = False
        self._needle_targets: dict[str, QPushButton] = {}
        self._needle_text: dict[str, str] = {}
        self._needle_overlays: dict[str, _SpinnerOverlay] = {}
        self._needle_spinner_angle = 0
        self._homing_animation_timer = QTimer(self)
        self._homing_animation_timer.setInterval(90)
        self._homing_animation_timer.timeout.connect(self._advance_homing_spinner)
        self._needle_animation_timer = QTimer(self)
        self._needle_animation_timer.setInterval(90)
        self._needle_animation_timer.timeout.connect(self._advance_needle_spinner)
        self._jog_state_sync_timer = QTimer(self)
        self._jog_state_sync_timer.setSingleShot(True)
        self._jog_state_sync_timer.setInterval(self.KEYBOARD_JOG_SYNC_DEBOUNCE_MS)
        self._jog_state_sync_timer.timeout.connect(self._sync_active_jog_state)
        self._jog_stop_resend_pending = False
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
        self.zero_b_button = QPushButton("Zero B", self)
        self.rotate_negative_button.setToolTip("Rotate clockwise (B-)")
        self.rotate_positive_button.setToolTip("Rotate counter-clockwise (B+)")
        self.zero_b_button.setToolTip("Use the current B position as zero")
        rotate_layout.addWidget(self.rotate_negative_button)
        rotate_layout.addWidget(self.rotate_positive_button)
        rotate_layout.addWidget(self.zero_b_button)
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
        self.zero_b_button.clicked.connect(self.zero_b_requested.emit)
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

        needles_layout = QHBoxLayout()
        needles_layout.addWidget(QLabel("Needles:", self))
        self.needles_raise_button = QPushButton("Raise", self)
        self.needles_lower_button = QPushButton("Lower", self)
        self.needles_raise_button.setCheckable(True)
        self.needles_lower_button.setCheckable(True)
        self.needles_raise_button.setToolTip("Raise needles (home A)")
        self.needles_lower_button.setToolTip("Lower needles (calibrated)")
        self.needles_raise_button.clicked.connect(self._raise_needles)
        self.needles_lower_button.clicked.connect(self._lower_needles)
        needles_layout.addWidget(self.needles_raise_button)
        needles_layout.addWidget(self.needles_lower_button)
        root_layout.addLayout(needles_layout)

        self.needles_status = QLabel("Needles: unknown", self)
        self.needles_status.setAlignment(Qt.AlignCenter)
        self.needles_status.setFixedHeight(18)
        root_layout.addWidget(self.needles_status)

        safety_layout = QHBoxLayout()
        self.unlock_button = QPushButton("Unlock", self)
        self.reset_button = QPushButton("Reset", self)
        self.reset_calibration_button = QPushButton("Reset Cal", self)
        safety_layout.addWidget(self.unlock_button)
        safety_layout.addWidget(self.reset_button)
        safety_layout.addWidget(self.reset_calibration_button)
        root_layout.addLayout(safety_layout)

        self.autofocus_button = QPushButton("Autofocus", self)
        self.autofocus_button.setToolTip("Run Z-axis autofocus sweep")
        self.autofocus_button.clicked.connect(self.autofocus_requested.emit)
        root_layout.addWidget(self.autofocus_button)

        self.unlock_button.clicked.connect(lambda: self.send_command("$X\n"))
        self.reset_button.clicked.connect(self._send_reset)
        self.reset_calibration_button.clicked.connect(
            self.reset_calibration_requested.emit
        )

        root_layout.addStretch(1)
        self._update_enabled_state()
        self.set_needles_state(False, False)

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

    def apply_jog_settings(
        self, linear_distance_mm: float, rotary_distance_deg: float
    ) -> None:
        """Update the jog distance used for linear and rotary axes."""

        self._linear_jog_distance_mm = max(0.001, float(linear_distance_mm))
        self._rotary_jog_distance_deg = max(0.001, float(rotary_distance_deg))
        logger.debug(
            "Joystick jog distance updated: linear_distance_mm=%s rotary_distance_deg=%s",
            self._linear_jog_distance_mm,
            self._rotary_jog_distance_deg,
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
        motion_enabled = enabled and self._axis_a_ready
        for widget in (
            self.linear_feedrate_combo,
            self.linear_custom_feedrate_edit,
            self.rotary_feedrate_combo,
            self.rotary_custom_feedrate_edit,
            self.home_all_button,
            self.needles_raise_button,
            self.needles_lower_button,
            self.unlock_button,
            self.reset_button,
            self.zero_b_button,
            self.reset_calibration_button,
        ):
            widget.setEnabled(enabled)
        for widget in (
            self.up_button,
            self.down_button,
            self.left_button,
            self.right_button,
            self.rotate_negative_button,
            self.rotate_positive_button,
            self.focus_down_button,
            self.focus_up_button,
            self.autofocus_button,
        ):
            widget.setEnabled(motion_enabled)
        for button in self._homing_buttons.values():
            button.setEnabled(enabled)

    def set_axis_a_ready(self, ready: bool) -> None:
        self._axis_a_ready = ready
        if not ready:
            self.stop_jog()
            self._key_stack.clear()
        self._update_enabled_state()

    def set_needles_state(self, raised: bool, known: bool) -> None:
        self._needles_up = raised
        self._needles_known = known
        if raised and known:
            self.needles_raise_button.setStyleSheet(self.NEEDLES_UP_STYLE)
            self.needles_lower_button.setStyleSheet(self.NEEDLES_DOWN_STYLE)
            self.needles_status.setText("Needles: up")
            self.needles_status.setStyleSheet(
                "QLabel { background: #2e7d32; color: #f5f5f5; border-radius: 3px; padding: 2px; }"
            )
        else:
            self.needles_raise_button.setStyleSheet(self.NEEDLES_DOWN_STYLE)
            self.needles_lower_button.setStyleSheet(self.NEEDLES_DOWN_STYLE)
            if known:
                self.needles_status.setText("Needles: down")
            else:
                self.needles_status.setText("Needles: unknown")
            self.needles_status.setStyleSheet(
                "QLabel { background: #f0b429; color: #1f1f1f; border-radius: 3px; padding: 2px; }"
            )

    def set_needles_action_started(self, action: str) -> None:
        if action == "raise":
            self._start_needle_animation("raise", self.needles_raise_button)
        elif action == "lower":
            self._start_needle_animation("lower", self.needles_lower_button)

    def set_needles_action_finished(self, success: bool, message: str, action: str) -> None:
        if action == "raise":
            self._stop_needle_animation("raise")
        elif action == "lower":
            self._stop_needle_animation("lower")
        if not success:
            self._show_warning(message)

    def _move_safety_check(self) -> bool:
        if not self._axis_a_ready:
            logger.debug("Jog blocked because A axis is not homed/zero")
            return False
        if self.stage_controller is not None and self.stage_controller.is_busy():
            logger.debug("Jog blocked because stage controller is busy")
            return False
        return True

    def set_stage_controller(self, stage_controller: Optional["StageController"]) -> None:
        self.stage_controller = stage_controller

    def start_jog(self, axis: str, direction: int) -> None:
        logger.debug("TIMING start_jog_requested axis=%s direction=%s", axis, direction)
        if not self._move_safety_check():
            return
        self.motion_axis_requested.emit(axis.upper())
        self._apply_axes(((axis, direction),))

    def stop_jog(self) -> None:
        had_active_axes = self._active_axes is not None
        logger.debug(
            "TIMING stop_jog_requested active_axes=%s key_stack=%s",
            self._active_axes,
            self._key_stack,
        )
        if not self.serial_connection or not self.serial_connection.is_open:
            self._active_axes = None
            if had_active_axes:
                self.jog_stopped.emit()
            return
        self._active_axes = None
        self.send_command(b"\x85")
        self._schedule_jog_stop_resend()
        if had_active_axes:
            self.jog_stopped.emit()
        logger.debug("Stop jog command issued")

    def _apply_axes(self, axes: tuple[tuple[str, int], ...]) -> None:
        if not self._move_safety_check():
            self.stop_jog()
            return
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
        commanded_distances: list[tuple[str, float]] = []
        for axis, direction in axes_sorted:
            distance = direction * self._distance_for_axis(axis)
            commanded_distances.append((axis, distance))
            parts.append(f"{axis}{distance:.3f}")
        command = f"$J=G91 G21 {' '.join(parts)} F{feedrate}\n"
        logger.debug(
            "TIMING jog_command_prepared axes=%s feedrate=%s command=%s",
            commanded_distances,
            feedrate,
            command.strip(),
        )
        self.send_command(command)
        self._active_axes = axes_sorted
        self.jog_command_changed.emit(tuple(commanded_distances), float(feedrate))
        logger.debug("TIMING jog_command_sent command=%s", command.strip())

    def _distance_for_axis(self, axis: str) -> float:
        if axis == "B":
            return self._rotary_jog_distance_deg
        return self._linear_jog_distance_mm

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
                default=self._linear_jog_distance_mm,
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

    def _compute_active_axes(self) -> tuple[tuple[str, int], ...]:
        unique_axes: dict[str, int] = {}
        for identifier in self._key_stack:
            mapping = self._mapping_from_identifier(identifier)
            if mapping is None:
                continue
            axis, direction = mapping
            unique_axes[axis] = direction
        return tuple(unique_axes.items())

    def _schedule_active_jog_update(self) -> None:
        if self._jog_state_sync_timer.isActive():
            self._jog_state_sync_timer.stop()
        self._jog_state_sync_timer.start()

    def _sync_active_jog_state(self) -> None:
        axes = self._compute_active_axes()
        logger.debug("Active keys mapped to axes: %s", axes)
        if not axes:
            self.stop_jog()
            return
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
        self.home_all_requested.emit()

    def _home_axis(self, axis: str) -> None:
        self.home_axis_requested.emit(axis)

    def set_homing_action_started(self, axis_key: str) -> None:
        if axis_key == "ALL":
            self._start_homing_animation("ALL", self.home_all_button)
            return
        button = self._homing_buttons.get(axis_key)
        if button is not None:
            self._start_homing_animation(axis_key, button)

    def set_homing_action_finished(self, success: bool, message: str, axis_key: str) -> None:
        if axis_key == "ALL":
            self._stop_homing_animation("ALL")
        else:
            self._stop_homing_animation(axis_key)
        if not success:
            self._show_warning(message)

    def _raise_needles(self) -> None:
        self._start_needle_animation("raise", self.needles_raise_button)
        self.needles_raise_requested.emit()

    def _lower_needles(self) -> None:
        self._start_needle_animation("lower", self.needles_lower_button)
        self.needles_lower_requested.emit()

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

    def _start_needle_animation(self, key: str, button: QPushButton) -> None:
        if key in self._needle_targets:
            return
        base_text = button.text()
        self._needle_targets[key] = button
        self._needle_text[key] = base_text
        overlay = _SpinnerOverlay(button)
        overlay.setGeometry(button.rect())
        overlay.show()
        overlay.raise_()
        self._needle_overlays[key] = overlay
        button.setProperty("homing", True)
        button.setChecked(True)
        self.needles_raise_button.setEnabled(False)
        self.needles_lower_button.setEnabled(False)
        button.setStyleSheet(button.styleSheet())
        self._advance_needle_spinner()
        if not self._needle_animation_timer.isActive():
            self._needle_animation_timer.start()

    def stop_needle_animation(self) -> None:
        for key in list(self._needle_targets.keys()):
            self._stop_needle_animation(key)

    def _stop_needle_animation(self, key: str) -> None:
        button = self._needle_targets.pop(key, None)
        base_text = self._needle_text.pop(key, None)
        overlay = self._needle_overlays.pop(key, None)
        if button is None:
            return
        button.setProperty("homing", False)
        button.setChecked(False)
        if base_text is not None:
            button.setText(base_text)
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()
        button.setStyleSheet(button.styleSheet())
        if not self._needle_targets:
            self._needle_animation_timer.stop()
            self._needle_spinner_angle = 0
            self._update_enabled_state()

    def _advance_needle_spinner(self) -> None:
        if not self._needle_targets:
            return
        self._needle_spinner_angle = (self._needle_spinner_angle + 30) % 360
        for key in self._needle_targets:
            overlay = self._needle_overlays.get(key)
            if overlay is not None:
                overlay.set_angle(self._needle_spinner_angle)

    def _send_reset(self) -> None:
        self.reset_requested.emit()
        self.send_command(b"\x18")

    def send_command(self, command: str | bytes) -> None:
        if not self.serial_connection or not self.serial_connection.is_open:
            logger.debug("Discarded command because serial is closed: %s", command)
            return
        if self.stage_controller is not None:
            try:
                if isinstance(command, bytes) and command == b"\x85":
                    self.stage_controller.queue_jog_stop()
                    return
                if isinstance(command, bytes) and command == b"\x18":
                    self.stage_controller.queue_soft_reset()
                    return
                if isinstance(command, str) and command.startswith("$J="):
                    self.stage_controller.queue_jog_command(command)
                    return
                if isinstance(command, str):
                    self.stage_controller.queue_manual_command(command)
                    return
            except Exception as error:  # pragma: no cover - UI safety guard
                self._show_warning(str(error))
                logger.exception("Failed to queue controller command: %s", error)
                return
        try:
            data = command if isinstance(command, bytes) else command.encode("ascii")
            if isinstance(command, str) and command.startswith("$J="):
                logger.debug("TIMING jog_serial_write_begin command=%s", command.strip())
            elif isinstance(command, bytes) and command == b"\x85":
                logger.debug("TIMING jog_stop_write_begin command=0x85")
            self.serial_connection.write(data)
            self.serial_connection.flush()
            if isinstance(command, str) and command.startswith("$J="):
                logger.debug("TIMING jog_serial_write_flushed command=%s", command.strip())
            elif isinstance(command, bytes) and command == b"\x85":
                logger.debug("TIMING jog_stop_write_flushed command=0x85")
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
        if event.type() == QEvent.ShortcutOverride:
            if self._should_process_global_event(obj):
                identifier, mapping = self._mapping_from_event(event)
                if identifier and mapping:
                    event.accept()
                    return True
        elif event.type() == QEvent.KeyPress:
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
        if not self._axis_a_ready:
            logger.debug("Ignoring global key event because A axis is not homed/zero")
            return False
        focus_widget = QApplication.instance().focusWidget() if QApplication.instance() else None
        if self._is_text_entry_widget(focus_widget) or self._is_terminal_widget(focus_widget):
            logger.debug("Ignoring global key event because focus is in terminal/text input")
            return False
        if isinstance(obj, QWidget) and self._is_text_entry_widget(obj):
            logger.debug(
                "Ignoring global key event originating from text widget %s",
                obj.objectName() or obj.__class__.__name__,
            )
            return False
        return True

    def _handle_key_press_event(self, event) -> bool:
        if not self._key_stack:
            if not self._move_safety_check():
                event.ignore()
                return True
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
            logger.debug(
                "TIMING keypress_received key=%s text=%s modifiers=%s mapping=%s",
                event.key(),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            if identifier not in self._key_stack:
                self._key_stack.append(identifier)
                self._schedule_active_jog_update()
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
                self._schedule_active_jog_update()
            event.accept()
            logger.debug(
                "TIMING keyrelease_received key=%s text=%s modifiers=%s mapping=%s remaining=%s",
                event.key(),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
                self._key_stack,
            )
            logger.debug(
                "Processed key release: key=%s text=%s modifiers=%s -> %s",
                event.key(),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            return True
        removed = self._remove_stale_key(event)
        if removed:
            self._schedule_active_jog_update()
            event.accept()
            logger.debug(
                "Recovered key release: key=%s text=%s modifiers=%s",
                event.key(),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
            )
            return True
        logger.debug(
            "No mapping for key release: key=%s text=%s modifiers=%s",
            event.key(),
            event.text(),
            keyboard_modifiers_to_int(event.modifiers()),
        )
        return False

    def _remove_stale_key(self, event) -> bool:
        if not self._key_stack:
            return False
        key = event.key()
        text = event.text().casefold() if event.text() else ""
        removed = False
        for identifier in list(self._key_stack):
            kind, value = identifier
            if kind == "key":
                if isinstance(value, tuple) and value[0] == key:
                    self._key_stack.remove(identifier)
                    removed = True
            elif kind == "text" and text and value == text:
                self._key_stack.remove(identifier)
                removed = True
        return removed

    def _schedule_jog_stop_resend(self) -> None:
        if self._jog_stop_resend_pending:
            return
        self._jog_stop_resend_pending = True

        def resend() -> None:
            self._jog_stop_resend_pending = False
            if self._key_stack:
                return
            if not self.serial_connection or not self.serial_connection.is_open:
                return
            self.send_command(b"\x85")
            logger.debug("Resent stop jog command")

        QTimer.singleShot(120, resend)

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

    @staticmethod
    def _is_terminal_widget(widget: Optional[QWidget]) -> bool:
        current = widget
        while current is not None:
            if current.__class__.__name__ == "SerialTerminalWindow":
                return True
            current = current.parentWidget()
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
