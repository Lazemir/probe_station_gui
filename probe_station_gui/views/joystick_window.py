"""Interactive joystick window for jogging the stage via serial commands."""

from __future__ import annotations

import ctypes
import logging
import math
import sys
import time
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import serial
from PySide6.QtCore import QEvent, QLocale, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QCloseEvent, QDoubleValidator, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
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


class _NeedleContactCoordinateEdit(QWidget):
    """Temporary editor for the saved needle contact A coordinate."""

    accepted = Signal(float)
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NeedleContactCoordinateEdit")
        self._line_edit = QLineEdit(self)
        validator = QDoubleValidator(self._line_edit)
        validator.setLocale(QLocale.c())
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._line_edit.setValidator(validator)
        self._line_edit.setAlignment(Qt.AlignCenter)
        self._line_edit.installEventFilter(self)
        self._line_edit.setContextMenuPolicy(Qt.CustomContextMenu)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._line_edit, 1)

        for widget in (self, self._line_edit):
            font = widget.font()
            font.setBold(False)
            widget.setFont(font)
        self.setStyleSheet(
            "#NeedleContactCoordinateEdit QLineEdit { font-weight: normal; }"
        )
        self.hide()
        self._accepting = False
        self._cancel_on_focus_out = True

    def set_cancel_on_focus_out(self, enabled: bool) -> None:
        self._cancel_on_focus_out = bool(enabled)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API style
        self._line_edit.setText(text)

    def clear(self) -> None:
        self._line_edit.clear()

    def setModified(self, modified: bool) -> None:  # noqa: N802 - Qt API style
        self._line_edit.setModified(modified)

    def selectAll(self) -> None:  # noqa: N802 - Qt API style
        self._line_edit.selectAll()

    def value(self) -> float | None:
        if not self._line_edit.hasAcceptableInput():
            return None
        return float(self._line_edit.text().strip())

    def line_edit(self) -> QLineEdit:
        return self._line_edit

    def setFocus(self, reason: Qt.FocusReason = Qt.OtherFocusReason) -> None:  # type: ignore[override]
        self._line_edit.setFocus(reason)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self._line_edit and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._accept_current_text()
                event.accept()
                return True
            if event.key() == Qt.Key_Escape:
                self.cancelled.emit()
                event.accept()
                return True
        if (
            obj is self._line_edit
            and event.type() == QEvent.FocusOut
            and not self._accepting
            and self._cancel_on_focus_out
        ):
            self.cancelled.emit()
        return super().eventFilter(obj, event)

    def _accept_current_text(self) -> None:
        value = self.value()
        if value is None:
            QApplication.beep()
            return
        self._accepting = True
        try:
            self.accepted.emit(value)
        finally:
            self._accepting = False


class JoystickWindow(QWidget):
    """Widget that provides directional jogging controls."""

    autofocus_requested = Signal()
    reset_requested = Signal()
    motion_axis_requested = Signal(str)
    jog_command_changed = Signal(object, float)
    jog_stopped = Signal()
    home_axis_requested = Signal(str)
    home_all_requested = Signal()
    needles_raise_requested = Signal(float)
    needles_lift_requested = Signal(float)
    needles_lower_requested = Signal(float)
    needle_current_lower_contact_save_requested = Signal()
    needle_contact_coordinate_save_requested = Signal(str, float)
    manual_axis_move_requested = Signal(str, float, str, float)
    manual_axis_settings_changed = Signal(str, float, str, float)
    control_mode_changed = Signal(str)
    linear_feedrate_changed = Signal(float)
    step_feedrate_changed = Signal(float)
    focus_feedrate_changed = Signal(float)
    focus_step_feedrate_changed = Signal(float)
    needle_feedrate_changed = Signal(float)
    needle_step_feedrate_changed = Signal(float)
    turntable_feedrate_changed = Signal(float)
    turntable_step_feedrate_changed = Signal(float)
    common_feedrate_changed = Signal(float)
    zero_b_requested = Signal()

    DEFAULT_JOG_DISTANCE_MM = 25.0
    DEFAULT_ROTATE_DISTANCE_DEG = 5.0
    DEFAULT_MANUAL_AXIS_DISTANCE_MM = 1.0
    DEFAULT_MANUAL_AXIS_MODE = "G91"
    DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN = 1.0
    DEFAULT_NEEDLE_FEEDRATE_MM_MIN = 1.0
    DEFAULT_NEEDLE_STEP_FEEDRATE_MM_MIN = 1.0
    DEFAULT_FOCUS_FEEDRATE_MM_MIN = 1.0
    DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN = 1.0
    DEFAULT_TURNTABLE_FEEDRATE_MM_MIN = 1.0
    DEFAULT_TURNTABLE_STEP_FEEDRATE_MM_MIN = 1.0
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
    MIN_LINEAR_FEEDRATE = 1.0
    MAX_LINEAR_FEEDRATE = 1000.0
    MODE_JOG = "jog"
    MODE_STEP = "step"
    FEED_TARGET_XY = "xy"
    FEED_TARGET_FOCUS = "focus"
    FEED_TARGET_NEEDLES = "needles"
    FEED_TARGET_TURNTABLE = "turntable"
    FEED_TARGET_COMMON = "common"
    FEED_TARGET_ORDER = (
        FEED_TARGET_XY,
        FEED_TARGET_FOCUS,
        FEED_TARGET_NEEDLES,
        FEED_TARGET_TURNTABLE,
    )
    FEED_TARGET_LABELS = {
        FEED_TARGET_XY: "XY",
        FEED_TARGET_FOCUS: "Z Focus",
        FEED_TARGET_NEEDLES: "A Needles",
        FEED_TARGET_TURNTABLE: "B Turntable",
        FEED_TARGET_COMMON: "Common",
    }
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
        "QPushButton { padding: 2px 6px; border: 1px solid transparent; border-radius: 4px; background: #1565c0; color: #f5f5f5; }"
        "QPushButton:pressed { background: #0d47a1; }"
        "QPushButton:checked { background: #1565c0; }"
        "QPushButton[homing=\"true\"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }"
        "QPushButton[homing=\"true\"]:pressed { background: #e0e0e0; }"
        "QPushButton:disabled { color: #9e9e9e; }"
    )
    NEEDLES_DOWN_STYLE = (
        "QPushButton { padding: 2px 6px; border: 1px solid transparent; border-radius: 4px; background: #f0b429; color: #1f1f1f; }"
        "QPushButton:pressed { background: #d89b19; }"
        "QPushButton:checked { background: #f0b429; }"
        "QPushButton[homing=\"true\"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }"
        "QPushButton[homing=\"true\"]:pressed { background: #e0e0e0; }"
        "QPushButton:disabled { color: #9e9e9e; }"
    )
    NEEDLES_ACTIVE_STYLE = NEEDLES_DOWN_STYLE
    NEEDLES_ACTIVE_DIM_STYLE = (
        "QPushButton { padding: 2px 6px; border: 1px solid transparent; border-radius: 4px; background: #d8bd78; color: #1f1f1f; }"
        "QPushButton:pressed { background: #c8ad68; }"
        "QPushButton:checked { background: #d8bd78; }"
        "QPushButton:disabled { color: #6f6f6f; }"
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
        self._linear_default: float = self.MIN_LINEAR_FEEDRATE
        self._linear_jog_distance_mm: float = self.DEFAULT_JOG_DISTANCE_MM
        self._rotary_jog_distance_deg: float = self.DEFAULT_ROTATE_DISTANCE_DEG
        self._manual_axis_distance_mm: float = self.DEFAULT_MANUAL_AXIS_DISTANCE_MM
        self._manual_axis_mode = self.DEFAULT_MANUAL_AXIS_MODE
        self._manual_axis_feedrate_mm_min: float = (
            self.DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN
        )
        self._needle_feedrate_value: float = self.DEFAULT_NEEDLE_FEEDRATE_MM_MIN
        self._needle_step_feedrate_value: float = (
            self.DEFAULT_NEEDLE_STEP_FEEDRATE_MM_MIN
        )
        self._focus_feedrate_value: float = self.DEFAULT_FOCUS_FEEDRATE_MM_MIN
        self._focus_step_feedrate_value: float = (
            self.DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN
        )
        self._turntable_feedrate_value: float = (
            self.DEFAULT_TURNTABLE_FEEDRATE_MM_MIN
        )
        self._turntable_step_feedrate_value: float = (
            self.DEFAULT_TURNTABLE_STEP_FEEDRATE_MM_MIN
        )
        self._common_feedrate_value: float = self._linear_default
        self._common_feedrate_max: float | None = None
        self._motion_safety_disabled = False
        self._applying_jog_settings = False
        self._control_mode = self.MODE_JOG
        self._active_feedrate_target = self.FEED_TARGET_XY
        self._axis_feedrate_limits: dict[str, float] = {}
        self._linear_feedrate_value: float = self._linear_default
        self._feedrate_values: dict[str, float] = {
            self._feedrate_key(self.FEED_TARGET_XY, self.MODE_JOG): (
                self._linear_feedrate_value
            ),
            self._feedrate_key(self.FEED_TARGET_XY, self.MODE_STEP): (
                self._manual_axis_feedrate_mm_min
            ),
            self._feedrate_key(self.FEED_TARGET_FOCUS, self.MODE_JOG): (
                self._focus_feedrate_value
            ),
            self._feedrate_key(self.FEED_TARGET_FOCUS, self.MODE_STEP): (
                self._focus_step_feedrate_value
            ),
            self._feedrate_key(self.FEED_TARGET_NEEDLES, self.MODE_JOG): (
                self._needle_feedrate_value
            ),
            self._feedrate_key(self.FEED_TARGET_NEEDLES, self.MODE_STEP): (
                self._needle_step_feedrate_value
            ),
            self._feedrate_key(self.FEED_TARGET_TURNTABLE, self.MODE_JOG): (
                self._turntable_feedrate_value
            ),
            self._feedrate_key(self.FEED_TARGET_TURNTABLE, self.MODE_STEP): (
                self._turntable_step_feedrate_value
            ),
        }
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
        self._needles_zone: str | None = None
        self._needle_targets: dict[str, QPushButton] = {}
        self._needle_text: dict[str, str] = {}
        self._needle_contact_coordinate_edit: _NeedleContactCoordinateEdit | None = None
        self._needle_contact_coordinate_button: QPushButton | None = None
        self._saved_needle_contact_a_coordinates: dict[str, float] = {}
        self._needle_blink_dimmed = False
        self._homing_animation_timer = QTimer(self)
        self._homing_animation_timer.setInterval(250)
        self._homing_animation_timer.timeout.connect(self._advance_homing_spinner)
        self._needle_animation_timer = QTimer(self)
        self._needle_animation_timer.setInterval(250)
        self._needle_animation_timer.timeout.connect(self._advance_needle_blink)
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

        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Mode:", self))
        self.jog_mode_combo = QComboBox(self)
        self.jog_mode_combo.addItem("Jog", self.MODE_JOG)
        self.jog_mode_combo.addItem("Step", self.MODE_STEP)
        mode_layout.addWidget(self.jog_mode_combo)
        self.step_distance_label = QLabel("Step:", self)
        self.step_distance_spin = QDoubleSpinBox(self)
        self.step_distance_spin.setLocale(QLocale.c())
        self.step_distance_spin.setDecimals(3)
        self.step_distance_spin.setRange(0.001, 1000.0)
        self.step_distance_spin.setSingleStep(0.1)
        self.step_distance_spin.setSuffix(" mm/deg")
        self.step_distance_spin.setValue(self._manual_axis_distance_mm)
        mode_layout.addWidget(self.step_distance_label)
        mode_layout.addWidget(self.step_distance_spin)
        root_layout.addLayout(mode_layout)

        feed_layout = QHBoxLayout()
        feed_layout.addWidget(QLabel("Feed for:", self))
        self.feedrate_target_combo = QComboBox(self)
        for target in self.FEED_TARGET_ORDER:
            self.feedrate_target_combo.addItem(
                self.FEED_TARGET_LABELS[target],
                target,
            )
        feed_layout.addWidget(self.feedrate_target_combo)
        self.linear_feedrate_slider = _FeedrateSlider(Qt.Horizontal, self)
        self.linear_feedrate_slider.setRange(
            int(self.MIN_LINEAR_FEEDRATE * self.LINEAR_FEEDRATE_SCALE),
            int(self.MIN_LINEAR_FEEDRATE * self.LINEAR_FEEDRATE_SCALE),
        )
        self.linear_feedrate_slider.valueChanged.connect(
            self._on_linear_feedrate_slider_changed
        )
        feed_layout.addWidget(self.linear_feedrate_slider, 1)
        self.linear_feedrate_spin = QDoubleSpinBox(self)
        self.linear_feedrate_spin.setLocale(QLocale.c())
        self.linear_feedrate_spin.setDecimals(1)
        self.linear_feedrate_spin.setRange(
            self.MIN_LINEAR_FEEDRATE,
            self.MIN_LINEAR_FEEDRATE,
        )
        self.linear_feedrate_spin.setSingleStep(10.0)
        self.linear_feedrate_spin.setSuffix(" mm/min")
        self.linear_feedrate_spin.setFixedWidth(112)
        self.linear_feedrate_spin.valueChanged.connect(
            self._on_linear_feedrate_spin_changed
        )
        feed_layout.addWidget(self.linear_feedrate_spin)

        root_layout.addLayout(feed_layout)

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

        axis_layout = QGridLayout()
        focus_label = QLabel("Focus (Z):", self)
        self.focus_down_button = QPushButton("Z-", self)
        self.focus_up_button = QPushButton("Z+", self)
        self.focus_down_button.setToolTip("Focus down (Z-)")
        self.focus_up_button.setToolTip("Focus up (Z+)")
        axis_layout.addWidget(focus_label, 0, 0)
        axis_layout.addWidget(self.focus_down_button, 0, 1)
        axis_layout.addWidget(self.focus_up_button, 0, 2)

        axis_a_label = QLabel("Needles (A):", self)
        self.axis_a_negative_button = QPushButton("A-", self)
        self.axis_a_positive_button = QPushButton("A+", self)
        self.axis_a_negative_button.setToolTip("Move needles down (A-)")
        self.axis_a_positive_button.setToolTip("Move needles up (A+)")
        axis_layout.addWidget(axis_a_label, 1, 0)
        axis_layout.addWidget(self.axis_a_negative_button, 1, 1)
        axis_layout.addWidget(self.axis_a_positive_button, 1, 2)

        turntable_label = QLabel("Turntable (B):", self)
        self.rotate_negative_button = QPushButton("↺", self)
        self.rotate_positive_button = QPushButton("↻", self)
        self.zero_b_button = QPushButton("Zero B", self)
        self.rotate_negative_button.setToolTip("Rotate B counter-clockwise")
        self.rotate_positive_button.setToolTip("Rotate B clockwise")
        self.zero_b_button.setToolTip("Use the current B position as zero")
        axis_layout.addWidget(turntable_label, 2, 0)
        axis_layout.addWidget(self.rotate_negative_button, 2, 1)
        axis_layout.addWidget(self.rotate_positive_button, 2, 2)
        axis_layout.addWidget(self.zero_b_button, 2, 3)
        root_layout.addLayout(axis_layout)

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
        self.axis_a_negative_button.pressed.connect(lambda: self.start_jog("A", -1))
        self.axis_a_negative_button.released.connect(self.stop_jog)
        self.axis_a_positive_button.pressed.connect(lambda: self.start_jog("A", 1))
        self.axis_a_positive_button.released.connect(self.stop_jog)
        self.rotate_negative_button.pressed.connect(lambda: self.start_jog("B", -1))
        self.rotate_negative_button.released.connect(self.stop_jog)
        self.rotate_positive_button.pressed.connect(lambda: self.start_jog("B", 1))
        self.rotate_positive_button.released.connect(self.stop_jog)
        self.zero_b_button.clicked.connect(self.zero_b_requested.emit)
        self.jog_mode_combo.currentIndexChanged.connect(
            lambda _index: self._on_jog_mode_changed()
        )
        self.feedrate_target_combo.currentIndexChanged.connect(
            lambda _index: self._on_feedrate_target_changed()
        )
        self.step_distance_spin.valueChanged.connect(
            lambda _value: self._emit_manual_axis_settings_changed()
        )
        self._update_mode_controls()

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
        self.needle_feedrate_spin = QDoubleSpinBox(self)
        self.needle_feedrate_spin.setLocale(QLocale.c())
        self.needle_feedrate_spin.setDecimals(1)
        self.needle_feedrate_spin.setRange(
            self.MIN_LINEAR_FEEDRATE,
            self.MIN_LINEAR_FEEDRATE,
        )
        self.needle_feedrate_spin.setSingleStep(10.0)
        self.needle_feedrate_spin.setSuffix(" mm/min")
        self.needle_feedrate_spin.setValue(self._needle_feedrate_value)
        self.needle_feedrate_spin.setFixedWidth(96)
        self.needle_feedrate_spin.setToolTip("Needle A feedrate")
        self.needles_raise_button = QPushButton("Raise", self)
        self.needles_lift_button = QPushButton("Lift", self)
        self.needles_lower_button = QPushButton("Lower", self)
        self.needles_raise_button.setCheckable(True)
        self.needles_lift_button.setCheckable(True)
        self.needles_lower_button.setCheckable(True)
        self.needles_raise_button.setToolTip(
            "Raise needles to the top A position."
        )
        self.needles_lift_button.setToolTip(
            "Lift needles to the upper edge of the contact zone."
        )
        self.needles_lower_button.setToolTip(
            "Lower needles to the saved contact A0 position. Right-click to save current A as the contact position."
        )
        needle_button_width = max(
            self.needles_raise_button.sizeHint().width(),
            self.needles_lift_button.sizeHint().width(),
            self.needles_lower_button.sizeHint().width(),
        ) + 2
        needle_button_height = max(
            self.needles_raise_button.sizeHint().height(),
            self.needles_lift_button.sizeHint().height(),
            self.needles_lower_button.sizeHint().height(),
        ) + 2
        self.needles_raise_button.setFixedSize(
            needle_button_width,
            needle_button_height,
        )
        self.needles_lift_button.setFixedSize(
            needle_button_width,
            needle_button_height,
        )
        self.needles_lower_button.setFixedSize(
            needle_button_width,
            needle_button_height,
        )
        self.needles_raise_button.clicked.connect(self._raise_needles)
        self.needles_lift_button.clicked.connect(self._lift_needles)
        self.needles_lower_button.clicked.connect(self._lower_needles)
        self.needles_lower_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.needles_lower_button.customContextMenuRequested.connect(
            lambda _pos: self._save_lower_needle_contact_from_current_position()
        )
        self.needle_feedrate_spin.valueChanged.connect(
            self._on_needle_feedrate_changed
        )
        needles_layout.addWidget(self.needle_feedrate_spin)
        needles_layout.addWidget(self.needles_raise_button)
        needles_layout.addWidget(self.needles_lift_button)
        needles_layout.addWidget(self.needles_lower_button)
        root_layout.addLayout(needles_layout)

        self._needle_contact_coordinate_edit = _NeedleContactCoordinateEdit(self)
        self._needle_contact_coordinate_edit.accepted.connect(
            self._save_needle_contact_coordinate
        )
        self._needle_contact_coordinate_edit.cancelled.connect(
            self._cancel_needle_contact_coordinate_edit
        )
        self._needle_contact_coordinate_edit.line_edit().customContextMenuRequested.connect(
            self._show_active_needle_contact_coordinate_menu
        )

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

    def _feedrate_key(self, target: str, mode: str | None = None) -> str:
        target_key = str(target).strip().lower()
        if target_key not in self.FEED_TARGET_LABELS:
            target_key = self.FEED_TARGET_XY
        mode_key = str(mode or self._control_mode).strip().lower()
        if mode_key not in {self.MODE_JOG, self.MODE_STEP}:
            mode_key = self.MODE_JOG
        return f"{mode_key}:{target_key}"

    def _feedrate_value_for_target(self, target: str, mode: str | None = None) -> float:
        mode_key = str(mode or self._control_mode).strip().lower()
        if mode_key not in {self.MODE_JOG, self.MODE_STEP}:
            mode_key = self.MODE_JOG
        key = self._feedrate_key(target, mode_key)
        if key in self._feedrate_values:
            return self._feedrate_values[key]
        if target == self.FEED_TARGET_COMMON:
            return self._common_feedrate_value
        if target == self.FEED_TARGET_XY:
            return (
                self._manual_axis_feedrate_mm_min
                if mode_key == self.MODE_STEP
                else self._linear_default
            )
        if target == self.FEED_TARGET_FOCUS:
            return (
                self._focus_step_feedrate_value
                if mode_key == self.MODE_STEP
                else self._focus_feedrate_value
            )
        if target == self.FEED_TARGET_NEEDLES:
            return (
                self._needle_step_feedrate_value
                if mode_key == self.MODE_STEP
                else self._needle_feedrate_value
            )
        if target == self.FEED_TARGET_TURNTABLE:
            return (
                self._turntable_step_feedrate_value
                if mode_key == self.MODE_STEP
                else self._turntable_feedrate_value
            )
        return self._linear_default

    def _feedrate_target_for_axis(self, axis: str) -> str:
        axis = axis.upper().strip()
        if axis == "Z":
            return self.FEED_TARGET_FOCUS
        if axis == "A":
            return self.FEED_TARGET_NEEDLES
        if axis == "B":
            return self.FEED_TARGET_TURNTABLE
        return self.FEED_TARGET_XY

    def _feedrate_max_for_target(self, target: str) -> float:
        def axis_limit(axis: str) -> float | None:
            value = self._axis_feedrate_limits.get(axis)
            if value is None:
                return None
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value) or value <= 0:
                return None
            return value

        if target == self.FEED_TARGET_XY:
            limits = [
                value
                for axis in ("X", "Y")
                if (value := axis_limit(axis)) is not None
            ]
            if limits:
                return max(limits)
        elif target == self.FEED_TARGET_FOCUS:
            limit = axis_limit("Z")
            if limit is not None:
                return limit
        elif target == self.FEED_TARGET_NEEDLES:
            limit = axis_limit("A")
            if limit is not None:
                return limit
        elif target == self.FEED_TARGET_TURNTABLE:
            limit = axis_limit("B")
            if limit is not None:
                return limit
        elif target == self.FEED_TARGET_COMMON:
            if self._common_feedrate_max is not None:
                try:
                    value = float(self._common_feedrate_max)
                except (TypeError, ValueError):
                    value = self.MIN_LINEAR_FEEDRATE
                if math.isfinite(value) and value > 0:
                    return value
        fallback_values = [
            self.MIN_LINEAR_FEEDRATE,
            float(self._linear_feedrate_value),
            float(self._linear_default),
            *(float(value) for value in self._linear_presets),
        ]
        return min(
            self.MAX_LINEAR_FEEDRATE,
            max(value for value in fallback_values if math.isfinite(value)),
        )

    def _feedrate_limit_known_for_target(self, target: str) -> bool:
        if target == self.FEED_TARGET_XY:
            return any(axis in self._axis_feedrate_limits for axis in ("X", "Y"))
        if target == self.FEED_TARGET_FOCUS:
            return "Z" in self._axis_feedrate_limits
        if target == self.FEED_TARGET_NEEDLES:
            return "A" in self._axis_feedrate_limits
        if target == self.FEED_TARGET_TURNTABLE:
            return "B" in self._axis_feedrate_limits
        if target == self.FEED_TARGET_COMMON:
            return self._common_feedrate_max is not None
        return False

    def _bounded_feedrate_setting(self, target: str, value: float) -> float:
        bounded = max(self.MIN_LINEAR_FEEDRATE, float(value))
        if self._feedrate_limit_known_for_target(target):
            bounded = min(self._feedrate_max_for_target(target), bounded)
        return bounded

    def _linear_feedrate_min_max(self) -> tuple[float, float]:
        target_max = max(
            self.MIN_LINEAR_FEEDRATE,
            float(self._feedrate_max_for_target(self._active_feedrate_target)),
        )
        if self._linear_feedrate_bounds is None:
            return (self.MIN_LINEAR_FEEDRATE, target_max)
        min_value, max_value = self._linear_feedrate_bounds
        minimum = max(self.MIN_LINEAR_FEEDRATE, float(min_value))
        maximum = min(target_max, max(self.MIN_LINEAR_FEEDRATE, float(max_value)))
        return (minimum, max(minimum, maximum))

    def _update_linear_feedrate_slider_range(self) -> None:
        minimum = int(round(self.MIN_LINEAR_FEEDRATE * self.LINEAR_FEEDRATE_SCALE))
        _min_value, max_value = self._linear_feedrate_min_max()
        maximum = int(round(max_value * self.LINEAR_FEEDRATE_SCALE))
        self.linear_feedrate_slider.blockSignals(True)
        if self.linear_feedrate_slider.minimum() != minimum or self.linear_feedrate_slider.maximum() != maximum:
            self.linear_feedrate_slider.setRange(minimum, maximum)
        self.linear_feedrate_slider.blockSignals(False)
        if hasattr(self, "linear_feedrate_spin"):
            self.linear_feedrate_spin.blockSignals(True)
            self.linear_feedrate_spin.setRange(self.MIN_LINEAR_FEEDRATE, max_value)
            self.linear_feedrate_spin.blockSignals(False)
        self._update_needle_feedrate_spin_range()
        if isinstance(self.linear_feedrate_slider, _FeedrateSlider):
            if self._linear_feedrate_bounds is None:
                self.linear_feedrate_slider.set_temporary_bounds(None, None)
            else:
                min_value, max_value = self._linear_feedrate_min_max()
                self.linear_feedrate_slider.set_temporary_bounds(
                    int(round(min_value * self.LINEAR_FEEDRATE_SCALE)),
                    int(round(max_value * self.LINEAR_FEEDRATE_SCALE)),
                )

    def _update_needle_feedrate_spin_range(self) -> None:
        if not hasattr(self, "needle_feedrate_spin"):
            return
        needle_max = max(
            self.MIN_LINEAR_FEEDRATE,
            float(self._feedrate_max_for_target(self.FEED_TARGET_NEEDLES)),
        )
        self.needle_feedrate_spin.blockSignals(True)
        self.needle_feedrate_spin.setRange(self.MIN_LINEAR_FEEDRATE, needle_max)
        self.needle_feedrate_spin.blockSignals(False)

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
        target = self._active_feedrate_target
        mode = self._control_mode
        key = self._feedrate_key(target, mode)
        previous = self._feedrate_values.get(key, self._linear_feedrate_value)
        changed = abs(bounded - previous) > 1e-9
        self._feedrate_values[key] = bounded
        self._linear_feedrate_value = bounded
        slider_value = self._slider_value_from_feedrate(bounded)
        if self.linear_feedrate_slider.value() != slider_value:
            self.linear_feedrate_slider.blockSignals(True)
            self.linear_feedrate_slider.setValue(slider_value)
            self.linear_feedrate_slider.blockSignals(False)
        if hasattr(self, "linear_feedrate_spin"):
            self.linear_feedrate_spin.blockSignals(True)
            self.linear_feedrate_spin.setValue(bounded)
            self.linear_feedrate_spin.blockSignals(False)
        if hasattr(self, "linear_feedrate_target_label"):
            self.linear_feedrate_target_label.setText(
                self.FEED_TARGET_LABELS.get(target, "Feed")
            )
        if changed:
            self._store_feedrate_value(target, mode, bounded, emit_changed=True)
        if reissue_if_active and self._active_axes:
            self._restart_active_jog_with_current_feedrate()

    def _store_feedrate_value(
        self, target: str, mode: str, value: float, *, emit_changed: bool
    ) -> None:
        is_step = mode == self.MODE_STEP
        if target == self.FEED_TARGET_XY:
            if is_step:
                self._manual_axis_feedrate_mm_min = value
                if emit_changed:
                    self.step_feedrate_changed.emit(value)
            else:
                self._linear_default = value
                if emit_changed:
                    self.linear_feedrate_changed.emit(value)
        elif target == self.FEED_TARGET_FOCUS:
            if is_step:
                self._focus_step_feedrate_value = value
                if emit_changed:
                    self.focus_step_feedrate_changed.emit(value)
            else:
                self._focus_feedrate_value = value
                if emit_changed:
                    self.focus_feedrate_changed.emit(value)
        elif target == self.FEED_TARGET_NEEDLES:
            if is_step:
                self._needle_step_feedrate_value = value
                if emit_changed:
                    self.needle_step_feedrate_changed.emit(value)
            else:
                self._needle_feedrate_value = value
                if hasattr(self, "needle_feedrate_spin"):
                    self.needle_feedrate_spin.blockSignals(True)
                    self.needle_feedrate_spin.setValue(value)
                    self.needle_feedrate_spin.blockSignals(False)
                if emit_changed:
                    self.needle_feedrate_changed.emit(value)
        elif target == self.FEED_TARGET_TURNTABLE:
            if is_step:
                self._turntable_step_feedrate_value = value
                if emit_changed:
                    self.turntable_step_feedrate_changed.emit(value)
            else:
                self._turntable_feedrate_value = value
                if emit_changed:
                    self.turntable_feedrate_changed.emit(value)
        elif target == self.FEED_TARGET_COMMON:
            self._common_feedrate_value = value
            if emit_changed:
                self.common_feedrate_changed.emit(value)

    def _set_active_feedrate_target(self, target: str) -> None:
        if target not in self.FEED_TARGET_LABELS:
            target = self.FEED_TARGET_XY
        self._active_feedrate_target = target
        if hasattr(self, "feedrate_target_combo"):
            index = self.feedrate_target_combo.findData(target)
            if index >= 0 and self.feedrate_target_combo.currentIndex() != index:
                self.feedrate_target_combo.blockSignals(True)
                self.feedrate_target_combo.setCurrentIndex(index)
                self.feedrate_target_combo.blockSignals(False)
        self._update_linear_feedrate_slider_range()
        self._set_linear_feedrate(
            self._feedrate_value_for_target(target),
            reissue_if_active=False,
        )

    def set_common_feedrate_target(
        self,
        feedrate: float,
        max_feedrate: float,
    ) -> None:
        try:
            feedrate_value = max(self.MIN_LINEAR_FEEDRATE, float(feedrate))
        except (TypeError, ValueError):
            feedrate_value = self.MIN_LINEAR_FEEDRATE
        try:
            max_value = max(self.MIN_LINEAR_FEEDRATE, float(max_feedrate))
        except (TypeError, ValueError):
            max_value = feedrate_value
        self._common_feedrate_max = max(max_value, feedrate_value)
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_COMMON, self.MODE_JOG)
        ] = min(self._common_feedrate_max, feedrate_value)
        self._ensure_common_feedrate_combo_item()
        self._set_active_feedrate_target(self.FEED_TARGET_COMMON)

    def clear_common_feedrate_target(self) -> None:
        if self._active_feedrate_target == self.FEED_TARGET_COMMON:
            self._set_active_feedrate_target(self.FEED_TARGET_XY)
        self._common_feedrate_max = None
        self._feedrate_values.pop(
            self._feedrate_key(self.FEED_TARGET_COMMON, self.MODE_JOG),
            None,
        )
        self._remove_common_feedrate_combo_item()
        self._update_linear_feedrate_slider_range()

    def _ensure_common_feedrate_combo_item(self) -> None:
        if not hasattr(self, "feedrate_target_combo"):
            return
        if self.feedrate_target_combo.findData(self.FEED_TARGET_COMMON) >= 0:
            return
        self.feedrate_target_combo.addItem(
            self.FEED_TARGET_LABELS[self.FEED_TARGET_COMMON],
            self.FEED_TARGET_COMMON,
        )

    def _remove_common_feedrate_combo_item(self) -> None:
        if not hasattr(self, "feedrate_target_combo"):
            return
        index = self.feedrate_target_combo.findData(self.FEED_TARGET_COMMON)
        if index >= 0:
            self.feedrate_target_combo.removeItem(index)

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

    def _on_linear_feedrate_spin_changed(self, value: float) -> None:
        self._set_linear_feedrate(float(value), reissue_if_active=True)

    def _on_feedrate_target_changed(self) -> None:
        self._set_active_feedrate_target(self._selected_feedrate_target())

    def _on_jog_mode_changed(self) -> None:
        mode = self._selected_control_mode()
        if mode == self._control_mode:
            return
        if self._active_axes is not None:
            self.stop_jog()
        previous_target = self._active_feedrate_target
        self._control_mode = mode
        self._update_mode_controls()
        self._set_active_feedrate_target(previous_target)
        self.control_mode_changed.emit(mode)

    def _selected_control_mode(self) -> str:
        data = self.jog_mode_combo.currentData()
        mode = str(data).strip().lower() if data is not None else ""
        if mode in {self.MODE_JOG, self.MODE_STEP}:
            return mode
        return self.MODE_JOG

    def _selected_feedrate_target(self) -> str:
        data = self.feedrate_target_combo.currentData()
        target = str(data).strip().lower() if data is not None else ""
        if target in self.FEED_TARGET_LABELS:
            return target
        return self.FEED_TARGET_XY

    def _update_mode_controls(self) -> None:
        is_step = self._control_mode == self.MODE_STEP
        self.step_distance_label.setVisible(is_step)
        self.step_distance_spin.setVisible(is_step)

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
        self._linear_default = self._bounded_feedrate_setting(
            self.FEED_TARGET_XY,
            candidate,
        )
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_XY, self.MODE_JOG)
        ] = self._linear_default
        self._update_linear_feedrate_slider_range()
        if (
            self._active_feedrate_target == self.FEED_TARGET_XY
            and self._control_mode == self.MODE_JOG
        ):
            self._set_linear_feedrate(self._linear_default, reissue_if_active=False)
        logger.info(
            "Joystick feedrate settings updated: linear=%s (default=%s)",
            self._linear_presets,
            self._linear_default,
        )

    def current_linear_feedrate(self) -> float:
        return float(self._linear_feedrate_value)

    def current_needle_feedrate(self) -> float:
        return float(self._needle_feedrate_value)

    def apply_needle_settings(self, feedrate_mm_min: float) -> None:
        self._set_needle_feedrate(feedrate_mm_min, emit_changed=False)

    def set_axis_feedrate_limits(self, limits: dict[str, float]) -> None:
        cleaned: dict[str, float] = {}
        for axis, value in limits.items():
            axis_name = str(axis).strip().upper()
            if axis_name not in self.MANUAL_JOG_AXES:
                continue
            try:
                feedrate = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(feedrate) and feedrate > 0.0:
                cleaned[axis_name] = feedrate
        self._axis_feedrate_limits = cleaned
        self._update_linear_feedrate_slider_range()
        self._set_needle_feedrate(
            self._needle_feedrate_value,
            emit_changed=False,
        )
        self._needle_step_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_NEEDLES,
            self._needle_step_feedrate_value,
        )
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_NEEDLES, self.MODE_STEP)
        ] = self._needle_step_feedrate_value
        self._set_linear_feedrate(
            self._linear_feedrate_value,
            reissue_if_active=False,
        )

    def apply_jog_settings(
        self,
        linear_distance_mm: float,
        rotary_distance_deg: float,
        motion_safety_disabled: bool = False,
        manual_axis: str = "A",
        manual_axis_distance_mm: float = DEFAULT_MANUAL_AXIS_DISTANCE_MM,
        manual_axis_mode: str = DEFAULT_MANUAL_AXIS_MODE,
        manual_axis_feedrate_mm_min: float = DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN,
        focus_feedrate_mm_min: float = DEFAULT_FOCUS_FEEDRATE_MM_MIN,
        turntable_feedrate_mm_min: float = DEFAULT_TURNTABLE_FEEDRATE_MM_MIN,
        mode: str = MODE_JOG,
        focus_step_feedrate_mm_min: float = DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN,
        needle_step_feedrate_mm_min: float = DEFAULT_NEEDLE_STEP_FEEDRATE_MM_MIN,
        turntable_step_feedrate_mm_min: float = (
            DEFAULT_TURNTABLE_STEP_FEEDRATE_MM_MIN
        ),
        **_legacy_visibility_options: object,
    ) -> None:
        """Update the jog distance used for linear axes."""

        self._applying_jog_settings = True
        was_motion_safety_disabled = self._motion_safety_disabled
        self._linear_jog_distance_mm = max(0.001, float(linear_distance_mm))
        self._rotary_jog_distance_deg = max(0.001, float(rotary_distance_deg))
        self._manual_axis_distance_mm = max(0.001, float(manual_axis_distance_mm))
        self._manual_axis_feedrate_mm_min = self._bounded_feedrate_setting(
            self.FEED_TARGET_XY,
            float(manual_axis_feedrate_mm_min),
        )
        self._focus_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_FOCUS,
            float(focus_feedrate_mm_min),
        )
        self._focus_step_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_FOCUS,
            float(focus_step_feedrate_mm_min),
        )
        self._needle_step_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_NEEDLES,
            float(needle_step_feedrate_mm_min),
        )
        self._turntable_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_TURNTABLE,
            float(turntable_feedrate_mm_min),
        )
        self._turntable_step_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_TURNTABLE,
            float(turntable_step_feedrate_mm_min),
        )
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_XY, self.MODE_STEP)
        ] = self._manual_axis_feedrate_mm_min
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_FOCUS, self.MODE_JOG)
        ] = self._focus_feedrate_value
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_FOCUS, self.MODE_STEP)
        ] = self._focus_step_feedrate_value
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_NEEDLES, self.MODE_STEP)
        ] = self._needle_step_feedrate_value
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_TURNTABLE, self.MODE_JOG)
        ] = self._turntable_feedrate_value
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_TURNTABLE, self.MODE_STEP)
        ] = self._turntable_step_feedrate_value
        self._motion_safety_disabled = bool(motion_safety_disabled)
        axis = manual_axis.strip().upper() if isinstance(manual_axis, str) else "A"
        if axis not in self.MANUAL_JOG_AXES:
            axis = "A"
        manual_mode = (
            manual_axis_mode.strip().upper()
            if isinstance(manual_axis_mode, str)
            else self.DEFAULT_MANUAL_AXIS_MODE
        )
        if manual_mode not in self.MANUAL_AXIS_MODES:
            manual_mode = self.DEFAULT_MANUAL_AXIS_MODE
        self._manual_axis_mode = manual_mode
        self.step_distance_spin.setValue(self._manual_axis_distance_mm)
        control_mode = str(mode).strip().lower()
        if control_mode not in {self.MODE_JOG, self.MODE_STEP}:
            control_mode = self.MODE_JOG
        self._control_mode = control_mode
        mode_index = self.jog_mode_combo.findData(control_mode)
        if mode_index >= 0:
            self.jog_mode_combo.blockSignals(True)
            self.jog_mode_combo.setCurrentIndex(mode_index)
            self.jog_mode_combo.blockSignals(False)
        self._update_mode_controls()
        self._set_active_feedrate_target(self.FEED_TARGET_XY)
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
            "Joystick jog settings updated: mode=%s linear_distance_mm=%s safety_disabled=%s manual_axis=%s step_distance_mm=%s manual_mode=%s xy_step_feedrate_mm_min=%s focus_jog_feedrate_mm_min=%s focus_step_feedrate_mm_min=%s needle_step_feedrate_mm_min=%s turntable_jog_feedrate_mm_min=%s turntable_step_feedrate_mm_min=%s",
            self._control_mode,
            self._linear_jog_distance_mm,
            self._motion_safety_disabled,
            axis,
            self._manual_axis_distance_mm,
            self._manual_axis_mode,
            self._manual_axis_feedrate_mm_min,
            self._focus_feedrate_value,
            self._focus_step_feedrate_value,
            self._needle_step_feedrate_value,
            self._turntable_feedrate_value,
            self._turntable_step_feedrate_value,
        )

    def _set_needle_feedrate(
        self,
        value: float,
        *,
        emit_changed: bool,
    ) -> None:
        bounded = min(
            self._feedrate_max_for_target(self.FEED_TARGET_NEEDLES),
            max(self.MIN_LINEAR_FEEDRATE, float(value)),
        )
        changed = abs(bounded - self._needle_feedrate_value) > 1e-9
        self._needle_feedrate_value = bounded
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_NEEDLES, self.MODE_JOG)
        ] = bounded
        if (
            hasattr(self, "needle_feedrate_spin")
            and self.needle_feedrate_spin.value() != bounded
        ):
            self.needle_feedrate_spin.blockSignals(True)
            self.needle_feedrate_spin.setValue(bounded)
            self.needle_feedrate_spin.blockSignals(False)
        if (
            self._active_feedrate_target == self.FEED_TARGET_NEEDLES
            and self._control_mode == self.MODE_JOG
        ):
            self._set_linear_feedrate(bounded, reissue_if_active=False)
        if changed and emit_changed:
            self.needle_feedrate_changed.emit(bounded)

    def _on_needle_feedrate_changed(self, value: float) -> None:
        self._set_active_feedrate_target(self.FEED_TARGET_NEEDLES)
        self._set_needle_feedrate(value, emit_changed=True)

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

    def _update_enabled_state(self) -> None:
        enabled = bool(self.serial_connection and self.serial_connection.is_open)
        motion_enabled = enabled and (self._axis_a_ready or self._motion_safety_disabled)
        extra_controls_enabled = enabled and (
            self._axis_a_ready or self._motion_safety_disabled
        )
        if not enabled:
            self._cancel_needle_contact_coordinate_edit()
        for widget in (
            self.linear_feedrate_slider,
            self.linear_feedrate_spin,
            self.jog_mode_combo,
            self.step_distance_spin,
            self.home_all_button,
            self.needle_feedrate_spin,
            self.needles_raise_button,
            self.needles_lift_button,
            self.needles_lower_button,
            self.unlock_button,
            self.reset_button,
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
        self._needles_up = bool(raised)
        self._needles_known = bool(known)
        if not self._needles_known:
            self._needles_zone = None
        elif self._needles_up:
            self._needles_zone = "raise"
        elif getattr(self, "_needles_zone", None) not in {"lift", "lower"}:
            self._needles_zone = "lower"
        self._apply_needle_button_styles()

    def set_needles_zone(self, zone: str) -> None:
        zone_key = str(zone).strip().lower()
        self._needles_zone = (
            zone_key if zone_key in {"raise", "lift", "lower"} else None
        )
        self._needles_known = self._needles_zone is not None
        self._needles_up = self._needles_zone == "raise"
        self._apply_needle_button_styles()

    def _apply_needle_button_styles(self) -> None:
        self.needles_raise_button.setStyleSheet(self.NEEDLES_DOWN_STYLE)
        self.needles_lift_button.setStyleSheet(self.NEEDLES_DOWN_STYLE)
        self.needles_lower_button.setStyleSheet(self.NEEDLES_DOWN_STYLE)
        if self._needles_known:
            zone = getattr(self, "_needles_zone", None)
            if zone == "raise" or (zone is None and self._needles_up):
                self.needles_raise_button.setStyleSheet(self.NEEDLES_UP_STYLE)
            elif zone == "lift":
                self.needles_lift_button.setStyleSheet(self.NEEDLES_UP_STYLE)
            else:
                self.needles_lower_button.setStyleSheet(self.NEEDLES_UP_STYLE)
        active_style = (
            self.NEEDLES_ACTIVE_DIM_STYLE
            if self._needle_blink_dimmed
            else self.NEEDLES_ACTIVE_STYLE
        )
        for button in self._needle_targets.values():
            button.setStyleSheet(active_style)

    def set_needles_action_started(self, action: str) -> None:
        if action == "raise":
            self._start_needle_animation("raise", self.needles_raise_button)
        elif action == "lift":
            self._start_needle_animation("lift", self.needles_lift_button)
        elif action == "lower":
            self._start_needle_animation("lower", self.needles_lower_button)

    def set_needles_action_finished(self, success: bool, message: str, action: str) -> None:
        if action == "raise":
            self._stop_needle_animation("raise")
        elif action == "lift":
            self._stop_needle_animation("lift")
        elif action == "lower":
            self._stop_needle_animation("lower")
        if not success:
            self._show_warning(message)

    def set_needle_contact_coordinate(
        self,
        action: str,
        a_coordinate: float | None,
    ) -> None:
        action_key = self._needle_action_for_key(action)
        if action_key is None:
            return
        if a_coordinate is None:
            self._saved_needle_contact_a_coordinates.pop(action_key, None)
            return
        try:
            value = float(a_coordinate)
        except (TypeError, ValueError):
            self._saved_needle_contact_a_coordinates.pop(action_key, None)
            return
        if math.isfinite(value):
            self._saved_needle_contact_a_coordinates[action_key] = value
        else:
            self._saved_needle_contact_a_coordinates.pop(action_key, None)

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
        if self._control_mode == self.MODE_STEP:
            self._manual_axis_step(axis, direction, mode="G91")
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
        if not had_active_axes:
            self._pending_jog_axes = None
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
        axis = axis.upper()
        if axis == "B":
            return self._rotary_jog_distance_deg
        if axis == "A":
            return self._manual_axis_distance_mm
        return self._linear_jog_distance_mm

    def _feedrate_for_axes(
        self, axes: tuple[tuple[str, int], ...]
    ) -> Optional[float]:
        if not axes:
            return None
        target = self._feedrate_target_for_axis(axes[0][0])
        self._set_active_feedrate_target(target)
        return self._linear_feedrate_value

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
        target = self._feedrate_target_for_axis(axis)
        self._set_active_feedrate_target(target)
        distance = float(direction) * self._manual_axis_distance_mm
        self.motion_axis_requested.emit(axis)
        self.manual_axis_move_requested.emit(
            axis,
            distance,
            mode or self.DEFAULT_MANUAL_AXIS_MODE,
            self._linear_feedrate_value,
        )

    def _emit_manual_axis_settings_changed(self) -> None:
        if self._applying_jog_settings:
            return
        self._manual_axis_distance_mm = max(
            0.001,
            float(self.step_distance_spin.value()),
        )
        self._manual_axis_mode = self.DEFAULT_MANUAL_AXIS_MODE
        self.manual_axis_settings_changed.emit(
            "X",
            self._manual_axis_distance_mm,
            self._manual_axis_mode,
            self._feedrate_values.get(
                self._feedrate_key(self.FEED_TARGET_XY, self.MODE_STEP),
                self._manual_axis_feedrate_mm_min,
            ),
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
        if hasattr(self, "linear_feedrate_slider"):
            self._set_active_feedrate_target(self.FEED_TARGET_NEEDLES)
        self.needles_raise_requested.emit(self._needle_feedrate_value)

    def _lift_needles(self) -> None:
        if hasattr(self, "linear_feedrate_slider"):
            self._set_active_feedrate_target(self.FEED_TARGET_NEEDLES)
        self.needles_lift_requested.emit(self._needle_feedrate_value)

    def _lower_needles(self) -> None:
        if hasattr(self, "linear_feedrate_slider"):
            self._set_active_feedrate_target(self.FEED_TARGET_NEEDLES)
        self.needles_lower_requested.emit(self._needle_feedrate_value)

    def _save_lower_needle_contact_from_current_position(self) -> None:
        self.needle_current_lower_contact_save_requested.emit()

    def _needle_action_for_key(self, action: str) -> str | None:
        action_key = str(action).strip().lower()
        if action_key in {"raise", "lift", "lower"}:
            return action_key
        return None

    def _needle_action_for_button(self, button: QPushButton) -> str:
        if button is self.needles_lower_button:
            return "lower"
        if button is self.needles_lift_button:
            return "lift"
        return "raise"

    def _show_needle_contact_coordinate_menu(self, pos) -> None:
        sender = self.sender()
        target_button = (
            sender if isinstance(sender, QPushButton) else self.needles_raise_button
        )
        action = self._needle_action_for_button(target_button)
        default_a_position = self._saved_needle_contact_a_coordinates.get(action)
        if default_a_position is None:
            default_a_position = self._current_needle_contact_a_coordinate()
        self._show_needle_contact_coordinate_editor(
            target_button,
            default_a_position,
        )
        self._show_needle_contact_context_menu(
            target_button,
            action,
            target_button.mapToGlobal(pos),
        )

    def _show_active_needle_contact_coordinate_menu(self, pos) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is None:
            return
        target_button = self._needle_contact_coordinate_button or self.needles_raise_button
        action = self._needle_action_for_button(target_button)
        self._show_needle_contact_context_menu(
            target_button,
            action,
            editor.line_edit().mapToGlobal(pos),
        )

    def _show_needle_contact_context_menu(
        self,
        target_button: QPushButton,
        action: str,
        global_pos,
    ) -> None:

        editor = self._needle_contact_coordinate_edit
        if editor is not None:
            editor.set_cancel_on_focus_out(False)
        menu = (
            editor.line_edit().createStandardContextMenu()
            if editor is not None
            else QMenu(target_button)
        )
        menu.addSeparator()
        save_action = menu.addAction("Save")
        use_current_action = menu.addAction("Use Current A Coordinate")
        menu.setDefaultAction(save_action)
        selected = menu.exec(global_pos)
        if editor is not None:
            editor.set_cancel_on_focus_out(True)
        if selected == save_action:
            self._save_needle_contact_coordinate_from_editor(action)
        elif selected == use_current_action:
            self._show_needle_contact_coordinate_editor(
                target_button,
                self._current_needle_contact_a_coordinate(),
            )
        elif editor is not None and editor.isVisible():
            editor.setFocus(Qt.PopupFocusReason)

    def _current_needle_contact_a_coordinate(self) -> float | None:
        if self.stage_controller is None:
            self._show_warning("Stage controller is not available.")
            return None
        raw_a_position = self.stage_controller.latest_a_position()
        if raw_a_position is None:
            raw_a_position = self.stage_controller.current_a_position()
        if raw_a_position is None:
            reason = self.stage_controller.last_a_position_read_failure()
            if reason:
                logger.warning("Unable to open contact coordinate editor: %s", reason)
            self._show_warning("Unable to read current A coordinate.")
            return None
        return self.stage_controller.calibrated_axis_display_value(
            "A",
            raw_a_position,
        )

    def _show_needle_contact_coordinate_editor(
        self,
        target_button: QPushButton,
        display_a_position: float | None,
    ) -> None:
        if display_a_position is None:
            return
        editor = self._needle_contact_coordinate_edit
        if editor is None:
            return
        self._needle_contact_coordinate_button = target_button
        self._position_needle_contact_coordinate_editor()
        editor.setText(f"{display_a_position:.4f}")
        editor.setModified(False)
        editor.show()
        editor.raise_()
        editor.setFocus(Qt.PopupFocusReason)
        editor.selectAll()

    def _save_needle_contact_coordinate(self, a_coordinate: float) -> None:
        target_button = self._needle_contact_coordinate_button or self.needles_raise_button
        action = self._needle_action_for_button(target_button)
        self._save_needle_contact_coordinate_value(action, a_coordinate)

    def _save_needle_contact_coordinate_from_editor(self, action: str) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is None:
            return
        value = editor.value()
        if value is None:
            QApplication.beep()
            return
        self._save_needle_contact_coordinate_value(action, value)

    def _save_needle_contact_coordinate_value(
        self,
        action: str,
        a_coordinate: float,
    ) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is not None:
            editor.hide()
        self._needle_contact_coordinate_button = None
        self.needle_contact_coordinate_save_requested.emit(action, float(a_coordinate))

    def _cancel_needle_contact_coordinate_edit(self) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is None or not editor.isVisible():
            return
        editor.hide()
        editor.clear()
        self._needle_contact_coordinate_button = None

    def _position_needle_contact_coordinate_editor(self) -> None:
        editor = self._needle_contact_coordinate_edit
        if editor is None:
            return
        target_button = (
            self._needle_contact_coordinate_button or self.needles_raise_button
        )
        button_rect = target_button.geometry()
        desired_width = max(button_rect.width() + 68, 148)
        left = button_rect.left()
        if target_button is self.needles_lower_button:
            left = button_rect.right() - desired_width + 1
        left = max(0, min(left, max(0, self.width() - desired_width)))
        editor.setGeometry(
            left,
            button_rect.top(),
            desired_width,
            button_rect.height(),
        )

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
        button.setProperty("homing", True)
        button.setChecked(True)
        self.needles_raise_button.setEnabled(False)
        self.needles_lift_button.setEnabled(False)
        self.needles_lower_button.setEnabled(False)
        self._apply_needle_button_styles()
        if not self._needle_animation_timer.isActive():
            self._needle_animation_timer.start()

    def stop_needle_animation(self) -> None:
        for key in list(self._needle_targets.keys()):
            self._stop_needle_animation(key)

    def _stop_needle_animation(self, key: str) -> None:
        button = self._needle_targets.pop(key, None)
        base_text = self._needle_text.pop(key, None)
        if button is None:
            return
        button.setProperty("homing", False)
        button.setChecked(False)
        if base_text is not None:
            button.setText(base_text)
        if not self._needle_targets:
            self._needle_animation_timer.stop()
            self._needle_blink_dimmed = False
            self._update_enabled_state()
        self._apply_needle_button_styles()

    def _advance_needle_blink(self) -> None:
        if not self._needle_targets:
            return
        self._needle_blink_dimmed = not self._needle_blink_dimmed
        self._apply_needle_button_styles()

    def _send_reset(self) -> None:
        self.reset_requested.emit()
        if self.stage_controller is None:
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

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        editor = self._needle_contact_coordinate_edit
        if editor is not None and editor.isVisible():
            self._position_needle_contact_coordinate_editor()

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
            if self._control_mode == self.MODE_STEP:
                axis, direction = mapping
                self._manual_axis_step(axis, direction, mode="G91")
                event.accept()
                return True
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
