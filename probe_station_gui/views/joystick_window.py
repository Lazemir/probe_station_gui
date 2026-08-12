"""Interactive joystick window for jogging the stage via serial commands."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import serial
from PySide6.QtCore import QEvent, QLocale, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.coordinates import VISIBLE_STAGE_AXES
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox,
    allow_wheel_value_change,
)
from probe_station_gui.views.joystick.feedrate_panel import (
    JoystickFeedrateMixin,
    _FeedrateSlider,
)
from probe_station_gui.views.joystick.homing_presenter import (
    JoystickHomingPresenterMixin,
)
from probe_station_gui.views.joystick.keyboard_event_dispatch import (
    JoystickKeyboardEventDispatchMixin,
)
from probe_station_gui.views.joystick.keyboard_input import (
    JoystickKeyboardInputMixin,
)
from probe_station_gui.views.joystick.jog_runtime import (
    JoystickJogRuntimeMixin,
    RelativeMotionProjector,
)
from probe_station_gui.views.joystick.needle_presenter import (
    JoystickNeedlePresenterMixin,
)
from probe_station_gui.views.joystick.widgets import (
    _NeedleContactCoordinateEdit,
    _SpinnerOverlay,
)

if TYPE_CHECKING:
    from probe_station_gui.stage.controller import StageController

logger = logging.getLogger(__name__)


class JoystickWindow(
    JoystickKeyboardEventDispatchMixin,
    JoystickKeyboardInputMixin,
    JoystickJogRuntimeMixin,
    JoystickHomingPresenterMixin,
    JoystickNeedlePresenterMixin,
    JoystickFeedrateMixin,
    QWidget,
):
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
    JOG_STOP_RESEND_DELAY_MS = 120
    MANUAL_JOG_AXES = VISIBLE_STAGE_AXES
    MANUAL_AXIS_MODES = ("G91", "G90")
    LINEAR_AXES = {"X", "Y", "Z"}
    HOMING_AXES = ("X", "Y", "Z", "A")
    LINEAR_FEEDRATE_SCALE = 10
    MIN_LINEAR_FEEDRATE = 1.0
    MAX_LINEAR_FEEDRATE = 1000.0
    MODE_JOG = "jog"
    MODE_STEP = "step"
    ACTION_TOGGLE_JOG_STEP = "__toggle_jog_step__"
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
    FEEDRATE_VALUE_STORAGE = {
        (FEED_TARGET_XY, MODE_JOG): ("_linear_default", "linear_feedrate_changed"),
        (FEED_TARGET_XY, MODE_STEP): (
            "_manual_axis_feedrate_mm_min",
            "step_feedrate_changed",
        ),
        (FEED_TARGET_FOCUS, MODE_JOG): (
            "_focus_feedrate_value",
            "focus_feedrate_changed",
        ),
        (FEED_TARGET_FOCUS, MODE_STEP): (
            "_focus_step_feedrate_value",
            "focus_step_feedrate_changed",
        ),
        (FEED_TARGET_NEEDLES, MODE_JOG): (
            "_needle_feedrate_value",
            "needle_feedrate_changed",
        ),
        (FEED_TARGET_NEEDLES, MODE_STEP): (
            "_needle_step_feedrate_value",
            "needle_step_feedrate_changed",
        ),
        (FEED_TARGET_TURNTABLE, MODE_JOG): (
            "_turntable_feedrate_value",
            "turntable_feedrate_changed",
        ),
        (FEED_TARGET_TURNTABLE, MODE_STEP): (
            "_turntable_step_feedrate_value",
            "turntable_step_feedrate_changed",
        ),
        (FEED_TARGET_COMMON, MODE_JOG): (
            "_common_feedrate_value",
            "common_feedrate_changed",
        ),
    }
    FEEDRATE_SPIN_STORAGE = {
        (FEED_TARGET_NEEDLES, MODE_JOG): "needle_feedrate_spin",
    }
    GLOBAL_EVENT_HANDLER_NAMES = {
        QEvent.ShortcutOverride: "_handle_shortcut_override_global_event",
        QEvent.KeyPress: "_handle_key_press_global_event",
        QEvent.KeyRelease: "_handle_key_release_global_event",
        QEvent.Wheel: "_handle_wheel_global_event",
    }
    GLOBAL_KEY_EVENT_TYPES = (
        QEvent.KeyPress,
        QEvent.KeyRelease,
        QEvent.ShortcutOverride,
    )
    HOMED_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #1565c0; color: #f5f5f5; }"
        "QPushButton:pressed { background: #0d47a1; }"
        "QPushButton:checked { background: #1565c0; }"
        'QPushButton[homing="true"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }'
        'QPushButton[homing="true"]:pressed { background: #e0e0e0; }'
        "QPushButton:disabled { color: #9e9e9e; }"
    )
    NOT_HOMED_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #f0b429; color: #1f1f1f; }"
        "QPushButton:pressed { background: #d89b19; }"
        "QPushButton:checked { background: #f0b429; }"
        'QPushButton[homing="true"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }'
        'QPushButton[homing="true"]:pressed { background: #e0e0e0; }'
        "QPushButton:disabled { color: #9e9e9e; }"
    )
    LIMIT_STYLE = (
        "QPushButton { padding: 2px 6px; border-radius: 4px; background: #c62828; color: #ffffff; }"
        "QPushButton:pressed { background: #8e0000; }"
        "QPushButton:checked { background: #c62828; }"
        'QPushButton[homing="true"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }'
        'QPushButton[homing="true"]:pressed { background: #e0e0e0; }'
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
        'QPushButton[homing="true"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }'
        'QPushButton[homing="true"]:pressed { background: #e0e0e0; }'
        "QPushButton:disabled { color: #9e9e9e; }"
    )
    NEEDLES_DOWN_STYLE = (
        "QPushButton { padding: 2px 6px; border: 1px solid transparent; border-radius: 4px; background: #f0b429; color: #1f1f1f; }"
        "QPushButton:pressed { background: #d89b19; }"
        "QPushButton:checked { background: #f0b429; }"
        'QPushButton[homing="true"] { background: #e6e6e6; color: #9e9e9e; border: 1px solid #cfcfcf; }'
        'QPushButton[homing="true"]:pressed { background: #e0e0e0; }'
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
        self._relative_motion_projector: RelativeMotionProjector | None = None
        self._active_jog_projection_lease: object | None = None
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
        self._focus_step_feedrate_value: float = self.DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN
        self._turntable_feedrate_value: float = self.DEFAULT_TURNTABLE_FEEDRATE_MM_MIN
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
        self.step_distance_spin = GuardedDoubleSpinBox(self)
        self.step_distance_spin.setLocale(QLocale.c())
        self.step_distance_spin.setDecimals(3)
        self.step_distance_spin.setRange(0.001, 1000.0)
        self.step_distance_spin.setSingleStep(0.001)
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
        allow_wheel_value_change(self.linear_feedrate_slider)
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
        allow_wheel_value_change(self.linear_feedrate_spin)
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
            lambda _value: self._on_step_distance_spin_changed()
        )
        self._update_mode_controls()

        homing_layout = QHBoxLayout()
        homing_layout.addWidget(QLabel("Homing:", self))
        for axis in self.HOMING_AXES:
            button = QPushButton(axis, self)
            button.setFixedSize(28, 28)
            button.setCheckable(True)
            button.setToolTip(f"Home {axis}")
            button.clicked.connect(
                lambda checked=False, axis=axis: self._home_axis(axis)
            )
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
        allow_wheel_value_change(self.needle_feedrate_spin)
        self.needles_raise_button = QPushButton("Raise", self)
        self.needles_lift_button = QPushButton("Lift", self)
        self.needles_lower_button = QPushButton("Lower", self)
        self.needles_raise_button.setCheckable(True)
        self.needles_lift_button.setCheckable(True)
        self.needles_lower_button.setCheckable(True)
        self.needles_raise_button.setToolTip("Raise needles to the top A position.")
        self.needles_lift_button.setToolTip(
            "Lift needles to the upper edge of the contact zone."
        )
        self.needles_lower_button.setToolTip(
            "Lower needles to the saved contact A0 position. Right-click to save current A as the contact position."
        )
        needle_button_width = (
            max(
                self.needles_raise_button.sizeHint().width(),
                self.needles_lift_button.sizeHint().width(),
                self.needles_lower_button.sizeHint().width(),
            )
            + 2
        )
        needle_button_height = (
            max(
                self.needles_raise_button.sizeHint().height(),
                self.needles_lift_button.sizeHint().height(),
                self.needles_lower_button.sizeHint().height(),
            )
            + 2
        )
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
        self.needle_feedrate_spin.valueChanged.connect(self._on_needle_feedrate_changed)
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

    def visible_axis_names(self) -> tuple[str, ...]:
        """Return axes exposed by ordinary jog controls."""

        return VISIBLE_STAGE_AXES

    def _update_enabled_state(self) -> None:
        enabled = bool(self.serial_connection and self.serial_connection.is_open)
        motion_enabled = enabled and (
            self._axis_a_ready or self._motion_safety_disabled
        )
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

    def _show_warning(self, message: str) -> None:
        QMessageBox.warning(self, "Joystick", message)
