"""Interactive joystick window for jogging the stage via serial commands."""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import serial
from PySide6.QtCore import QEvent, QLocale, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QCloseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.qt_compat import (
    derive_native_scan_code_from_qt_key,
    keyboard_modifiers_to_int,
    native_scan_code_to_int,
)
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


class _FeedrateSlider(QSlider):
    """Slider with visual overlay for temporarily unavailable feedrate ranges."""

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._temporary_bounds: tuple[int, int] | None = None

    def set_temporary_bounds(self, minimum: int | None, maximum: int | None) -> None:
        if minimum is None or maximum is None:
            bounds = None
        else:
            bounds = (int(minimum), int(maximum))
        if bounds == self._temporary_bounds:
            return
        self._temporary_bounds = bounds
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self._temporary_bounds is None or self.orientation() != Qt.Horizontal:
            return
        lower, upper = self._temporary_bounds
        slider_min = self.minimum()
        slider_max = self.maximum()
        if slider_max <= slider_min:
            return
        lower = max(slider_min, min(slider_max, lower))
        upper = max(slider_min, min(slider_max, upper))
        if lower <= slider_min and upper >= slider_max:
            return

        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.CC_Slider,
            option,
            QStyle.SC_SliderGroove,
            self,
        )
        handle = self.style().subControlRect(
            QStyle.CC_Slider,
            option,
            QStyle.SC_SliderHandle,
            self,
        )
        usable_left = groove.left() + handle.width() // 2
        usable_right = groove.right() - handle.width() // 2
        usable_width = max(1, usable_right - usable_left)
        lower_x = usable_left + QStyle.sliderPositionFromValue(
            slider_min,
            slider_max,
            lower,
            usable_width,
            option.upsideDown,
        )
        upper_x = usable_left + QStyle.sliderPositionFromValue(
            slider_min,
            slider_max,
            upper,
            usable_width,
            option.upsideDown,
        )
        if lower_x > upper_x:
            lower_x, upper_x = upper_x, lower_x

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        color = QColor("#9e9e9e")
        color.setAlpha(150)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        overlay_rect = groove.adjusted(0, -2, 0, 2)
        if lower > slider_min:
            painter.drawRect(
                overlay_rect.left(),
                overlay_rect.top(),
                max(0, lower_x - overlay_rect.left()),
                overlay_rect.height(),
            )
        if upper < slider_max:
            painter.drawRect(
                upper_x,
                overlay_rect.top(),
                max(0, overlay_rect.right() - upper_x + 1),
                overlay_rect.height(),
            )


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
    reset_calibration_requested = Signal()
    manual_axis_move_requested = Signal(str, float, str, float)
    manual_axis_settings_changed = Signal(str, float, str, float)
    linear_feedrate_changed = Signal(float)
    zero_b_requested = Signal()

    DEFAULT_JOG_DISTANCE_MM = 25.0
    DEFAULT_ROTATE_DISTANCE_DEG = 5.0
    DEFAULT_MANUAL_AXIS_DISTANCE_MM = 1.0
    DEFAULT_MANUAL_AXIS_MODE = "G91"
    DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN = 600.0
    DEFAULT_LINEAR_FEEDRATE_PRESETS: tuple[float, ...] = (
        1.0,
        3.0,
        10.0,
        30.0,
        100.0,
        300.0,
    )
    KEYBOARD_JOG_SYNC_DEBOUNCE_MS = 10
    KEYBOARD_JOG_SECONDARY_AXIS_ACTIVATION_MS = 320
    KEYBOARD_JOG_DIAGONAL_CHORD_WINDOW_MS = 90
    KEYBOARD_JOG_AXIS_DROP_CHORD_WINDOW_MS = 160
    KEYBOARD_JOG_DIRECTION_CHANGE_CHORD_WINDOW_MS = 250
    KEYBOARD_JOG_PHYSICAL_KEY_WATCHDOG_MS = 80
    JOG_STOP_RESEND_DELAYS_MS = (80, 180, 400, 900, 1500)
    MANUAL_JOG_AXES = ("X", "Y", "Z", "A", "B", "C")
    MANUAL_AXIS_MODES = ("G91", "G90")
    LINEAR_AXES = {"X", "Y", "Z"}
    HOMING_AXES = ("X", "Y", "Z", "A")
    LINEAR_FEEDRATE_SCALE = 10
    MIN_LINEAR_FEEDRATE = 0.1
    MAX_LINEAR_FEEDRATE = 1000.0
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
    LIMIT_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #c62828; color: #ffffff; }"
        "QPushButton:pressed { background: #8e0000; }"
        "QPushButton:checked { background: #c62828; }"
        "QPushButton[homing=\"true\"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }"
        "QPushButton[homing=\"true\"]:pressed { background: #e0e0e0; }"
        "QPushButton:disabled { color: #9e9e9e; }"
    )
    HOMING_ACTIVE_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #1565c0; color: #f5f5f5; }"
        "QPushButton:pressed { background: #0d47a1; }"
        "QPushButton:checked { background: #1565c0; }"
        "QPushButton:disabled { color: #d0d0d0; }"
    )
    HOMING_ACTIVE_DIM_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #6f8fb8; color: #f5f5f5; }"
        "QPushButton:pressed { background: #5e7ea7; }"
        "QPushButton:checked { background: #6f8fb8; }"
        "QPushButton:disabled { color: #d0d0d0; }"
    )
    HOMING_PENDING_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #d8bd78; color: #1f1f1f; }"
        "QPushButton:pressed { background: #c8ad68; }"
        "QPushButton:checked { background: #d8bd78; }"
        "QPushButton:disabled { color: #6f6f6f; }"
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
        self._key_press_times: dict[Tuple[str, object], float] = {}
        self._pending_key_activations: dict[Tuple[str, object], QTimer] = {}
        self._key_bindings: Dict[tuple, tuple[str, int]] = {}
        self._linear_presets: List[float] = list(self.DEFAULT_LINEAR_FEEDRATE_PRESETS)
        self._linear_default: float = 1.0
        self._linear_jog_distance_mm: float = self.DEFAULT_JOG_DISTANCE_MM
        self._manual_axis_distance_mm: float = self.DEFAULT_MANUAL_AXIS_DISTANCE_MM
        self._manual_axis_mode = self.DEFAULT_MANUAL_AXIS_MODE
        self._manual_axis_feedrate_mm_min: float = (
            self.DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN
        )
        self._motion_safety_disabled = False
        self._show_axis_a_controls = False
        self._show_axis_b_controls = False
        self._manual_axis_controls_enabled = False
        self._applying_jog_settings = False
        self._linear_feedrate_value: float = self._linear_default
        self._linear_feedrate_bounds: tuple[float, float] | None = None
        self._last_feedrate_wheel_at = 0.0
        self._homing_buttons: dict[str, QPushButton] = {}
        self._homing_targets: dict[str, QPushButton] = {}
        self._homing_text: dict[str, str] = {}
        self._homing_overlays: dict[str, _SpinnerOverlay] = {}
        self._homed_axes: set[str] = set()
        self._limit_axes: set[str] = set()
        self._pending_homing_axes: set[str] = set()
        self._homing_spinner_angle = 0
        self._homing_blink_dimmed = False
        self._axis_a_ready = False
        self._needles_known = False
        self._needles_up = False
        self._needle_targets: dict[str, QPushButton] = {}
        self._needle_text: dict[str, str] = {}
        self._needle_overlays: dict[str, _SpinnerOverlay] = {}
        self._needle_spinner_angle = 0
        self._homing_animation_timer = QTimer(self)
        self._homing_animation_timer.setInterval(250)
        self._homing_animation_timer.timeout.connect(self._advance_homing_spinner)
        self._needle_animation_timer = QTimer(self)
        self._needle_animation_timer.setInterval(90)
        self._needle_animation_timer.timeout.connect(self._advance_needle_spinner)
        self._jog_state_sync_timer = QTimer(self)
        self._jog_state_sync_timer.setSingleShot(True)
        self._jog_state_sync_timer.setInterval(self.KEYBOARD_JOG_SYNC_DEBOUNCE_MS)
        self._jog_state_sync_timer.timeout.connect(self._sync_active_jog_state)
        self._physical_key_watchdog_timer = QTimer(self)
        self._physical_key_watchdog_timer.setInterval(
            self.KEYBOARD_JOG_PHYSICAL_KEY_WATCHDOG_MS
        )
        self._physical_key_watchdog_timer.timeout.connect(
            self._drop_released_physical_keys
        )
        self._pending_jog_axes: Optional[tuple[tuple[str, int], ...]] = None
        self._jog_stop_resend_generation = 0
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
        self.linear_feedrate_value_label = QLabel(self)
        self.linear_feedrate_value_label.setMinimumWidth(160)
        self.linear_feedrate_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        linear_feed_layout.addWidget(self.linear_feedrate_value_label)

        feed_container.addLayout(linear_feed_layout)

        self.linear_feedrate_slider = _FeedrateSlider(Qt.Horizontal, self)
        self.linear_feedrate_slider.setRange(
            int(self.MIN_LINEAR_FEEDRATE * self.LINEAR_FEEDRATE_SCALE),
            int(self.MAX_LINEAR_FEEDRATE * self.LINEAR_FEEDRATE_SCALE),
        )
        self.linear_feedrate_slider.valueChanged.connect(
            self._on_linear_feedrate_slider_changed
        )
        feed_container.addWidget(self.linear_feedrate_slider)

        root_layout.addLayout(feed_container)

        self._set_linear_feedrate(self._linear_default, reissue_if_active=False)

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

        self.extra_axis_widget = QWidget(self)
        extra_axis_layout = QVBoxLayout(self.extra_axis_widget)
        extra_axis_layout.setContentsMargins(0, 0, 0, 0)

        rotate_layout = QHBoxLayout()
        rotate_layout.addStretch(1)
        self.rotate_label = QLabel("Extra axes:", self)
        rotate_layout.addWidget(self.rotate_label)
        self.axis_a_negative_button = QPushButton("A-", self)
        self.axis_a_positive_button = QPushButton("A+", self)
        self.rotate_negative_button = QPushButton("B-", self)
        self.rotate_positive_button = QPushButton("B+", self)
        self.zero_b_button = QPushButton("Zero B", self)
        self.axis_a_negative_button.setToolTip("Move A negative")
        self.axis_a_positive_button.setToolTip("Move A positive")
        self.rotate_negative_button.setToolTip("Move B negative")
        self.rotate_positive_button.setToolTip("Move B positive")
        self.zero_b_button.setToolTip("Use the current B position as zero")
        rotate_layout.addWidget(self.axis_a_negative_button)
        rotate_layout.addWidget(self.axis_a_positive_button)
        rotate_layout.addWidget(self.rotate_negative_button)
        rotate_layout.addWidget(self.rotate_positive_button)
        rotate_layout.addWidget(self.zero_b_button)
        rotate_layout.addStretch(1)
        extra_axis_layout.addLayout(rotate_layout)

        manual_axis_layout = QHBoxLayout()
        self.manual_axis_label = QLabel("Axis:", self)
        manual_axis_layout.addWidget(self.manual_axis_label)
        self.manual_axis_combo = QComboBox(self)
        self.manual_axis_combo.addItems(self.MANUAL_JOG_AXES)
        manual_axis_layout.addWidget(self.manual_axis_combo)
        self.manual_axis_mode_label = QLabel("Mode:", self)
        manual_axis_layout.addWidget(self.manual_axis_mode_label)
        self.manual_axis_mode_combo = QComboBox(self)
        self.manual_axis_mode_combo.addItems(self.MANUAL_AXIS_MODES)
        manual_axis_layout.addWidget(self.manual_axis_mode_combo)
        self.manual_axis_distance_label = QLabel("Step:", self)
        manual_axis_layout.addWidget(self.manual_axis_distance_label)
        self.manual_axis_distance_spin = QDoubleSpinBox(self)
        self.manual_axis_distance_spin.setLocale(QLocale.c())
        self.manual_axis_distance_spin.setDecimals(3)
        self.manual_axis_distance_spin.setRange(0.001, 1000.0)
        self.manual_axis_distance_spin.setSingleStep(0.1)
        self.manual_axis_distance_spin.setSuffix(" mm")
        self.manual_axis_distance_spin.setValue(self._manual_axis_distance_mm)
        manual_axis_layout.addWidget(self.manual_axis_distance_spin)
        self.manual_axis_feedrate_label = QLabel("Feed:", self)
        self.manual_axis_feedrate_spin = QDoubleSpinBox(self)
        self.manual_axis_feedrate_spin.setLocale(QLocale.c())
        self.manual_axis_feedrate_spin.setDecimals(1)
        self.manual_axis_feedrate_spin.setRange(
            self.MIN_LINEAR_FEEDRATE,
            self.MAX_LINEAR_FEEDRATE,
        )
        self.manual_axis_feedrate_spin.setSingleStep(10.0)
        self.manual_axis_feedrate_spin.setSuffix(" mm/min")
        self.manual_axis_feedrate_spin.setValue(self._manual_axis_feedrate_mm_min)
        self.manual_axis_feedrate_label.hide()
        self.manual_axis_feedrate_spin.hide()
        manual_axis_layout.addWidget(self.manual_axis_feedrate_label)
        manual_axis_layout.addWidget(self.manual_axis_feedrate_spin)
        self.manual_axis_negative_button = QPushButton("Move -", self)
        self.manual_axis_positive_button = QPushButton("Move +", self)
        manual_axis_layout.addWidget(self.manual_axis_negative_button)
        manual_axis_layout.addWidget(self.manual_axis_positive_button)
        extra_axis_layout.addLayout(manual_axis_layout)
        root_layout.addWidget(self.extra_axis_widget)
        self.extra_axis_widget.hide()

        self.up_button.pressed.connect(lambda: self.start_jog("Y", 1))
        self.up_button.released.connect(self.stop_jog)
        self.down_button.pressed.connect(lambda: self.start_jog("Y", -1))
        self.down_button.released.connect(self.stop_jog)
        self.left_button.pressed.connect(lambda: self.start_jog("X", -1))
        self.left_button.released.connect(self.stop_jog)
        self.right_button.pressed.connect(lambda: self.start_jog("X", 1))
        self.right_button.released.connect(self.stop_jog)
        self.focus_down_button.pressed.connect(lambda: self.start_jog("Z", -1))
        self.focus_down_button.released.connect(self.stop_jog)
        self.focus_up_button.pressed.connect(lambda: self.start_jog("Z", 1))
        self.focus_up_button.released.connect(self.stop_jog)
        self.axis_a_negative_button.clicked.connect(
            lambda: self._manual_axis_step("A", -1, mode="G91")
        )
        self.axis_a_positive_button.clicked.connect(
            lambda: self._manual_axis_step("A", 1, mode="G91")
        )
        self.rotate_negative_button.clicked.connect(
            lambda: self._manual_axis_step("B", -1, mode="G91")
        )
        self.rotate_positive_button.clicked.connect(
            lambda: self._manual_axis_step("B", 1, mode="G91")
        )
        self.zero_b_button.clicked.connect(self.zero_b_requested.emit)
        self.manual_axis_negative_button.clicked.connect(
            lambda: self._manual_axis_step(self._selected_manual_axis(), -1)
        )
        self.manual_axis_positive_button.clicked.connect(
            lambda: self._manual_axis_step(self._selected_manual_axis(), 1)
        )
        self.manual_axis_combo.currentTextChanged.connect(
            lambda _text: self._emit_manual_axis_settings_changed()
        )
        self.manual_axis_mode_combo.currentTextChanged.connect(
            lambda _text: self._emit_manual_axis_settings_changed()
        )
        self.manual_axis_distance_spin.valueChanged.connect(
            lambda _value: self._emit_manual_axis_settings_changed()
        )
        self.manual_axis_feedrate_spin.valueChanged.connect(
            self._on_manual_axis_feedrate_changed
        )

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

    def _format_feedrate(self, value: float) -> str:
        text = f"{float(value):.1f} mm/min"
        if self._linear_feedrate_bounds is not None:
            min_value, max_value = self._linear_feedrate_bounds
            text += f" [{min_value:.1f}-{max_value:.1f}]"
        return text

    def _linear_feedrate_min_max(self) -> tuple[float, float]:
        if self._linear_feedrate_bounds is None:
            return (self.MIN_LINEAR_FEEDRATE, self.MAX_LINEAR_FEEDRATE)
        min_value, max_value = self._linear_feedrate_bounds
        return (
            max(self.MIN_LINEAR_FEEDRATE, float(min_value)),
            max(self.MIN_LINEAR_FEEDRATE, float(max_value)),
        )

    def _update_linear_feedrate_slider_range(self) -> None:
        minimum = int(round(self.MIN_LINEAR_FEEDRATE * self.LINEAR_FEEDRATE_SCALE))
        maximum = int(round(self.MAX_LINEAR_FEEDRATE * self.LINEAR_FEEDRATE_SCALE))
        self.linear_feedrate_slider.blockSignals(True)
        if self.linear_feedrate_slider.minimum() != minimum or self.linear_feedrate_slider.maximum() != maximum:
            self.linear_feedrate_slider.setRange(minimum, maximum)
        self.linear_feedrate_slider.blockSignals(False)
        if isinstance(self.linear_feedrate_slider, _FeedrateSlider):
            if self._linear_feedrate_bounds is None:
                self.linear_feedrate_slider.set_temporary_bounds(None, None)
            else:
                min_value, max_value = self._linear_feedrate_min_max()
                self.linear_feedrate_slider.set_temporary_bounds(
                    int(round(min_value * self.LINEAR_FEEDRATE_SCALE)),
                    int(round(max_value * self.LINEAR_FEEDRATE_SCALE)),
                )

    def _slider_value_from_feedrate(self, value: float) -> int:
        min_value, max_value = self._linear_feedrate_min_max()
        bounded = min(max_value, max(min_value, float(value)))
        return int(round(bounded * self.LINEAR_FEEDRATE_SCALE))

    def _feedrate_from_slider_value(self, slider_value: int) -> float:
        return float(slider_value) / float(self.LINEAR_FEEDRATE_SCALE)

    def _set_linear_feedrate(
        self,
        value: float,
        *,
        reissue_if_active: bool,
    ) -> None:
        min_value, max_value = self._linear_feedrate_min_max()
        bounded = min(max_value, max(min_value, float(value)))
        bounded = self._feedrate_from_slider_value(
            self._slider_value_from_feedrate(bounded)
        )
        changed = abs(bounded - self._linear_feedrate_value) > 1e-9
        self._linear_feedrate_value = bounded
        slider_value = self._slider_value_from_feedrate(bounded)
        if self.linear_feedrate_slider.value() != slider_value:
            self.linear_feedrate_slider.blockSignals(True)
            self.linear_feedrate_slider.setValue(slider_value)
            self.linear_feedrate_slider.blockSignals(False)
        self.linear_feedrate_value_label.setText(self._format_feedrate(bounded))
        if hasattr(self, "manual_axis_feedrate_spin"):
            self.manual_axis_feedrate_spin.blockSignals(True)
            self.manual_axis_feedrate_spin.setValue(bounded)
            self.manual_axis_feedrate_spin.blockSignals(False)
        if changed:
            self._manual_axis_feedrate_mm_min = bounded
            self.linear_feedrate_changed.emit(bounded)
        if reissue_if_active:
            self._restart_active_jog_with_current_feedrate()

    def set_temporary_linear_feedrate_bounds(
        self, min_feedrate: float, max_feedrate: float
    ) -> None:
        min_value = max(self.MIN_LINEAR_FEEDRATE, float(min_feedrate))
        max_value = max(min_value, float(max_feedrate))
        self._linear_feedrate_bounds = (min_value, max_value)
        self._update_linear_feedrate_slider_range()
        self._set_linear_feedrate(
            self._linear_feedrate_value,
            reissue_if_active=False,
        )

    def clear_temporary_linear_feedrate_bounds(self) -> None:
        if self._linear_feedrate_bounds is None:
            return
        self._linear_feedrate_bounds = None
        self._update_linear_feedrate_slider_range()
        self._set_linear_feedrate(
            self._linear_feedrate_value,
            reissue_if_active=False,
        )

    def _on_linear_feedrate_slider_changed(self, slider_value: int) -> None:
        self._set_linear_feedrate(
            self._feedrate_from_slider_value(slider_value),
            reissue_if_active=True,
        )

    def apply_feedrate_settings(
        self,
        linear_presets: List[float],
        linear_default: float,
        rotary_presets: List[float],
        rotary_default: float,
    ) -> None:
        """Apply only the linear default feedrate used by jog controls."""

        cleaned_linear = sorted(
            {
                max(self.MIN_LINEAR_FEEDRATE, min(self.MAX_LINEAR_FEEDRATE, float(value)))
                for value in linear_presets
                if isinstance(value, (int, float))
            }
        )
        if cleaned_linear:
            self._linear_presets = list(cleaned_linear)
        try:
            candidate = float(linear_default)
        except (TypeError, ValueError):
            candidate = self._linear_presets[0] if self._linear_presets else 10.0
        self._linear_default = min(
            self.MAX_LINEAR_FEEDRATE,
            max(self.MIN_LINEAR_FEEDRATE, candidate),
        )
        self._set_linear_feedrate(self._linear_default, reissue_if_active=False)
        logger.info(
            "Joystick feedrate settings updated: linear=%s (default=%s)",
            self._linear_presets,
            self._linear_default,
        )

    def current_linear_feedrate(self) -> float:
        return float(self._linear_feedrate_value)

    def apply_jog_settings(
        self,
        linear_distance_mm: float,
        rotary_distance_deg: float,
        motion_safety_disabled: bool = False,
        show_axis_a_controls: bool = False,
        show_axis_b_controls: bool = False,
        manual_axis_controls_enabled: bool = False,
        manual_axis: str = "A",
        manual_axis_distance_mm: float = DEFAULT_MANUAL_AXIS_DISTANCE_MM,
        manual_axis_mode: str = DEFAULT_MANUAL_AXIS_MODE,
        manual_axis_feedrate_mm_min: float = DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN,
    ) -> None:
        """Update the jog distance used for linear axes."""

        self._applying_jog_settings = True
        was_motion_safety_disabled = self._motion_safety_disabled
        self._linear_jog_distance_mm = max(0.001, float(linear_distance_mm))
        self._manual_axis_distance_mm = max(0.001, float(manual_axis_distance_mm))
        self._manual_axis_feedrate_mm_min = min(
            self.MAX_LINEAR_FEEDRATE,
            max(self.MIN_LINEAR_FEEDRATE, float(manual_axis_feedrate_mm_min)),
        )
        self._motion_safety_disabled = bool(motion_safety_disabled)
        self._show_axis_a_controls = bool(show_axis_a_controls)
        self._show_axis_b_controls = bool(show_axis_b_controls)
        self._manual_axis_controls_enabled = bool(manual_axis_controls_enabled)
        axis = manual_axis.strip().upper() if isinstance(manual_axis, str) else "A"
        if axis not in self.MANUAL_JOG_AXES:
            axis = "A"
        mode = (
            manual_axis_mode.strip().upper()
            if isinstance(manual_axis_mode, str)
            else self.DEFAULT_MANUAL_AXIS_MODE
        )
        if mode not in self.MANUAL_AXIS_MODES:
            mode = self.DEFAULT_MANUAL_AXIS_MODE
        axis_index = self.manual_axis_combo.findText(axis)
        if axis_index >= 0:
            self.manual_axis_combo.setCurrentIndex(axis_index)
        mode_index = self.manual_axis_mode_combo.findText(mode)
        if mode_index >= 0:
            self.manual_axis_mode_combo.setCurrentIndex(mode_index)
        self._manual_axis_mode = mode
        self.manual_axis_distance_spin.setValue(self._manual_axis_distance_mm)
        if self._manual_axis_controls_enabled:
            self._set_linear_feedrate(
                self._manual_axis_feedrate_mm_min,
                reissue_if_active=False,
            )
        self.manual_axis_feedrate_spin.blockSignals(True)
        self.manual_axis_feedrate_spin.setValue(self._linear_feedrate_value)
        self.manual_axis_feedrate_spin.blockSignals(False)
        self._update_extra_axis_visibility()
        self._applying_jog_settings = False
        if (
            was_motion_safety_disabled
            and not self._motion_safety_disabled
            and not self._axis_a_ready
        ):
            self.stop_jog()
            self._pending_jog_axes = None
            self._key_stack.clear()
            self._key_press_times.clear()
            self._clear_pending_key_activations()
            self._sync_physical_key_watchdog()
        self._update_enabled_state()
        logger.debug(
            "Joystick jog settings updated: linear_distance_mm=%s safety_disabled=%s axis_a=%s axis_b=%s manual=%s manual_axis=%s manual_axis_distance_mm=%s manual_mode=%s manual_feedrate_mm_min=%s",
            self._linear_jog_distance_mm,
            self._motion_safety_disabled,
            self._show_axis_a_controls,
            self._show_axis_b_controls,
            self._manual_axis_controls_enabled,
            axis,
            self._manual_axis_distance_mm,
            self._manual_axis_mode,
            self._manual_axis_feedrate_mm_min,
        )

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Assign the serial connection used for jogging commands."""

        if self.serial_connection and self.serial_connection.is_open:
            self.stop_jog()
        self.serial_connection = serial_connection
        if not serial_connection or not serial_connection.is_open:
            self._active_axes = None
            self._pending_jog_axes = None
            self._key_stack.clear()
            self._key_press_times.clear()
            self._clear_pending_key_activations()
            self._sync_physical_key_watchdog()
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
            self._pending_homing_axes.clear()
            self._stop_homing_animation("ALL")
            for axis in self.HOMING_AXES:
                self._stop_homing_animation(axis)
        self._update_enabled_state()

    def _update_extra_axis_visibility(self) -> None:
        axis_controls_visible = self._show_axis_a_controls or self._show_axis_b_controls
        self.rotate_label.setVisible(axis_controls_visible)
        self.axis_a_negative_button.setVisible(self._show_axis_a_controls)
        self.axis_a_positive_button.setVisible(self._show_axis_a_controls)
        self.rotate_negative_button.setVisible(self._show_axis_b_controls)
        self.rotate_positive_button.setVisible(self._show_axis_b_controls)
        self.zero_b_button.setVisible(self._show_axis_b_controls)
        for widget in (
            self.manual_axis_label,
            self.manual_axis_combo,
            self.manual_axis_mode_label,
            self.manual_axis_mode_combo,
            self.manual_axis_distance_label,
            self.manual_axis_distance_spin,
            self.manual_axis_feedrate_label,
            self.manual_axis_feedrate_spin,
            self.manual_axis_negative_button,
            self.manual_axis_positive_button,
        ):
            widget.setVisible(self._manual_axis_controls_enabled)
        self.extra_axis_widget.setVisible(
            axis_controls_visible or self._manual_axis_controls_enabled
        )

    def _update_enabled_state(self) -> None:
        enabled = bool(self.serial_connection and self.serial_connection.is_open)
        motion_enabled = enabled and (self._axis_a_ready or self._motion_safety_disabled)
        extra_controls_enabled = enabled and (
            self._axis_a_ready or self._motion_safety_disabled
        )
        for widget in (
            self.linear_feedrate_slider,
            self.home_all_button,
            self.needles_raise_button,
            self.needles_lower_button,
            self.unlock_button,
            self.reset_button,
            self.reset_calibration_button,
        ):
            widget.setEnabled(enabled)
        for widget in (
            self.up_button,
            self.down_button,
            self.left_button,
            self.right_button,
            self.focus_down_button,
            self.focus_up_button,
            self.autofocus_button,
        ):
            widget.setEnabled(motion_enabled)
        for widget in (
            self.axis_a_negative_button,
            self.axis_a_positive_button,
            self.rotate_negative_button,
            self.rotate_positive_button,
            self.zero_b_button,
            self.manual_axis_combo,
            self.manual_axis_mode_combo,
            self.manual_axis_distance_spin,
            self.manual_axis_feedrate_spin,
            self.manual_axis_negative_button,
            self.manual_axis_positive_button,
        ):
            widget.setEnabled(extra_controls_enabled)
        for button in self._homing_buttons.values():
            button.setEnabled(enabled)

    def set_axis_a_ready(self, ready: bool) -> None:
        self._axis_a_ready = ready
        if not ready and not self._motion_safety_disabled:
            self.stop_jog()
            self._pending_jog_axes = None
            self._key_stack.clear()
            self._key_press_times.clear()
            self._clear_pending_key_activations()
            self._sync_physical_key_watchdog()
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
        if self._motion_safety_disabled:
            if self.stage_controller is not None and self.stage_controller.is_busy():
                logger.debug("Jog blocked because stage controller is busy")
                return False
            return True
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

    def _restart_active_jog_with_current_feedrate(self) -> None:
        axes = self._active_axes
        if not axes:
            return
        self.stop_jog()
        self._apply_axes(axes)

    def stop_jog(self) -> None:
        had_active_axes = self._active_axes is not None
        logger.debug(
            "TIMING stop_jog_requested active_axes=%s key_stack=%s",
            self._active_axes,
            self._key_stack,
        )
        if not self.serial_connection or not self.serial_connection.is_open:
            self._active_axes = None
            self._pending_jog_axes = None
            self._clear_pending_key_activations()
            if had_active_axes:
                self.jog_stopped.emit()
            return
        self._active_axes = None
        self._pending_jog_axes = None
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
        commanded_distances: list[tuple[str, float]] = []
        for axis, direction in axes_sorted:
            distance = direction * self._distance_for_axis(axis)
            commanded_distances.append((axis, distance))
        if self.stage_controller is not None:
            try:
                commanded_distances = list(
                    self.stage_controller.constrain_jog_distances(
                        tuple(commanded_distances)
                    )
                )
            except Exception as error:  # pragma: no cover - UI safety guard
                self._show_warning(str(error))
                logger.exception("Failed to constrain jog command: %s", error)
                self.jog_command_changed.emit(tuple(), float(feedrate))
                return
        if not commanded_distances:
            self.jog_command_changed.emit(tuple(), float(feedrate))
            return
        parts = [f"{axis}{distance:.3f}" for axis, distance in commanded_distances]
        command = f"$J=G91 G21 {' '.join(parts)} F{feedrate}\n"
        logger.debug(
            "TIMING jog_command_prepared axes=%s feedrate=%s command=%s",
            commanded_distances,
            feedrate,
            command.strip(),
        )
        if not self.send_command(command):
            self.jog_command_changed.emit(tuple(), float(feedrate))
            return
        self._active_axes = tuple(
            (axis, direction)
            for axis, direction in axes_sorted
            if any(command_axis == axis for command_axis, _ in commanded_distances)
        )
        self.jog_command_changed.emit(tuple(commanded_distances), float(feedrate))
        logger.debug("TIMING jog_command_sent command=%s", command.strip())

    def _distance_for_axis(self, axis: str) -> float:
        if axis.upper() not in self.LINEAR_AXES:
            return self._manual_axis_distance_mm
        return self._linear_jog_distance_mm

    def _feedrate_for_axes(
        self, axes: tuple[tuple[str, int], ...]
    ) -> Optional[float]:
        has_linear = any(axis in self.LINEAR_AXES for axis, _ in axes)

        if has_linear:
            return self._linear_feedrate_value
        if self._motion_safety_disabled:
            return self._linear_feedrate_value

        return None

    def _selected_manual_axis(self) -> str:
        axis = self.manual_axis_combo.currentText().strip().upper()
        if axis not in self.MANUAL_JOG_AXES:
            return "A"
        return axis

    def _selected_manual_axis_mode(self) -> str:
        mode = self.manual_axis_mode_combo.currentText().strip().upper()
        if mode not in self.MANUAL_AXIS_MODES:
            return self.DEFAULT_MANUAL_AXIS_MODE
        return mode

    def _manual_axis_step(
        self, axis: str, direction: int, *, mode: Optional[str] = None
    ) -> None:
        if not (self._axis_a_ready or self._motion_safety_disabled):
            return
        if self.stage_controller is not None and self.stage_controller.is_busy():
            logger.debug("Manual axis step blocked because stage controller is busy")
            return
        axis = axis.strip().upper()
        if axis not in self.MANUAL_JOG_AXES:
            return
        distance = float(direction) * self._manual_axis_distance_mm
        self.motion_axis_requested.emit(axis)
        self.manual_axis_move_requested.emit(
            axis,
            distance,
            mode or self._selected_manual_axis_mode(),
            self._linear_feedrate_value,
        )

    def _on_manual_axis_feedrate_changed(self, value: float) -> None:
        self._set_linear_feedrate(float(value), reissue_if_active=True)
        self._emit_manual_axis_settings_changed()

    def _emit_manual_axis_settings_changed(self) -> None:
        if self._applying_jog_settings:
            return
        self._manual_axis_distance_mm = max(
            0.001,
            float(self.manual_axis_distance_spin.value()),
        )
        self._manual_axis_mode = self._selected_manual_axis_mode()
        self._manual_axis_feedrate_mm_min = self._linear_feedrate_value
        self.manual_axis_settings_changed.emit(
            self._selected_manual_axis(),
            self._manual_axis_distance_mm,
            self._manual_axis_mode,
            self._manual_axis_feedrate_mm_min,
        )

    def _compute_active_axes(self) -> tuple[tuple[str, int], ...]:
        axis_directions: dict[str, list[int]] = {}
        for identifier in self._key_stack:
            mapping = self._mapping_from_identifier(identifier)
            if mapping is None:
                continue
            axis, direction = mapping
            axis_directions.setdefault(axis, []).append(direction)
        unique_axes: dict[str, int] = {}
        for axis, directions in axis_directions.items():
            active_direction = self._active_direction_for_axis(axis)
            if active_direction in directions:
                unique_axes[axis] = active_direction
            else:
                unique_axes[axis] = directions[-1]
        return tuple(unique_axes.items())

    def _schedule_active_jog_update(self) -> None:
        axes = self._compute_active_axes()
        if not axes:
            self._pending_jog_axes = None
            self._clear_pending_key_activations()
            if self._jog_state_sync_timer.isActive():
                self._jog_state_sync_timer.stop()
            logger.debug(
                "Scheduled immediate jog stop: axes=%s active_axes=%s",
                axes,
                self._active_axes,
            )
            self.stop_jog()
            return
        self._pending_jog_axes = axes
        interval = self._jog_sync_interval_for_axes(axes)
        if self._jog_state_sync_timer.isActive():
            self._jog_state_sync_timer.stop()
        self._jog_state_sync_timer.setInterval(interval)
        self._jog_state_sync_timer.start()
        logger.debug(
            "Scheduled jog state sync: axes=%s interval_ms=%s active_axes=%s",
            axes,
            interval,
            self._active_axes,
        )

    def _sync_active_jog_state(self) -> None:
        axes = self._pending_jog_axes
        if axes is None:
            axes = self._compute_active_axes()
        self._pending_jog_axes = None
        logger.debug("Active keys mapped to axes: %s", axes)
        if not axes:
            self.stop_jog()
            return
        self._apply_axes(axes)

    def _jog_sync_interval_for_axes(
        self, axes: tuple[tuple[str, int], ...]
    ) -> int:
        if not axes:
            return 0
        linear_axes = [axis for axis, _direction in axes if axis in self.LINEAR_AXES]
        if not (self._active_axes or ()):
            if len(linear_axes) == 1:
                return self.KEYBOARD_JOG_DIAGONAL_CHORD_WINDOW_MS
            return 0
        active_linear_axes = [
            axis
            for axis, _direction in self._active_axes
            if axis in self.LINEAR_AXES
        ]
        for axis, direction in axes:
            active_direction = self._active_direction_for_axis(axis)
            if active_direction is not None and active_direction != direction:
                return self.KEYBOARD_JOG_DIRECTION_CHANGE_CHORD_WINDOW_MS
        if len(linear_axes) == 1 and len(active_linear_axes) >= 2:
            return self.KEYBOARD_JOG_AXIS_DROP_CHORD_WINDOW_MS
        return self.KEYBOARD_JOG_SYNC_DEBOUNCE_MS

    def _active_direction_for_axis(self, axis: str) -> Optional[int]:
        for active_axis, active_direction in self._active_axes or ():
            if active_axis == axis:
                return active_direction
        return None

    def set_homing_status(self, homed_axes: set[str]) -> None:
        active_axes = set(homed_axes).intersection(self.HOMING_AXES)
        self._homed_axes = set(active_axes)
        all_homed = active_axes == set(self.HOMING_AXES)
        for axis, button in self._homing_buttons.items():
            if axis in self._homing_targets:
                self._set_homing_button_state(
                    button,
                    axis in active_axes,
                    all_homed=all_homed,
                    limit=axis in self._limit_axes,
                    active=True,
                )
            elif axis in active_axes:
                self._set_homing_button_state(
                    button,
                    True,
                    all_homed=all_homed,
                    limit=axis in self._limit_axes,
                )
            else:
                self._set_homing_button_state(
                    button,
                    False,
                    all_homed=all_homed,
                    limit=axis in self._limit_axes,
                    pending=axis in self._pending_homing_axes,
                )
        all_pending = bool(self._pending_homing_axes)
        if "ALL" in self._homing_targets:
            self._set_homing_button_state(
                self.home_all_button,
                all_homed,
                all_homed=all_homed,
                active=True,
            )
        elif all_homed:
            self._set_homing_button_state(self.home_all_button, True, all_homed=True)
        else:
            self._set_homing_button_state(
                self.home_all_button,
                False,
                all_homed=False,
                pending=all_pending,
            )

    def _set_homing_button_state(
        self,
        button: QPushButton,
        homed: bool,
        *,
        all_homed: bool = False,
        limit: bool = False,
        pending: bool = False,
        active: bool = False,
    ) -> None:
        button.setProperty("homing", active)
        button.setProperty("all_homed", all_homed)
        button.setProperty("limit", limit)
        button.setProperty("pending", pending)
        button.setChecked(active)
        if active:
            button.setStyleSheet(
                self.HOMING_ACTIVE_DIM_STYLE
                if self._homing_blink_dimmed
                else self.HOMING_ACTIVE_STYLE
            )
        elif pending:
            button.setStyleSheet(self.HOMING_PENDING_STYLE)
        elif limit:
            button.setStyleSheet(self.LIMIT_STYLE)
        elif all_homed:
            button.setStyleSheet(self.ALL_HOMED_STYLE)
        else:
            button.setStyleSheet(self.HOMED_STYLE if homed else self.NOT_HOMED_STYLE)

    def set_pending_homing_actions(self, axes: object) -> None:
        if isinstance(axes, (set, list, tuple)):
            self._pending_homing_axes = {
                str(axis).strip().upper()
                for axis in axes
                if str(axis).strip().upper() in self.HOMING_AXES
            }
        else:
            self._pending_homing_axes = set()
        self.set_homing_status(set(self._homed_axes))

    def set_limit_axes(self, axes: object) -> None:
        if isinstance(axes, (set, list, tuple)):
            self._limit_axes = {
                str(axis).strip().upper()
                for axis in axes
                if str(axis).strip().upper() in self.HOMING_AXES
            }
        else:
            self._limit_axes = set()
        self.set_homing_status(set(self._homed_axes))

    def _home_all(self) -> None:
        self.home_all_requested.emit()

    def _home_axis(self, axis: str) -> None:
        self.home_axis_requested.emit(axis)

    def set_homing_action_started(self, axis_key: str) -> None:
        axis_key = axis_key.strip().upper()
        if axis_key == "ALL":
            self._start_homing_animation("ALL", self.home_all_button)
            return
        button = self._homing_buttons.get(axis_key)
        if button is not None:
            self._start_homing_animation(axis_key, button)

    def set_homing_action_finished(self, success: bool, message: str, axis_key: str) -> None:
        axis_key = axis_key.strip().upper()
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
        button.setProperty("homing", True)
        button.setChecked(True)
        self.set_homing_status(set(self._homed_axes))
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
        button.setEnabled(True)
        if not self._homing_targets:
            self._homing_animation_timer.stop()
            self._homing_spinner_angle = 0
            self._homing_blink_dimmed = False
        self.set_homing_status(set(self._homed_axes))

    def _advance_homing_spinner(self) -> None:
        if not self._homing_targets:
            return
        self._homing_spinner_angle = (self._homing_spinner_angle + 30) % 360
        self._homing_blink_dimmed = not self._homing_blink_dimmed
        self.set_homing_status(set(self._homed_axes))

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

    def send_command(self, command: str | bytes) -> bool:
        if not self.serial_connection or not self.serial_connection.is_open:
            logger.debug("Discarded command because serial is closed: %s", command)
            return False
        if self.stage_controller is not None:
            try:
                if isinstance(command, bytes) and command == b"\x85":
                    self.stage_controller.queue_jog_stop()
                    return True
                if isinstance(command, bytes) and command == b"\x18":
                    self.stage_controller.queue_soft_reset(source="joystick_reset_button")
                    return True
                if isinstance(command, str) and command.startswith("$J="):
                    self.stage_controller.queue_jog_command(command)
                    return True
                if isinstance(command, str):
                    self.stage_controller.queue_manual_command(command)
                    return True
            except Exception as error:  # pragma: no cover - UI safety guard
                self._show_warning(str(error))
                logger.exception("Failed to queue controller command: %s", error)
                return False
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
            return True
        except serial.SerialException as error:  # pragma: no cover - best effort guard
            self._show_warning(f"Serial communication error: {error}")
            self.set_serial(None)
            logger.exception("Serial communication error: %s", error)
            return False

    def _show_warning(self, message: str) -> None:
        QMessageBox.warning(self, "Joystick", message)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        if not self._apply_wheel_delta(event.angleDelta().y()):
            super().wheelEvent(event)
            return
        event.accept()

    def _apply_wheel_delta(self, delta_y: int) -> bool:
        if delta_y == 0:
            return False
        now = time.monotonic()
        dt = now - self._last_feedrate_wheel_at if self._last_feedrate_wheel_at else 1.0
        self._last_feedrate_wheel_at = now
        notch_units = abs(delta_y) / 120.0
        base_step = max(0.2, self._linear_feedrate_value * 0.03)
        speed_multiplier = 1.0
        if dt < 0.25:
            speed_multiplier += min(5.0, (0.25 - dt) * 12.0)
        step = base_step * notch_units * speed_multiplier
        if delta_y < 0:
            step = -step
        self._set_linear_feedrate(
            self._linear_feedrate_value + step,
            reissue_if_active=self._active_axes is not None,
        )
        logger.debug(
            "Feedrate wheel applied: delta=%s step=%s value=%s active_axes=%s",
            delta_y,
            step,
            self._linear_feedrate_value,
            self._active_axes,
        )
        return True

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if self._handle_key_press_event(event):
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._handle_key_release_event(event):
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        self._pending_jog_axes = None
        self._clear_pending_key_activations()
        self._key_stack.clear()
        self._key_press_times.clear()
        self._sync_physical_key_watchdog()
        self.stop_jog()
        super().focusOutEvent(event)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._install_event_filter()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._pending_jog_axes = None
        self._clear_pending_key_activations()
        self._key_stack.clear()
        self._key_press_times.clear()
        self._sync_physical_key_watchdog()
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
            scan_code_value = (
                native_scan_code_to_int(event.nativeScanCode())
                if hasattr(event, "nativeScanCode")
                else 0
            )
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
                "Global key event: type=%s key=%s scan=%s text=%r modifiers=%s source=%s",
                event_type_name,
                key_value,
                scan_code_value,
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
        elif event.type() == QEvent.Wheel:
            if self._should_process_global_event(obj) and self._handle_wheel_event(event, obj):
                event.accept()
                return True
        return super().eventFilter(obj, event)

    def _should_process_global_event(self, obj) -> bool:
        if not self.isVisible():
            logger.debug("Ignoring global key event because joystick is hidden")
            return False
        window = self.window()
        app = QApplication.instance()
        active_window = app.activeWindow() if app is not None else None
        if window is None:
            logger.debug("Ignoring global key event because joystick window is unavailable")
            return False
        if active_window is None:
            logger.debug("Ignoring global key event because application has no active window")
            return False
        if active_window is not window and obj is not active_window:
            try:
                obj_window = obj.window() if hasattr(obj, "window") else None
            except RuntimeError:
                obj_window = None
            if obj_window is not window:
                logger.debug(
                    "Ignoring global key event because active window does not belong to joystick host"
                )
                return False
        if not window.isActiveWindow() and active_window is not window:
            logger.debug("Ignoring global key event because joystick host window is not active")
            return False
        if not (self._axis_a_ready or self._motion_safety_disabled):
            logger.debug("Ignoring global key event because A axis is not homed/zero")
            return False
        focus_widget = app.focusWidget() if app else None
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
                "Ignored auto-repeat key press: key=%s scan=%s text=%s modifiers=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
            )
            return True
        identifier, mapping = self._mapping_from_event(event)
        if identifier and mapping:
            logger.debug(
                "TIMING keypress_received key=%s scan=%s text=%s modifiers=%s mapping=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            self._register_pressed_mapping(identifier, mapping)
            event.accept()
            logger.debug(
                "Processed key press: key=%s scan=%s text=%s modifiers=%s -> %s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            return True
        logger.debug(
            "No mapping for key press: key=%s scan=%s text=%s modifiers=%s",
            event.key(),
            self._event_scan_code(event),
            event.text(),
            keyboard_modifiers_to_int(event.modifiers()),
        )
        return False

    def _handle_key_release_event(self, event) -> bool:
        if event.isAutoRepeat():
            event.ignore()
            logger.debug(
                "Ignored auto-repeat key release: key=%s scan=%s text=%s modifiers=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
            )
            return True
        identifier, mapping = self._mapping_from_event(event)
        if identifier and mapping:
            if self._cancel_pending_key_activation(identifier):
                self._key_press_times.pop(identifier, None)
                event.accept()
                logger.debug(
                    "Cancelled pending key activation: key=%s scan=%s text=%s modifiers=%s mapping=%s",
                    event.key(),
                    self._event_scan_code(event),
                    event.text(),
                    keyboard_modifiers_to_int(event.modifiers()),
                    mapping,
                )
                return True
            if identifier in self._key_stack:
                self._release_key_identifier(identifier)
            event.accept()
            logger.debug(
                "TIMING keyrelease_received key=%s scan=%s text=%s modifiers=%s mapping=%s remaining=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
                self._key_stack,
            )
            logger.debug(
                "Processed key release: key=%s scan=%s text=%s modifiers=%s -> %s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            return True
        removed = self._remove_stale_key(event)
        if removed:
            self._promote_pending_keys_if_needed()
            self._schedule_active_jog_update()
            event.accept()
            logger.debug(
                "Recovered key release: key=%s scan=%s text=%s modifiers=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
            )
            return True
        logger.debug(
            "No mapping for key release: key=%s scan=%s text=%s modifiers=%s",
            event.key(),
            self._event_scan_code(event),
            event.text(),
            keyboard_modifiers_to_int(event.modifiers()),
        )
        return False

    def _release_key_identifier(self, identifier: Tuple[str, object]) -> bool:
        if identifier not in self._key_stack:
            return False
        self._key_stack.remove(identifier)
        self._key_press_times.pop(identifier, None)
        self._promote_pending_keys_if_needed()
        self._sync_physical_key_watchdog()
        self._schedule_active_jog_update()
        return True

    def _handle_wheel_event(self, event, obj) -> bool:
        widget = obj if isinstance(obj, QWidget) else None
        if self._is_text_entry_widget(widget) or self._is_terminal_widget(widget):
            return False
        return self._apply_wheel_delta(event.angleDelta().y())

    def _remove_stale_key(self, event) -> bool:
        if not self._key_stack:
            return False
        key = event.key()
        scan_code = self._event_scan_code(event)
        removed = False
        for identifier in list(self._key_stack):
            kind, value = identifier
            if kind == "scan":
                if isinstance(value, tuple) and value[0] == scan_code and scan_code:
                    self._key_stack.remove(identifier)
                    self._key_press_times.pop(identifier, None)
                    removed = True
            elif kind == "key":
                if isinstance(value, tuple) and value[0] == key:
                    self._key_stack.remove(identifier)
                    self._key_press_times.pop(identifier, None)
                    removed = True
        if removed:
            self._sync_physical_key_watchdog()
        return removed

    def _register_pressed_mapping(
        self, identifier: Tuple[str, object], mapping: tuple[str, int]
    ) -> None:
        if identifier in self._key_stack:
            return
        if identifier in self._pending_key_activations:
            return
        self._key_press_times[identifier] = time.monotonic()

        axis, _direction = mapping
        active_axes = {active_axis for active_axis, _ in (self._active_axes or ())}
        pressed_axes = {
            axis_name
            for pending_identifier in self._key_stack
            if (resolved := self._mapping_from_identifier(pending_identifier)) is not None
            for axis_name, _ in (resolved,)
        }
        current_axes = active_axes.union(pressed_axes)

        if not current_axes or axis in current_axes or axis in self.LINEAR_AXES:
            self._key_stack.append(identifier)
            self._sync_physical_key_watchdog()
            self._schedule_active_jog_update()
            return

        self._schedule_pending_key_activation(
            identifier,
            mapping,
            activation_delay_ms=self._secondary_axis_activation_delay_ms(),
        )

    def _schedule_pending_key_activation(
        self,
        identifier: Tuple[str, object],
        mapping: tuple[str, int],
        *,
        activation_delay_ms: int,
    ) -> None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(int(max(0, activation_delay_ms)))
        timer.timeout.connect(
            lambda ident=identifier, resolved_mapping=mapping: self._activate_pending_key(
                ident, resolved_mapping
            )
        )
        self._pending_key_activations[identifier] = timer
        timer.start()
        self._sync_physical_key_watchdog()
        logger.debug(
            "Deferred secondary axis activation for %s by %s ms mapping=%s",
            identifier,
            int(max(0, activation_delay_ms)),
            mapping,
        )

    def _activate_pending_key(
        self, identifier: Tuple[str, object], mapping: tuple[str, int]
    ) -> None:
        timer = self._pending_key_activations.pop(identifier, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        if identifier in self._key_stack:
            return
        self._key_stack.append(identifier)
        self._sync_physical_key_watchdog()
        self._schedule_active_jog_update()
        logger.debug("Activated deferred secondary axis for %s mapping=%s", identifier, mapping)

    def _cancel_pending_key_activation(self, identifier: Tuple[str, object]) -> bool:
        timer = self._pending_key_activations.pop(identifier, None)
        if timer is None:
            return False
        timer.stop()
        timer.deleteLater()
        return True

    def _clear_pending_key_activations(self) -> None:
        for identifier in list(self._pending_key_activations.keys()):
            self._cancel_pending_key_activation(identifier)

    def _promote_pending_keys_if_needed(self) -> None:
        if self._key_stack:
            return
        if not self._pending_key_activations:
            return
        for identifier in list(self._pending_key_activations.keys()):
            mapping = self._mapping_from_identifier(identifier)
            if mapping is None:
                self._cancel_pending_key_activation(identifier)
                continue
            self._activate_pending_key(identifier, mapping)

    def _secondary_axis_activation_delay_ms(self) -> int:
        linear_press_times = [
            self._key_press_times.get(identifier)
            for identifier in self._key_stack
            if (resolved := self._mapping_from_identifier(identifier)) is not None
            and resolved[0] in self.LINEAR_AXES
        ]
        linear_press_times = [
            float(value) for value in linear_press_times if isinstance(value, (int, float))
        ]
        if not linear_press_times:
            return self.KEYBOARD_JOG_SECONDARY_AXIS_ACTIVATION_MS
        elapsed_ms = (time.monotonic() - max(linear_press_times)) * 1000.0
        if elapsed_ms <= self.KEYBOARD_JOG_DIAGONAL_CHORD_WINDOW_MS:
            logger.debug(
                "Immediate diagonal chord accepted: elapsed_ms=%.1f threshold_ms=%s",
                elapsed_ms,
                self.KEYBOARD_JOG_DIAGONAL_CHORD_WINDOW_MS,
            )
            return 0
        return self.KEYBOARD_JOG_SECONDARY_AXIS_ACTIVATION_MS

    def _schedule_jog_stop_resend(self) -> None:
        self._jog_stop_resend_generation += 1
        generation = self._jog_stop_resend_generation

        def resend(expected_generation: int) -> None:
            if expected_generation != self._jog_stop_resend_generation:
                return
            if self._key_stack or self._active_axes is not None:
                return
            if not self.serial_connection or not self.serial_connection.is_open:
                return
            self.send_command(b"\x85")
            logger.debug("Resent stop jog command")

        for delay_ms in self.JOG_STOP_RESEND_DELAYS_MS:
            QTimer.singleShot(
                delay_ms,
                lambda expected_generation=generation: resend(expected_generation),
            )

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
        scan_code = self._event_scan_code(event)
        modifiers = keyboard_modifiers_to_int(event.modifiers())

        if scan_code:
            mapping = self._key_bindings.get(("scan", scan_code, modifiers))
            if mapping:
                return ("scan", (scan_code, modifiers)), mapping
            for identifier, mapping in self._key_bindings.items():
                if identifier[0] == "scan" and identifier[1] == scan_code:
                    return ("scan", (identifier[1], identifier[2])), mapping

        mapping = self._key_bindings.get(("key", key, modifiers))
        if mapping:
            return ("key", (key, modifiers)), mapping

        for identifier, mapping in self._key_bindings.items():
            if identifier[0] == "key" and identifier[1] == key:
                return ("key", (identifier[1], identifier[2])), mapping

        return (None, None)

    def _mapping_from_identifier(self, identifier: Tuple[str, object]) -> Optional[tuple[str, int]]:
        kind, value = identifier
        if kind == "scan":
            scan_code, modifiers = value  # type: ignore[misc]
            return self._key_bindings.get(("scan", scan_code, modifiers))
        if kind == "key":
            key, modifiers = value  # type: ignore[misc]
            return self._key_bindings.get(("key", key, modifiers))
        return None

    def apply_control_bindings(self, bindings: Dict[str, list[KeyBinding]]) -> None:
        """Update the joystick key map based on the provided settings."""

        mapping: Dict[tuple, tuple[str, int]] = {}
        for action in CONTROL_ACTIONS:
            for binding in bindings.get(action.key, []):
                scan_code = int(binding.native_scan_code or 0)
                if not scan_code:
                    scan_code = derive_native_scan_code_from_qt_key(binding.qt_key)
                if scan_code:
                    mapping[("scan", scan_code, binding.modifiers)] = (
                        action.axis,
                        action.direction,
                    )
                mapping[("key", binding.qt_key, binding.modifiers)] = (
                    action.axis,
                    action.direction,
                )
        self._key_bindings = mapping
        self._key_stack = [
            identifier
            for identifier in self._key_stack
            if self._mapping_from_identifier(identifier) is not None
        ]
        self._key_press_times = {
            identifier: timestamp
            for identifier, timestamp in self._key_press_times.items()
            if self._mapping_from_identifier(identifier) is not None
        }
        self._sync_physical_key_watchdog()
        logger.info("Joystick key bindings updated: %d entries", len(self._key_bindings))

    def _sync_physical_key_watchdog(self) -> None:
        if self._key_stack or self._pending_key_activations:
            if not self._physical_key_watchdog_timer.isActive():
                self._physical_key_watchdog_timer.start()
            return
        if self._physical_key_watchdog_timer.isActive():
            self._physical_key_watchdog_timer.stop()

    def _drop_released_physical_keys(self) -> None:
        if not self._key_stack and not self._pending_key_activations:
            self._sync_physical_key_watchdog()
            return

        removed: list[Tuple[str, object]] = []
        for identifier in list(self._key_stack):
            is_down = self._physical_key_is_down(identifier)
            if is_down is False:
                self._key_stack.remove(identifier)
                self._key_press_times.pop(identifier, None)
                removed.append(identifier)
        for identifier in list(self._pending_key_activations.keys()):
            is_down = self._physical_key_is_down(identifier)
            if is_down is False:
                self._cancel_pending_key_activation(identifier)
                self._key_press_times.pop(identifier, None)
                removed.append(identifier)

        if not removed:
            self._sync_physical_key_watchdog()
            return

        logger.warning(
            "Recovered lost keyboard release for jog: removed=%s remaining=%s",
            removed,
            self._key_stack,
        )
        self._promote_pending_keys_if_needed()
        self._sync_physical_key_watchdog()
        self._schedule_active_jog_update()

    @staticmethod
    def _physical_key_is_down(identifier: Tuple[str, object]) -> Optional[bool]:
        if not sys.platform.startswith("win"):
            return None
        try:
            kind, value = identifier
            if not isinstance(value, tuple) or not value:
                return None
            vk_code = 0
            if kind == "scan":
                scan_code = int(value[0] or 0)
                if not scan_code:
                    return None
                vk_code = int(ctypes.windll.user32.MapVirtualKeyW(scan_code, 3))
            elif kind == "key":
                vk_code = int(value[0] or 0)
            if not (0 < vk_code <= 0xFF):
                return None
            return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)
        except Exception:
            logger.debug(
                "Unable to read physical key state for %s",
                identifier,
                exc_info=True,
            )
            return None

    @staticmethod
    def _event_scan_code(event) -> int:
        native_scan = getattr(event, "nativeScanCode", None)
        if native_scan is None:
            return 0
        if callable(native_scan):
            return native_scan_code_to_int(native_scan())
        return native_scan_code_to_int(native_scan)
