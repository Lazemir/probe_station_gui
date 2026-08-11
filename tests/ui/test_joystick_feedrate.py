import ast
import inspect
import sys
import textwrap
import types
import unittest
from typing import NamedTuple

_PYSIDE6_MODULES = ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets")
_ORIGINAL_PYSIDE6 = {name: sys.modules.get(name) for name in _PYSIDE6_MODULES}


def _install_pyside6_stubs() -> None:
    qtcore = types.ModuleType("PySide6.QtCore")
    qtgui = types.ModuleType("PySide6.QtGui")
    qtwidgets = types.ModuleType("PySide6.QtWidgets")

    class Signal:  # noqa: N801 - mimic Qt type name
        def __init__(self, *args, **kwargs) -> None:
            pass

        def emit(self, *args, **kwargs) -> None:
            pass

    class Qt:  # noqa: N801 - mimic Qt namespace
        Horizontal = 1
        StrongFocus = 2
        AlignCenter = 4
        AlignRight = 8
        AlignVCenter = 16
        CustomContextMenu = 32
        PopupFocusReason = 64
        OtherFocusReason = 128
        NoFocus = 0
        KeyboardModifier = int
        KeyboardModifiers = int
        Key_Return = 16777220
        Key_Enter = 16777221
        Key_Escape = 16777216
        NoPen = 0
        RoundCap = 0
        WA_TransparentForMouseEvents = 0
        WA_NoSystemBackground = 0
        WA_TranslucentBackground = 0

    class _FakeWidget:
        def __init__(self, *args, **kwargs) -> None:
            self._value = 0.0

        def blockSignals(self, *_args) -> None:  # noqa: N802 - Qt API style
            pass

        def setValue(self, value) -> None:  # noqa: N802 - Qt API style
            self._value = float(value)

        def value(self) -> float:
            return float(self._value)

    class _FakeSlider(_FakeWidget):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._minimum = 0
            self._maximum = 0

        def setRange(self, minimum, maximum) -> None:  # noqa: N802 - Qt API style
            self._minimum = int(minimum)
            self._maximum = int(maximum)

        def minimum(self) -> int:
            return self._minimum

        def maximum(self) -> int:
            return self._maximum

        def set_temporary_bounds(self, *_args) -> None:
            pass

    class _FakeLabel(_FakeWidget):
        def setText(self, text) -> None:  # noqa: N802 - Qt API style
            self.text = str(text)

    qtcore.QEvent = types.SimpleNamespace(
        KeyPress=1,
        KeyRelease=2,
        ShortcutOverride=3,
        Wheel=4,
        FocusOut=5,
    )
    qtcore.QLocale = types.SimpleNamespace(c=lambda: None)
    qtcore.QRectF = object
    qtcore.QTimer = _FakeWidget
    qtcore.Qt = Qt
    qtcore.Signal = Signal
    qtgui.QColor = object
    qtgui.QCloseEvent = object
    qtgui.QDoubleValidator = _FakeWidget
    qtgui.QPainter = object
    qtgui.QPen = object
    qtwidgets.QWidget = _FakeWidget
    qtwidgets.QAbstractSpinBox = _FakeWidget
    qtwidgets.QApplication = _FakeWidget
    qtwidgets.QComboBox = _FakeWidget
    qtwidgets.QDoubleSpinBox = _FakeWidget
    qtwidgets.QGridLayout = _FakeWidget
    qtwidgets.QHBoxLayout = _FakeWidget
    qtwidgets.QLabel = _FakeLabel
    qtwidgets.QLineEdit = _FakeWidget
    qtwidgets.QMenu = _FakeWidget
    qtwidgets.QMessageBox = _FakeWidget
    qtwidgets.QPlainTextEdit = _FakeWidget
    qtwidgets.QPushButton = _FakeWidget
    qtwidgets.QSlider = _FakeSlider
    qtwidgets.QStyle = types.SimpleNamespace(
        CC_Slider=1,
        SC_SliderGroove=2,
        SC_SliderHandle=3,
        sliderPositionFromValue=lambda *_args: 0,
    )
    qtwidgets.QStyleOptionSlider = _FakeWidget
    qtwidgets.QTextEdit = _FakeWidget
    qtwidgets.QVBoxLayout = _FakeWidget

    pyside6 = types.ModuleType("PySide6")
    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore
    sys.modules["PySide6.QtGui"] = qtgui
    sys.modules["PySide6.QtWidgets"] = qtwidgets

    if "serial" not in sys.modules:
        serial_stub = types.ModuleType("serial")
        serial_stub.Serial = object
        serial_stub.SerialException = Exception
        sys.modules["serial"] = serial_stub


def _clear_probe_station_stubs() -> None:
    package = sys.modules.get("probe_station_gui")
    if package is not None and not hasattr(package, "__path__"):
        for name in list(sys.modules):
            if name == "probe_station_gui" or name.startswith("probe_station_gui."):
                del sys.modules[name]


def _restore_pyside6_modules() -> None:
    for name in _PYSIDE6_MODULES:
        original = _ORIGINAL_PYSIDE6[name]
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


_clear_probe_station_stubs()
_install_pyside6_stubs()

from probe_station_gui.views.joystick_window import JoystickWindow

_restore_pyside6_modules()


class _SignalRecorder:
    def __init__(self) -> None:
        self.values: list[float] = []

    def emit(self, value: float) -> None:
        self.values.append(float(value))


class _ArgsSignalRecorder:
    def __init__(self) -> None:
        self.values: list[tuple] = []

    def emit(self, *values) -> None:
        self.values.append(tuple(values))


class _ProjectionResult(NamedTuple):
    accepted: bool
    raw_distances: tuple[tuple[str, float], ...]
    lease: object | None
    reason: str


class _FakeSpin:
    def __init__(self) -> None:
        self._value = 0.0
        self.range: tuple[float, float] | None = None

    def blockSignals(self, _blocked: bool) -> None:  # noqa: N802 - Qt API style
        pass

    def setRange(self, minimum: float, maximum: float) -> None:  # noqa: N802 - Qt API style
        self.range = (float(minimum), float(maximum))

    def setValue(self, value: float) -> None:  # noqa: N802 - Qt API style
        self._value = float(value)

    def value(self) -> float:
        return float(self._value)


class _FakeSlider(_FakeSpin):
    def __init__(self) -> None:
        super().__init__()
        self._minimum = 0
        self._maximum = 0
        self.temporary_bounds: tuple[int | None, int | None] | None = None

    def setRange(self, minimum: int, maximum: int) -> None:  # noqa: N802 - Qt API style
        self._minimum = int(minimum)
        self._maximum = int(maximum)

    def minimum(self) -> int:
        return self._minimum

    def maximum(self) -> int:
        return self._maximum

    def set_temporary_bounds(  # noqa: N802 - Qt API style
        self, minimum: int | None, maximum: int | None
    ) -> None:
        self.temporary_bounds = (minimum, maximum)


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API style
        self.text = str(text)


class _FakeButton:
    def __init__(self, text: str) -> None:
        self._text = text
        self.styles: list[str] = []
        self.properties: dict[str, object] = {}
        self.checked = False
        self.enabled = True

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API style
        self._text = str(text)

    def setStyleSheet(self, style: str) -> None:  # noqa: N802 - Qt API style
        self.styles.append(str(style))

    def setProperty(self, name: str, value: object) -> None:  # noqa: N802 - Qt API style
        self.properties[str(name)] = value

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt API style
        self.checked = bool(checked)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt API style
        self.enabled = bool(enabled)


class _FakeTimer:
    def __init__(self) -> None:
        self.active = False

    def isActive(self) -> bool:  # noqa: N802 - Qt API style
        return self.active

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False


class _FakeCombo:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.index = -1

    def addItem(self, label: str, data: str) -> None:  # noqa: N802 - Qt API style
        self.items.append((str(label), str(data)))
        if self.index < 0:
            self.index = 0

    def findData(self, data: str) -> int:  # noqa: N802 - Qt API style
        for index, (_label, item_data) in enumerate(self.items):
            if item_data == data:
                return index
        return -1

    def currentIndex(self) -> int:  # noqa: N802 - Qt API style
        return self.index

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt API style
        self.index = int(index)

    def blockSignals(self, _blocked: bool) -> None:  # noqa: N802 - Qt API style
        pass

    def removeItem(self, index: int) -> None:  # noqa: N802 - Qt API style
        self.items.pop(int(index))
        if self.index >= len(self.items):
            self.index = len(self.items) - 1


class _FakeSerialConnection:
    def __init__(self) -> None:
        self.is_open = True
        self.writes: list[bytes] = []
        self.flush_count = 0

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    def flush(self) -> None:
        self.flush_count += 1


def _button_constructor_labels(*attribute_names: str) -> dict[str, str]:
    source = textwrap.dedent(inspect.getsource(JoystickWindow.__init__))
    tree = ast.parse(source)
    wanted = set(attribute_names)
    labels: dict[str, str] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr in wanted
        ):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "QPushButton"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            continue
        labels[target.attr] = value.args[0].value

    return labels


class JoystickFeedrateTest(unittest.TestCase):
    @staticmethod
    def _jog_widget() -> tuple[JoystickWindow, list[object], list[str]]:
        widget = JoystickWindow.__new__(JoystickWindow)
        sent: list[object] = []
        warnings: list[str] = []
        widget._active_axes = None
        widget._active_jog_projection_lease = None
        widget._pending_jog_axes = None
        widget._key_stack = []
        widget.serial_connection = types.SimpleNamespace(is_open=True)
        widget.stage_controller = None
        widget._linear_jog_distance_mm = 25.0
        widget._rotary_jog_distance_deg = 5.0
        widget._manual_axis_distance_mm = 1.0
        widget._move_safety_check = lambda: True
        widget._feedrate_for_axes = lambda _axes: 20.0
        widget._clear_pending_key_activations = lambda: None
        widget._schedule_jog_stop_resend = lambda: None
        widget.send_command = lambda command: sent.append(command) or True
        widget._show_warning = lambda message: warnings.append(str(message))
        widget.jog_command_changed = _ArgsSignalRecorder()
        widget.jog_stopped = _ArgsSignalRecorder()
        return widget, sent, warnings

    def test_motion_button_labels_use_unicode_symbols(self) -> None:
        labels = _button_constructor_labels(
            "up_button",
            "left_button",
            "right_button",
            "down_button",
            "rotate_negative_button",
            "rotate_positive_button",
        )

        self.assertEqual(
            labels,
            {
                "up_button": "↑",
                "left_button": "←",
                "right_button": "→",
                "down_button": "↓",
                "rotate_negative_button": "↺",
                "rotate_positive_button": "↻",
            },
        )

    def test_feed_target_labels_do_not_duplicate_selected_mode(self) -> None:
        labels = [
            JoystickWindow.FEED_TARGET_LABELS[target]
            for target in JoystickWindow.FEED_TARGET_ORDER
        ]

        self.assertEqual(labels, ["XY", "Z Focus", "A Needles", "B Turntable"])
        self.assertFalse(any("Jog" in label or "Step" in label for label in labels))

    def test_common_feed_target_is_not_in_normal_menu_order(self) -> None:
        self.assertNotIn(
            JoystickWindow.FEED_TARGET_COMMON,
            JoystickWindow.FEED_TARGET_ORDER,
        )
        self.assertEqual(
            JoystickWindow.FEED_TARGET_LABELS[JoystickWindow.FEED_TARGET_COMMON],
            "Common",
        )

    def test_wheel_changes_feedrate_before_axis_limits_are_known(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget._linear_feedrate_value = JoystickWindow.MIN_LINEAR_FEEDRATE
        widget._linear_default = JoystickWindow.MIN_LINEAR_FEEDRATE
        widget._linear_presets = [1.0, 3.0, 10.0, 30.0, 100.0, 300.0]
        widget._linear_feedrate_bounds = None
        widget._axis_feedrate_limits = {}
        widget._active_feedrate_target = JoystickWindow.FEED_TARGET_XY
        widget._control_mode = JoystickWindow.MODE_JOG
        widget._feedrate_values = {
            JoystickWindow._feedrate_key(
                widget,
                JoystickWindow.FEED_TARGET_XY,
                JoystickWindow.MODE_JOG,
            ): JoystickWindow.MIN_LINEAR_FEEDRATE
        }
        widget.linear_feedrate_slider = _FakeSlider()
        widget.linear_feedrate_spin = _FakeSpin()
        widget.linear_feedrate_target_label = _FakeLabel()
        widget.needle_feedrate_spin = _FakeSpin()
        widget._needle_feedrate_value = JoystickWindow.MIN_LINEAR_FEEDRATE
        widget._active_axes = None
        widget._last_feedrate_wheel_at = 0.0
        widget.linear_feedrate_changed = _SignalRecorder()
        JoystickWindow._update_linear_feedrate_slider_range(widget)

        changed = JoystickWindow._apply_wheel_delta(widget, 120)

        self.assertTrue(changed)
        self.assertGreater(
            widget._linear_feedrate_value,
            JoystickWindow.MIN_LINEAR_FEEDRATE,
        )
        self.assertGreater(
            widget.linear_feedrate_slider.maximum(),
            widget.linear_feedrate_slider.minimum(),
        )
        self.assertEqual(
            widget.linear_feedrate_changed.values,
            [widget._linear_feedrate_value],
        )

    def test_panel_wheel_changes_feedrate_in_step_mode(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget._control_mode = JoystickWindow.MODE_STEP
        widget._manual_axis_distance_mm = 1.0
        widget._linear_default = 42.0
        widget._linear_presets = [1.0, 3.0, 10.0, 30.0, 100.0, 300.0]
        widget._linear_feedrate_bounds = None
        widget._axis_feedrate_limits = {}
        widget._active_feedrate_target = JoystickWindow.FEED_TARGET_XY
        widget._feedrate_values = {
            JoystickWindow._feedrate_key(
                widget,
                JoystickWindow.FEED_TARGET_XY,
                JoystickWindow.MODE_STEP,
            ): 42.0
        }
        widget.step_distance_spin = _FakeSpin()
        widget.step_distance_spin.setValue(1.0)
        widget._last_feedrate_wheel_at = 0.0
        widget._linear_feedrate_value = 42.0
        widget.linear_feedrate_slider = _FakeSlider()
        widget.linear_feedrate_spin = _FakeSpin()
        widget.linear_feedrate_target_label = _FakeLabel()
        widget.needle_feedrate_spin = _FakeSpin()
        widget._needle_feedrate_value = JoystickWindow.MIN_LINEAR_FEEDRATE
        widget._active_axes = None
        widget.linear_feedrate_changed = _SignalRecorder()
        widget.step_feedrate_changed = _SignalRecorder()
        JoystickWindow._update_linear_feedrate_slider_range(widget)

        changed = JoystickWindow._apply_wheel_delta(widget, 120)

        self.assertTrue(changed)
        self.assertEqual(widget._manual_axis_distance_mm, 1.0)
        self.assertEqual(widget.step_distance_spin.value(), 1.0)
        self.assertGreater(widget._linear_feedrate_value, 42.0)
        self.assertEqual(widget.linear_feedrate_spin.value(), widget._linear_feedrate_value)

    def test_step_editor_uses_guarded_spin_and_one_micron_increment(self) -> None:
        source = inspect.getsource(JoystickWindow.__init__)

        self.assertIn("self.step_distance_spin = GuardedDoubleSpinBox(self)", source)
        self.assertIn("self.step_distance_spin.setSingleStep(0.001)", source)
        self.assertNotIn("allow_wheel_value_change(self.step_distance_spin)", source)

    def test_step_request_is_emitted_even_while_controller_is_busy(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget._axis_a_ready = True
        widget._motion_safety_disabled = False
        widget.stage_controller = types.SimpleNamespace(is_busy=lambda: True)
        widget._manual_axis_distance_mm = 0.001
        widget._linear_feedrate_value = 7.0
        widget._set_active_feedrate_target = lambda _target: None
        widget.motion_axis_requested = _ArgsSignalRecorder()
        widget.manual_axis_move_requested = _ArgsSignalRecorder()

        JoystickWindow._manual_axis_step(widget, "X", 1, mode="G91")

        self.assertEqual(
            widget.manual_axis_move_requested.values,
            [("X", 0.001, "G91", 7.0)],
        )

    def test_manual_axis_settings_do_not_override_linear_feedrate(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget._linear_feedrate_value = 42.0
        widget._linear_default = 42.0
        widget._linear_feedrate_bounds = None
        widget._manual_axis_feedrate_mm_min = 600.0
        widget._focus_feedrate_value = 100.0
        widget._focus_step_feedrate_value = 50.0
        widget._needle_step_feedrate_value = 70.0
        widget._turntable_feedrate_value = 360.0
        widget._turntable_step_feedrate_value = 180.0
        widget._feedrate_values = {
            JoystickWindow._feedrate_key(
                widget, JoystickWindow.FEED_TARGET_XY, JoystickWindow.MODE_JOG
            ): 42.0,
            JoystickWindow._feedrate_key(
                widget, JoystickWindow.FEED_TARGET_XY, JoystickWindow.MODE_STEP
            ): 600.0,
            JoystickWindow._feedrate_key(
                widget, JoystickWindow.FEED_TARGET_FOCUS, JoystickWindow.MODE_JOG
            ): 100.0,
            JoystickWindow._feedrate_key(
                widget, JoystickWindow.FEED_TARGET_FOCUS, JoystickWindow.MODE_STEP
            ): 50.0,
            JoystickWindow._feedrate_key(
                widget, JoystickWindow.FEED_TARGET_NEEDLES, JoystickWindow.MODE_JOG
            ): 80.0,
            JoystickWindow._feedrate_key(
                widget, JoystickWindow.FEED_TARGET_NEEDLES, JoystickWindow.MODE_STEP
            ): 70.0,
            JoystickWindow._feedrate_key(
                widget,
                JoystickWindow.FEED_TARGET_TURNTABLE,
                JoystickWindow.MODE_JOG,
            ): 360.0,
            JoystickWindow._feedrate_key(
                widget,
                JoystickWindow.FEED_TARGET_TURNTABLE,
                JoystickWindow.MODE_STEP,
            ): 180.0,
        }
        widget._active_feedrate_target = JoystickWindow.FEED_TARGET_XY
        widget._axis_feedrate_limits = {}
        widget._control_mode = JoystickWindow.MODE_JOG
        widget._motion_safety_disabled = False
        widget._applying_jog_settings = False
        widget._axis_a_ready = True
        widget._pending_jog_axes = None
        widget._key_stack = []
        widget._key_press_times = {}
        widget.step_distance_spin = types.SimpleNamespace(
            setValue=lambda value: setattr(widget, "_step_spin_value", float(value))
        )
        widget.jog_mode_combo = types.SimpleNamespace(
            findData=lambda _value: 0,
            blockSignals=lambda _blocked: None,
            setCurrentIndex=lambda _index: None,
        )
        widget._update_mode_controls = lambda: None
        widget._set_active_feedrate_target = (
            lambda target: setattr(widget, "_active_feedrate_target", target)
        )
        widget._update_enabled_state = lambda: None
        widget.stop_jog = lambda: None
        widget._clear_pending_key_activations = lambda: None
        widget._sync_physical_key_watchdog = lambda: None
        widget.linear_feedrate_changed = _SignalRecorder()

        JoystickWindow.apply_jog_settings(
            widget,
            1.0,
            1.0,
            manual_axis_feedrate_mm_min=123.4,
            focus_feedrate_mm_min=55.0,
            focus_step_feedrate_mm_min=44.0,
            needle_step_feedrate_mm_min=33.0,
            turntable_feedrate_mm_min=222.0,
            turntable_step_feedrate_mm_min=111.0,
        )

        self.assertEqual(widget._linear_feedrate_value, 42.0)
        self.assertEqual(
            widget._feedrate_values[
                JoystickWindow._feedrate_key(
                    widget,
                    JoystickWindow.FEED_TARGET_XY,
                    JoystickWindow.MODE_JOG,
                )
            ],
            42.0,
        )
        self.assertEqual(
            widget._feedrate_values[
                JoystickWindow._feedrate_key(
                    widget,
                    JoystickWindow.FEED_TARGET_XY,
                    JoystickWindow.MODE_STEP,
                )
            ],
            123.4,
        )
        self.assertEqual(
            widget._feedrate_values[
                JoystickWindow._feedrate_key(
                    widget,
                    JoystickWindow.FEED_TARGET_FOCUS,
                    JoystickWindow.MODE_STEP,
                )
            ],
            44.0,
        )
        self.assertEqual(
            widget._feedrate_values[
                JoystickWindow._feedrate_key(
                    widget,
                    JoystickWindow.FEED_TARGET_NEEDLES,
                    JoystickWindow.MODE_STEP,
                )
            ],
            33.0,
        )
        self.assertEqual(
            widget._feedrate_values[
                JoystickWindow._feedrate_key(
                    widget,
                    JoystickWindow.FEED_TARGET_TURNTABLE,
                    JoystickWindow.MODE_STEP,
                )
            ],
            111.0,
        )
        self.assertEqual(widget.linear_feedrate_changed.values, [])

    def test_raise_click_only_emits_request(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget._needle_feedrate_value = 77.5
        widget.needles_raise_requested = _ArgsSignalRecorder()
        animation_calls: list[tuple] = []
        widget._start_needle_animation = lambda *args: animation_calls.append(args)

        JoystickWindow._raise_needles(widget)

        self.assertEqual(widget.needles_raise_requested.values, [(77.5,)])
        self.assertEqual(animation_calls, [])

    def test_lift_click_only_emits_request(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget._needle_feedrate_value = 66.5
        widget.needles_lift_requested = _ArgsSignalRecorder()

        JoystickWindow._lift_needles(widget)

        self.assertEqual(widget.needles_lift_requested.values, [(66.5,)])

    def test_lower_right_click_save_requests_current_contact_save(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget.needle_current_lower_contact_save_requested = _ArgsSignalRecorder()

        JoystickWindow._save_lower_needle_contact_from_current_position(widget)

        self.assertEqual(
            widget.needle_current_lower_contact_save_requested.values,
            [()],
        )

    def test_stop_without_active_jog_does_not_send_cancel(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget._active_axes = None
        widget._pending_jog_axes = None
        widget._key_stack = []
        widget.serial_connection = types.SimpleNamespace(is_open=True)
        sent: list[object] = []
        widget.send_command = lambda command: sent.append(command) or True
        widget.jog_stopped = _ArgsSignalRecorder()

        JoystickWindow.stop_jog(widget)

        self.assertEqual(sent, [])
        self.assertEqual(widget.jog_stopped.values, [])

    def test_coordinate_system_change_cancels_jog_and_held_key_state(self) -> None:
        widget, sent, _warnings = self._jog_widget()
        widget._key_stack = [("key", "W")]
        widget._key_press_times = {("key", "W"): 1.0}
        cleared: list[object] = []
        widget._clear_pending_key_activations = lambda: cleared.append("pending")
        widget._sync_physical_key_watchdog = lambda: cleared.append("watchdog")
        JoystickWindow._apply_axes(widget, (("X", 1),))

        JoystickWindow.cancel_jog_input(widget)

        self.assertEqual(sent, ["$J=G91 G21 X25.000 F20.0\n", b"\x85"])
        self.assertEqual(widget._key_stack, [])
        self.assertEqual(widget._key_press_times, {})
        self.assertEqual(cleared, ["pending", "watchdog"])
        self.assertIsNone(widget._active_jog_projection_lease)

    def test_initial_jog_projects_distances_and_stores_returned_lease(self) -> None:
        widget, sent, _warnings = self._jog_widget()
        lease = object()
        calls: list[tuple[tuple[tuple[str, float], ...], object | None]] = []

        def project(requested_distances, active_lease):
            calls.append((requested_distances, active_lease))
            return _ProjectionResult(True, (("Y", 2.5),), lease, "")

        JoystickWindow.set_relative_motion_projector(widget, project)

        JoystickWindow._apply_axes(widget, (("X", 1),))

        self.assertEqual(calls, [((("X", 25.0),), None)])
        self.assertEqual(sent, ["$J=G91 G21 Y2.500 F20.0\n"])
        self.assertIs(widget._active_jog_projection_lease, lease)
        self.assertEqual(widget._active_axes, (("X", 1),))
        self.assertEqual(
            widget.jog_command_changed.values,
            [((("Y", 2.5),), 20.0)],
        )

    def test_feedrate_restart_reuses_active_projection_lease(self) -> None:
        widget, _sent, _warnings = self._jog_widget()
        lease = object()
        observed_leases: list[object | None] = []

        def project(requested_distances, active_lease):
            observed_leases.append(active_lease)
            return _ProjectionResult(True, requested_distances, lease, "")

        JoystickWindow.set_relative_motion_projector(widget, project)
        JoystickWindow._apply_axes(widget, (("X", 1),))

        JoystickWindow._restart_active_jog_with_current_feedrate(widget)

        self.assertEqual(observed_leases, [None, lease])
        self.assertIs(widget._active_jog_projection_lease, lease)

    def test_new_jog_after_stop_starts_without_previous_projection_lease(self) -> None:
        widget, _sent, _warnings = self._jog_widget()
        lease = object()
        observed_leases: list[object | None] = []

        def project(requested_distances, active_lease):
            observed_leases.append(active_lease)
            return _ProjectionResult(True, requested_distances, lease, "")

        JoystickWindow.set_relative_motion_projector(widget, project)
        JoystickWindow._apply_axes(widget, (("X", 1),))
        JoystickWindow.stop_jog(widget)

        JoystickWindow._apply_axes(widget, (("X", 1),))

        self.assertEqual(observed_leases, [None, None])

    def test_held_key_axis_chord_restart_reuses_active_projection_lease(self) -> None:
        widget, _sent, _warnings = self._jog_widget()
        lease = object()
        calls: list[tuple[tuple[tuple[str, float], ...], object | None]] = []

        def project(requested_distances, active_lease):
            calls.append((requested_distances, active_lease))
            return _ProjectionResult(True, requested_distances, lease, "")

        JoystickWindow.set_relative_motion_projector(widget, project)
        JoystickWindow._apply_axes(widget, (("X", 1),))

        JoystickWindow._apply_axes(widget, (("X", 1), ("Y", 1)))

        self.assertEqual(
            calls,
            [
                ((("X", 25.0),), None),
                ((("X", 25.0), ("Y", 25.0)), lease),
            ],
        )
        self.assertIs(widget._active_jog_projection_lease, lease)

    def test_projection_rejection_does_not_send_jog_and_surfaces_reason(self) -> None:
        widget, sent, warnings = self._jog_widget()

        def reject(_requested_distances, active_lease):
            self.assertIsNone(active_lease)
            return _ProjectionResult(
                False,
                (),
                object(),
                "Coordinate system changed.",
            )

        JoystickWindow.set_relative_motion_projector(widget, reject)

        JoystickWindow._apply_axes(widget, (("X", 1),))

        self.assertEqual(sent, [])
        self.assertEqual(warnings, ["Coordinate system changed."])
        self.assertEqual(widget.jog_command_changed.values, [((), 20.0)])
        self.assertIsNone(widget._active_jog_projection_lease)
        self.assertIsNone(widget._active_axes)

    def test_projection_rejection_during_restart_clears_active_lease(self) -> None:
        widget, sent, warnings = self._jog_widget()
        lease = object()

        def accept(requested_distances, _active_lease):
            return _ProjectionResult(True, requested_distances, lease, "")

        JoystickWindow.set_relative_motion_projector(widget, accept)
        JoystickWindow._apply_axes(widget, (("X", 1),))

        def reject(_requested_distances, active_lease):
            self.assertIs(active_lease, lease)
            return _ProjectionResult(False, (), lease, "Coordinate system changed.")

        widget._relative_motion_projector = reject
        JoystickWindow._restart_active_jog_with_current_feedrate(widget)

        self.assertEqual(
            [command for command in sent if isinstance(command, str)],
            ["$J=G91 G21 X25.000 F20.0\n"],
        )
        self.assertEqual(warnings, ["Coordinate system changed."])
        self.assertEqual(widget.jog_command_changed.values[-1], ((), 20.0))
        self.assertIsNone(widget._active_jog_projection_lease)
        self.assertIsNone(widget._active_axes)

    def test_jog_without_projector_preserves_standalone_distances(self) -> None:
        widget, sent, _warnings = self._jog_widget()

        JoystickWindow.set_relative_motion_projector(widget, None)

        JoystickWindow._apply_axes(widget, (("X", -1),))

        self.assertEqual(sent, ["$J=G91 G21 X-25.000 F20.0\n"])
        self.assertEqual(widget._active_axes, (("X", -1),))

    def test_known_down_state_marks_lower_button_blue(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget.needles_raise_button = _FakeButton("Raise")
        widget.needles_lift_button = _FakeButton("Lift")
        widget.needles_lower_button = _FakeButton("Lower")
        widget._needle_targets = {}
        widget._needle_blink_dimmed = False
        widget._needles_up = False
        widget._needles_known = True

        JoystickWindow._apply_needle_button_styles(widget)

        self.assertIn("#f0b429", widget.needles_raise_button.styles[-1])
        self.assertIn("#f0b429", widget.needles_lift_button.styles[-1])
        self.assertIn("#1565c0", widget.needles_lower_button.styles[-1])

    def test_lift_state_marks_lift_button_blue(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget.needles_raise_button = _FakeButton("Raise")
        widget.needles_lift_button = _FakeButton("Lift")
        widget.needles_lower_button = _FakeButton("Lower")
        widget._needle_targets = {}
        widget._needle_blink_dimmed = False
        widget._needles_up = False
        widget._needles_known = False

        JoystickWindow.set_needles_zone(widget, "lift")

        self.assertIn("#f0b429", widget.needles_raise_button.styles[-1])
        self.assertIn("#1565c0", widget.needles_lift_button.styles[-1])
        self.assertIn("#f0b429", widget.needles_lower_button.styles[-1])

    def test_needle_action_blinks_yellow_without_spinner_overlay(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget.needles_raise_button = _FakeButton("Raise")
        widget.needles_lift_button = _FakeButton("Lift")
        widget.needles_lower_button = _FakeButton("Lower")
        widget._needle_targets = {}
        widget._needle_text = {}
        widget._needle_blink_dimmed = False
        widget._needles_up = False
        widget._needles_known = True
        widget._needle_animation_timer = _FakeTimer()
        widget._update_enabled_state = lambda: None

        JoystickWindow._start_needle_animation(
            widget,
            "raise",
            widget.needles_raise_button,
        )
        self.assertTrue(widget._needle_animation_timer.isActive())
        self.assertFalse(widget.needles_raise_button.enabled)
        self.assertFalse(widget.needles_lift_button.enabled)
        self.assertFalse(widget.needles_lower_button.enabled)
        self.assertIn("#f0b429", widget.needles_raise_button.styles[-1])

        JoystickWindow._advance_needle_blink(widget)
        self.assertIn("#d8bd78", widget.needles_raise_button.styles[-1])

        JoystickWindow._stop_needle_animation(widget, "raise")
        self.assertFalse(widget._needle_animation_timer.isActive())
        self.assertFalse(widget.needles_raise_button.checked)
        self.assertIn("#f0b429", widget.needles_raise_button.styles[-1])

    def test_queue_controller_command_delegates_to_stage_controller_channel(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        observed: list[tuple[object, str]] = []
        widget.stage_controller = types.SimpleNamespace(
            queue_outbound_command=lambda command, *, source="unknown": (
                observed.append((command, source)) or True
            )
        )
        widget._show_warning = lambda _message: None

        handled = JoystickWindow._queue_controller_command(widget, "$J=G91 G21 X1.000 F10")

        self.assertTrue(handled)
        self.assertEqual(
            observed,
            [("$J=G91 G21 X1.000 F10", "joystick_reset_button")],
        )

    def test_send_command_preserves_fallback_for_unsupported_bytes(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget.serial_connection = _FakeSerialConnection()
        widget.stage_controller = types.SimpleNamespace(
            queue_outbound_command=lambda _command, *, source="unknown": None
        )
        widget._show_warning = lambda _message: None
        widget.set_serial = lambda _serial: None

        sent = JoystickWindow.send_command(widget, b"\x99")

        self.assertTrue(sent)
        self.assertEqual(widget.serial_connection.writes, [b"\x99"])
        self.assertEqual(widget.serial_connection.flush_count, 1)


if __name__ == "__main__":
    unittest.main()
