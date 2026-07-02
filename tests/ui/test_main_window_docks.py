from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace
from typing import Any


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


serial_module = sys.modules.get("serial")
if serial_module is not None and not hasattr(serial_module, "__path__"):
    serial_tools = types.ModuleType("serial.tools")
    serial_list_ports = types.ModuleType("serial.tools.list_ports")
    serial_list_ports.comports = lambda: []
    serial_tools.list_ports = serial_list_ports
    serial_module.tools = serial_tools
    sys.modules["serial.tools"] = serial_tools
    sys.modules["serial.tools.list_ports"] = serial_list_ports

sys.modules.pop("probe_station_gui.views.joystick_window", None)
sys.modules.pop("probe_station_gui.views.serial_terminal_window", None)
sys.modules.pop("probe_station_gui.views.main_window_docks", None)

from probe_station_gui.views import main_window_docks  # noqa: E402


class _Signal:
    def __init__(self) -> None:
        self.connections: list[Any] = []

    def connect(self, slot: Any) -> None:
        self.connections.append(slot)


class _FakeStageController:
    def __init__(self) -> None:
        for name in (
            "homing_status_changed",
            "limit_axes_changed",
            "homing_action_started",
            "homing_action_finished",
            "axis_a_ready_changed",
            "needles_state_changed",
            "needles_zone_changed",
            "needles_action_started",
            "needles_action_finished",
            "stage_position_changed",
        ):
            setattr(self, name, _Signal())
        self.motion_safety_disabled: list[bool] = []
        self.requests: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.reset_sources: list[str] = []
        self.applied_axis_rates: list[dict[str, float]] = []

    def _record_request(self, name: str, *args: object, **kwargs: object) -> None:
        self.requests.append((name, tuple(args), dict(kwargs)))

    def set_motion_safety_disabled(self, value: bool) -> None:
        self.motion_safety_disabled.append(bool(value))

    def axis_max_feedrates(self) -> dict[str, float]:
        return {"X": 200.0}

    def apply_axis_max_feedrates(self, rates: object) -> None:
        self.applied_axis_rates.append(dict(rates))

    def reset_controller(self, *, source: str) -> None:
        self.reset_sources.append(source)

    def request_autofocus(self, *args: object, **kwargs: object) -> None:
        self._record_request("request_autofocus", *args, **kwargs)

    def request_needles_raise(self, *args: object, **kwargs: object) -> None:
        self._record_request("request_needles_raise", *args, **kwargs)

    def request_needles_lift(self, *args: object, **kwargs: object) -> None:
        self._record_request("request_needles_lift", *args, **kwargs)

    def request_needles_lower(self, *args: object, **kwargs: object) -> None:
        self._record_request("request_needles_lower", *args, **kwargs)

    def request_oscillation(self, *args: object, **kwargs: object) -> None:
        self._record_request("request_oscillation", *args, **kwargs)

    def request_stop_oscillation(self, *args: object, **kwargs: object) -> None:
        self._record_request("request_stop_oscillation", *args, **kwargs)


class _FakeLcrController:
    def __init__(self) -> None:
        self.status_message = _Signal()
        self.connection_changed = _Signal()
        self.reading_started = _Signal()
        self.reading_summary_updated = _Signal()
        self.reading_updated = _Signal()

    def live_polling_enabled(self) -> bool:
        return True

    def request_connect(self) -> None:
        return None

    def request_disconnect(self) -> None:
        return None


class _FakeSettingsManager:
    def feedrate_configuration(self) -> object:
        return SimpleNamespace(
            linear=SimpleNamespace(presets=[10.0, 20.0], default=10.0),
            rotary=SimpleNamespace(presets=[60.0, 120.0], default=60.0),
        )

    def jog_configuration(self) -> object:
        return SimpleNamespace(
            linear_distance_mm=0.1,
            rotary_distance_deg=1.0,
            motion_safety_disabled=True,
            manual_axis="X",
            manual_axis_distance_mm=0.05,
            manual_axis_mode="G91",
            manual_axis_feedrate_mm_min=25.0,
            focus_feedrate_mm_min=15.0,
            turntable_feedrate_mm_min=30.0,
            mode="step",
            focus_step_feedrate_mm_min=12.0,
            needles_step_feedrate_mm_min=8.0,
            turntable_step_feedrate_mm_min=20.0,
        )

    def needle_calibration_configuration(self) -> object:
        return SimpleNamespace(feedrate_mm_min=8.0)


class _FakeDialog:
    def __init__(self, _parent: object | None = None) -> None:
        self._title = ""
        self._modal = True
        self.closed = False

    def setWindowTitle(self, title: str) -> None:  # noqa: N802 - Qt naming
        self._title = str(title)

    def windowTitle(self) -> str:  # noqa: N802 - Qt naming
        return self._title

    def setModal(self, modal: bool) -> None:  # noqa: N802 - Qt naming
        self._modal = bool(modal)

    def isModal(self) -> bool:  # noqa: N802 - Qt naming
        return self._modal

    def setMinimumWidth(self, _width: int) -> None:  # noqa: N802 - Qt naming
        return None

    def resize(self, _width: int, _height: int) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeLayout:
    def __init__(self, _parent: object | None = None) -> None:
        self.items: list[object] = []

    def setContentsMargins(self, *_args: object) -> None:  # noqa: N802 - Qt naming
        return None

    def setSpacing(self, _value: int) -> None:  # noqa: N802 - Qt naming
        return None

    def addWidget(self, widget: object) -> None:  # noqa: N802 - Qt naming
        self.items.append(widget)

    def addLayout(self, layout: object) -> None:  # noqa: N802 - Qt naming
        self.items.append(layout)

    def addStretch(self, _stretch: int) -> None:  # noqa: N802 - Qt naming
        return None


class _FakeButton:
    def __init__(self, _text: str, _parent: object | None = None) -> None:
        self.clicked = _Signal()


class _FakeTabs:
    def __init__(self, _parent: object | None = None) -> None:
        self._tabs: list[tuple[object, str]] = []

    def addTab(self, widget: object, title: str) -> None:  # noqa: N802 - Qt naming
        self._tabs.append((widget, str(title)))

    def tabText(self, index: int) -> str:  # noqa: N802 - Qt naming
        return self._tabs[index][1]


class _FakeSerialConnectionPanel:
    def __init__(self, _parent: object | None = None) -> None:
        self.connected = _Signal()
        self.disconnected = _Signal()
        self.lcr_connect_requested = _Signal()
        self.lcr_disconnect_requested = _Signal()
        self.lcr_status_messages: list[object] = []

    def set_lcr_status_message(self, message: object) -> None:
        self.lcr_status_messages.append(message)


class _FakeSerialTerminalWindow:
    def __init__(self, _parent: object | None = None) -> None:
        self.manual_command_sent = _Signal()
        self.stage_controller = None
        self.serial = None

    def set_stage_controller(self, stage_controller: object) -> None:
        self.stage_controller = stage_controller

    def set_serial(self, serial_connection: object) -> None:
        self.serial = serial_connection


class _FakeResistanceMonitorPanel:
    def __init__(self, _parent: object | None = None) -> None:
        self.standby_enabled_changed = _Signal()
        self.standby_values: list[bool] = []
        self.status_messages: list[object] = []

    def set_standby_enabled(self, value: bool) -> None:
        self.standby_values.append(bool(value))

    def set_status_message(self, message: object) -> None:
        self.status_messages.append(message)


class _FakeJoystickWindow:
    def __init__(self, _parent: object | None = None) -> None:
        for name in (
            "autofocus_requested",
            "home_axis_requested",
            "home_all_requested",
            "needles_raise_requested",
            "needles_lift_requested",
            "needles_lower_requested",
            "needle_current_lower_contact_save_requested",
            "needle_contact_coordinate_save_requested",
            "zero_b_requested",
            "manual_axis_move_requested",
            "manual_axis_settings_changed",
            "control_mode_changed",
            "linear_feedrate_changed",
            "common_feedrate_changed",
            "step_feedrate_changed",
            "focus_feedrate_changed",
            "focus_step_feedrate_changed",
            "needle_feedrate_changed",
            "needle_step_feedrate_changed",
            "turntable_feedrate_changed",
            "turntable_step_feedrate_changed",
            "motion_axis_requested",
            "jog_command_changed",
            "jog_stopped",
            "reset_requested",
        ):
            setattr(self, name, _Signal())
        self.stage_controller = None
        self.serial = None

    def set_stage_controller(self, stage_controller: object) -> None:
        self.stage_controller = stage_controller

    def set_serial(self, serial_connection: object) -> None:
        self.serial = serial_connection

    def apply_feedrate_settings(self, *_args: object) -> None:
        return None

    def apply_jog_settings(self, *_args: object, **_kwargs: object) -> None:
        return None

    def apply_needle_settings(self, *_args: object) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("set_"):
            def setter(*_args: object, **_kwargs: object) -> None:
                return None

            return setter
        raise AttributeError(name)


class _FakeOscillationPanel:
    def __init__(self) -> None:
        self.start_requested = _Signal()
        self.stop_requested = _Signal()
        self.configuration_changed = _Signal()


class _FakeContactOscillationWindow:
    def __init__(self) -> None:
        self.visibility_changed = _Signal()
        self.autofocus_requested = _Signal()
        self.save_surface_position_requested = _Signal()
        self.move_to_surface_position_requested = _Signal()
        self.contact_seek_requested = _Signal()
        self.contact_seek_cancel_requested = _Signal()
        self.oscillation_panel = _FakeOscillationPanel()


class _FakeAlignmentPanel:
    def __init__(self, _parent: object | None = None) -> None:
        self.open_design_window_requested = _Signal()
        self.capture_point_requested = _Signal()
        self.reset_points_requested = _Signal()
        self.cancel_pick_requested = _Signal()
        self.clear_registration_requested = _Signal()


class _FakeAction:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self.text = str(text)


class _FakeDock:
    def __init__(self, title: str, _parent: object | None = None) -> None:
        self.title = title
        self._object_name = ""
        self._widget = None
        self.hidden = False
        self.raised = False
        self._toggle_action = _FakeAction()

    def setObjectName(self, name: str) -> None:  # noqa: N802 - Qt naming
        self._object_name = str(name)

    def objectName(self) -> str:  # noqa: N802 - Qt naming
        return self._object_name

    def setWidget(self, widget: object) -> None:  # noqa: N802 - Qt naming
        self._widget = widget

    def widget(self) -> object | None:
        return self._widget

    def setAllowedAreas(self, _areas: object) -> None:  # noqa: N802 - Qt naming
        return None

    def hide(self) -> None:
        self.hidden = True

    def raise_(self) -> None:
        self.raised = True

    def toggleViewAction(self) -> _FakeAction:  # noqa: N802 - Qt naming
        return self._toggle_action


class _DockOwner:
    _NOOP_HANDLER_NAMES = (
        "_on_manual_terminal_command",
        "_on_resistance_standby_enabled_changed",
        "_request_home_axis_from_ui",
        "_request_home_all_from_ui",
        "_save_current_needle_height",
        "_save_needle_position_from_display_a_coordinate",
        "_zero_b_axis",
        "_on_manual_axis_move_requested",
        "_save_manual_axis_jog_settings",
        "_save_jog_control_mode",
        "_on_linear_feedrate_changed",
        "_apply_coordinate_move_feedrate",
        "_on_step_feedrate_changed",
        "_on_focus_feedrate_changed",
        "_on_focus_step_feedrate_changed",
        "_on_needle_feedrate_changed",
        "_on_needle_step_feedrate_changed",
        "_on_turntable_feedrate_changed",
        "_on_turntable_step_feedrate_changed",
        "_on_manual_motion_axis",
        "_on_manual_jog_command_changed",
        "_on_manual_jog_stopped",
        "_on_homing_status_changed",
        "_on_limit_axes_changed",
        "_on_homing_action_started",
        "_on_homing_action_finished",
        "_on_needles_action_started",
        "_on_needles_action_finished",
        "_save_surface_position",
        "_move_to_surface_position",
        "_request_contact_seek",
        "_cancel_contact_seek",
        "_on_lcr_connection_changed",
        "_on_lcr_reading_started",
        "_on_lcr_reading_summary_updated",
        "_on_lcr_reading_updated",
        "_save_oscillation_configuration",
        "_request_alignment_capture",
        "_reset_alignment_capture_points",
        "_cancel_manual_alignment_pick",
        "_clear_design_registration",
    )

    def __init__(self) -> None:
        self.serial_connection = None
        self.stage_controller = _FakeStageController()
        self.lcr_controller = _FakeLcrController()
        self.settings_manager = _FakeSettingsManager()

        self.serial_connection_dialog = None
        self.serial_connection_tabs = None
        self.serial_connection_panel = None
        self.serial_terminal_panel = None
        self.resistance_panel = None
        self.resistance_dock = None
        self.joystick_panel = None
        self.joystick_dock = None
        self.contact_calibration_window = None
        self.oscillation_panel = None
        self.alignment_panel = None
        self.alignment_dock = None

        self.axis_limit_calls: list[object] = []
        self.dock_calls: list[tuple[object, ...]] = []
        self.noop_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.refresh_calls: list[str] = []
        for name in self._NOOP_HANDLER_NAMES:
            setattr(self, name, self._record_noop(name))

    def _record_noop(self, name: str) -> Any:
        def handler(*args: object, **kwargs: object) -> None:
            self.noop_calls.append((name, tuple(args), dict(kwargs)))

        return handler

    def _refresh_manual_alignment_ui(self) -> None:
        self.refresh_calls.append("manual_alignment")

    def _update_coordinate_display(self) -> None:
        self.refresh_calls.append("coordinate")

    def _refresh_design_panel(self) -> None:
        self.refresh_calls.append("design_panel")

    def _invalidate_design_registration(self, message: str) -> None:
        self.refresh_calls.append(message)

    def _toggle_design_layout_window(self, visible: bool) -> None:
        self.refresh_calls.append(f"design:{visible}")

    def addDockWidget(self, *args: object) -> None:  # noqa: N802 - Qt naming
        self.dock_calls.append(("add", *args))

    def splitDockWidget(self, *args: object) -> None:  # noqa: N802 - Qt naming
        self.dock_calls.append(("split", *args))

    def resizeDocks(self, *args: object) -> None:  # noqa: N802 - Qt naming
        self.dock_calls.append(("resize", *args))


def test_create_main_window_docks_assigns_owner_attrs_and_dock_names(monkeypatch) -> None:
    monkeypatch.setattr(main_window_docks, "QDialog", _FakeDialog)
    monkeypatch.setattr(main_window_docks, "QHBoxLayout", _FakeLayout)
    monkeypatch.setattr(main_window_docks, "QPushButton", _FakeButton)
    monkeypatch.setattr(main_window_docks, "QTabWidget", _FakeTabs)
    monkeypatch.setattr(main_window_docks, "QVBoxLayout", _FakeLayout)
    monkeypatch.setattr(
        main_window_docks, "SerialConnectionPanel", _FakeSerialConnectionPanel
    )
    monkeypatch.setattr(
        main_window_docks, "SerialTerminalWindow", _FakeSerialTerminalWindow
    )
    monkeypatch.setattr(
        main_window_docks, "ResistanceMonitorPanel", _FakeResistanceMonitorPanel
    )
    monkeypatch.setattr(main_window_docks, "JoystickWindow", _FakeJoystickWindow)
    monkeypatch.setattr(
        main_window_docks, "ContactOscillationWindow", _FakeContactOscillationWindow
    )
    monkeypatch.setattr(main_window_docks, "AlignmentPanel", _FakeAlignmentPanel)
    monkeypatch.setattr(main_window_docks, "CollapsibleDockWidget", _FakeDock)
    owner = _DockOwner()

    main_window_docks.create_main_window_docks(owner)

    assert owner.serial_connection_dialog.windowTitle() == "Connection"
    assert not owner.serial_connection_dialog.isModal()
    assert owner.serial_connection_tabs.tabText(0) == "Connection"
    assert owner.serial_connection_tabs.tabText(1) == "Terminal"
    assert owner.serial_terminal_panel.stage_controller is owner.stage_controller
    assert owner.joystick_panel.stage_controller is owner.stage_controller
    assert owner.resistance_dock.objectName() == "ResistanceDock"
    assert owner.joystick_dock.objectName() == "JoystickDock"
    assert owner.alignment_dock.objectName() == "AlignmentDock"
    assert owner.resistance_dock.widget() is owner.resistance_panel
    assert owner.joystick_dock.widget() is owner.joystick_panel
    assert owner.alignment_dock.widget() is owner.alignment_panel
    assert owner.oscillation_panel is owner.contact_calibration_window.oscillation_panel
    assert owner.stage_controller.applied_axis_rates == [{"X": 200.0}]
    assert owner.stage_controller.motion_safety_disabled == [True]
    assert owner.refresh_calls == ["manual_alignment", "coordinate", "design_panel"]


def test_needles_zone_change_persists_controller_state(monkeypatch) -> None:
    monkeypatch.setattr(main_window_docks, "QDialog", _FakeDialog)
    monkeypatch.setattr(main_window_docks, "QHBoxLayout", _FakeLayout)
    monkeypatch.setattr(main_window_docks, "QPushButton", _FakeButton)
    monkeypatch.setattr(main_window_docks, "QTabWidget", _FakeTabs)
    monkeypatch.setattr(main_window_docks, "QVBoxLayout", _FakeLayout)
    monkeypatch.setattr(
        main_window_docks, "SerialConnectionPanel", _FakeSerialConnectionPanel
    )
    monkeypatch.setattr(
        main_window_docks, "SerialTerminalWindow", _FakeSerialTerminalWindow
    )
    monkeypatch.setattr(
        main_window_docks, "ResistanceMonitorPanel", _FakeResistanceMonitorPanel
    )
    monkeypatch.setattr(main_window_docks, "JoystickWindow", _FakeJoystickWindow)
    monkeypatch.setattr(
        main_window_docks, "ContactOscillationWindow", _FakeContactOscillationWindow
    )
    monkeypatch.setattr(main_window_docks, "AlignmentPanel", _FakeAlignmentPanel)
    monkeypatch.setattr(main_window_docks, "CollapsibleDockWidget", _FakeDock)
    owner = _DockOwner()
    saved: list[object] = []
    owner.stage_controller.export_cached_controller_state = lambda: {
        "needles_zone": "lift"
    }
    owner._design_session = SimpleNamespace(export_persisted_state=lambda: None)
    owner._controller_state_persistence_suspended = False
    owner.settings_manager.save_controller_state = lambda state: saved.append(state)

    main_window_docks.create_main_window_docks(owner)
    for slot in owner.stage_controller.needles_zone_changed.connections:
        slot("lift")

    assert saved == [{"needles_zone": "lift"}]
