import sys
import types
import unittest


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


_clear_probe_station_stubs()
_install_pyside6_stubs()

from probe_station_gui.views.joystick_window import JoystickWindow


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


class _FakeSpin:
    def __init__(self) -> None:
        self._value = 0.0

    def blockSignals(self, _blocked: bool) -> None:  # noqa: N802 - Qt API style
        pass

    def setValue(self, value: float) -> None:  # noqa: N802 - Qt API style
        self._value = float(value)

    def value(self) -> float:
        return float(self._value)


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


class JoystickFeedrateTest(unittest.TestCase):
    def test_manual_axis_settings_do_not_override_linear_feedrate(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget._linear_feedrate_value = 42.0
        widget._linear_feedrate_bounds = None
        widget._manual_axis_feedrate_mm_min = 600.0
        widget._motion_safety_disabled = False
        widget._show_axis_a_controls = False
        widget._show_axis_b_controls = False
        widget._manual_axis_controls_enabled = False
        widget._applying_jog_settings = False
        widget._axis_a_ready = True
        widget._pending_jog_axes = None
        widget._key_stack = []
        widget._key_press_times = {}
        widget.manual_axis_combo = types.SimpleNamespace(
            findText=lambda _value: 0,
            setCurrentIndex=lambda _index: None,
        )
        widget.manual_axis_mode_combo = types.SimpleNamespace(
            findText=lambda _value: 0,
            setCurrentIndex=lambda _index: None,
        )
        widget.manual_axis_distance_spin = types.SimpleNamespace(
            setValue=lambda _value: None
        )
        widget.manual_axis_feedrate_spin = _FakeSpin()
        widget._update_extra_axis_visibility = lambda: None
        widget._update_enabled_state = lambda: None
        widget.stop_jog = lambda: None
        widget._clear_pending_key_activations = lambda: None
        widget._sync_physical_key_watchdog = lambda: None
        widget.linear_feedrate_changed = _SignalRecorder()

        JoystickWindow.apply_jog_settings(
            widget,
            1.0,
            1.0,
            manual_axis_controls_enabled=True,
            manual_axis_feedrate_mm_min=123.4,
        )

        self.assertEqual(widget._linear_feedrate_value, 42.0)
        self.assertEqual(widget.manual_axis_feedrate_spin.value(), 42.0)
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

    def test_known_down_state_marks_lower_button_blue(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget.needles_raise_button = _FakeButton("Raise")
        widget.needles_lower_button = _FakeButton("Lower")
        widget._needle_targets = {}
        widget._needle_blink_dimmed = False
        widget._needles_up = False
        widget._needles_known = True

        JoystickWindow._apply_needle_button_styles(widget)

        self.assertIn("#f0b429", widget.needles_raise_button.styles[-1])
        self.assertIn("#1565c0", widget.needles_lower_button.styles[-1])

    def test_needle_action_blinks_yellow_without_spinner_overlay(self) -> None:
        widget = JoystickWindow.__new__(JoystickWindow)
        widget.needles_raise_button = _FakeButton("Raise")
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
        self.assertFalse(widget.needles_lower_button.enabled)
        self.assertIn("#f0b429", widget.needles_raise_button.styles[-1])

        JoystickWindow._advance_needle_blink(widget)
        self.assertIn("#d8bd78", widget.needles_raise_button.styles[-1])

        JoystickWindow._stop_needle_animation(widget, "raise")
        self.assertFalse(widget._needle_animation_timer.isActive())
        self.assertFalse(widget.needles_raise_button.checked)
        self.assertIn("#f0b429", widget.needles_raise_button.styles[-1])


if __name__ == "__main__":
    unittest.main()
