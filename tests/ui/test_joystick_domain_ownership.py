from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
from pathlib import Path


OWNER_MODULES = (
    "probe_station_gui.views.joystick.keyboard_event_dispatch",
    "probe_station_gui.views.joystick.keyboard_input",
    "probe_station_gui.views.joystick.jog_runtime",
    "probe_station_gui.views.joystick.homing_presenter",
    "probe_station_gui.views.joystick.needle_presenter",
)

KEYBOARD_EVENT_DISPATCH_METHODS = {
    "_install_event_filter",
    "_remove_event_filter",
    "wheelEvent",
    "_apply_wheel_delta",
    "_wheel_speed_multiplier",
    "keyPressEvent",
    "keyReleaseEvent",
    "focusOutEvent",
    "resizeEvent",
    "showEvent",
    "closeEvent",
    "eventFilter",
    "_handle_shortcut_override_global_event",
    "_handle_key_press_global_event",
    "_handle_key_release_global_event",
    "_handle_wheel_global_event",
    "_log_global_key_event",
    "_accept_global_event",
    "_should_process_global_event",
    "_global_event_context",
    "_global_event_focus_is_blocked",
    "_handle_wheel_event",
    "_is_text_entry_widget",
    "_is_terminal_widget",
}

KEYBOARD_INPUT_METHODS = {
    "_is_control_mode_toggle_mapping",
    "_is_control_mode_toggle_event",
    "_handle_control_mode_toggle_press",
    "_handle_control_mode_toggle_release",
    "_handle_key_press_event",
    "_handle_key_release_event",
    "_release_key_identifier",
    "_remove_stale_key",
    "_register_pressed_mapping",
    "_schedule_pending_key_activation",
    "_activate_pending_key",
    "_cancel_pending_key_activation",
    "_clear_pending_key_activations",
    "_promote_pending_keys_if_needed",
    "_secondary_axis_activation_delay_ms",
    "_mapping_from_event",
    "_mapping_from_identifier",
    "apply_control_bindings",
    "_sync_physical_key_watchdog",
    "_drop_released_physical_keys",
    "_physical_key_is_down",
    "_event_scan_code",
}

JOG_RUNTIME_METHODS = {
    "set_serial",
    "_move_safety_check",
    "set_stage_controller",
    "set_relative_motion_projector",
    "start_jog",
    "_restart_active_jog_with_current_feedrate",
    "stop_jog",
    "cancel_jog_input",
    "_apply_axes",
    "_distance_for_axis",
    "_feedrate_for_axes",
    "_manual_axis_step",
    "_on_step_distance_spin_changed",
    "_set_step_distance",
    "_emit_manual_axis_settings_changed",
    "_compute_active_axes",
    "_schedule_active_jog_update",
    "_sync_active_jog_state",
    "_jog_sync_interval_for_axes",
    "_active_direction_for_axis",
    "_axis_direction_changes",
    "_send_reset",
    "send_command",
    "_queue_controller_command",
    "_log_serial_write_timing",
    "_invalidate_jog_stop_resend",
    "_schedule_jog_stop_resend",
}

HOMING_METHODS = {
    "set_homing_status",
    "_set_homing_button_state",
    "set_pending_homing_actions",
    "set_limit_axes",
    "_home_all",
    "_home_axis",
    "set_homing_action_started",
    "set_homing_action_finished",
    "_start_homing_animation",
    "_stop_homing_animation",
    "_advance_homing_spinner",
}

NEEDLE_METHODS = {
    "set_axis_a_ready",
    "set_needles_state",
    "set_needles_zone",
    "_apply_needle_button_styles",
    "set_needles_action_started",
    "set_needles_action_finished",
    "set_needle_contact_coordinate",
    "_raise_needles",
    "_lift_needles",
    "_lower_needles",
    "_save_lower_needle_contact_from_current_position",
    "_needle_action_for_key",
    "_needle_action_for_button",
    "_show_needle_contact_coordinate_menu",
    "_show_active_needle_contact_coordinate_menu",
    "_show_needle_contact_context_menu",
    "_current_needle_contact_a_coordinate",
    "_show_needle_contact_coordinate_editor",
    "_save_needle_contact_coordinate",
    "_save_needle_contact_coordinate_from_editor",
    "_save_needle_contact_coordinate_value",
    "_cancel_needle_contact_coordinate_edit",
    "_position_needle_contact_coordinate_editor",
    "_start_needle_animation",
    "stop_needle_animation",
    "_stop_needle_animation",
    "_advance_needle_blink",
}


def test_joystick_owner_modules_exist() -> None:
    assert all(importlib.util.find_spec(name) is not None for name in OWNER_MODULES)


def test_joystick_window_uses_exact_deep_owner_order() -> None:
    from probe_station_gui.views.joystick_window import JoystickWindow

    assert tuple(base.__name__ for base in JoystickWindow.__bases__) == (
        "JoystickKeyboardEventDispatchMixin",
        "JoystickKeyboardInputMixin",
        "JoystickJogRuntimeMixin",
        "JoystickHomingPresenterMixin",
        "JoystickNeedlePresenterMixin",
        "JoystickFeedrateMixin",
        "QWidget",
    )


def test_each_joystick_domain_has_one_canonical_owner() -> None:
    from probe_station_gui.views.joystick.keyboard_event_dispatch import (
        JoystickKeyboardEventDispatchMixin,
    )
    from probe_station_gui.views.joystick.homing_presenter import (
        JoystickHomingPresenterMixin,
    )
    from probe_station_gui.views.joystick.jog_runtime import JoystickJogRuntimeMixin
    from probe_station_gui.views.joystick.keyboard_input import (
        JoystickKeyboardInputMixin,
    )
    from probe_station_gui.views.joystick.needle_presenter import (
        JoystickNeedlePresenterMixin,
    )
    from probe_station_gui.views.joystick_window import JoystickWindow

    groups = (
        (JoystickKeyboardEventDispatchMixin, KEYBOARD_EVENT_DISPATCH_METHODS),
        (JoystickKeyboardInputMixin, KEYBOARD_INPUT_METHODS),
        (JoystickJogRuntimeMixin, JOG_RUNTIME_METHODS),
        (JoystickHomingPresenterMixin, HOMING_METHODS),
        (JoystickNeedlePresenterMixin, NEEDLE_METHODS),
    )
    for owner, names in groups:
        declared_methods = {
            name
            for name, value in owner.__dict__.items()
            if inspect.isroutine(value)
            or isinstance(value, (classmethod, staticmethod))
        }
        assert declared_methods == names
        for name in names:
            assert name not in JoystickWindow.__dict__
            assert inspect.getattr_static(
                JoystickWindow, name
            ) is inspect.getattr_static(owner, name)

    assert KEYBOARD_EVENT_DISPATCH_METHODS.isdisjoint(
        JoystickKeyboardInputMixin.__dict__
    )


def test_joystick_owner_import_graph_is_empty() -> None:
    for module_name in OWNER_MODULES:
        module = importlib.import_module(module_name)
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        dependencies = {
            imported
            for node in ast.walk(tree)
            for imported in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module is not None
                else []
            )
            if imported in OWNER_MODULES
        }
        assert dependencies == set()


def test_joystick_public_method_signatures_are_stable() -> None:
    from probe_station_gui.views.joystick_window import JoystickWindow

    expected = {
        "visible_axis_names": "(self) -> 'tuple[str, ...]'",
        "set_serial": "(self, serial_connection: 'Optional[serial.Serial]') -> 'None'",
        "set_axis_a_ready": "(self, ready: 'bool') -> 'None'",
        "set_needles_state": "(self, raised: 'bool', known: 'bool') -> 'None'",
        "set_needles_zone": "(self, zone: 'str') -> 'None'",
        "set_stage_controller": (
            "(self, stage_controller: \"Optional['StageController']\") -> 'None'"
        ),
        "set_relative_motion_projector": (
            "(self, projector: 'RelativeMotionProjector | None') -> 'None'"
        ),
        "start_jog": "(self, axis: 'str', direction: 'int') -> 'None'",
        "stop_jog": "(self) -> 'None'",
        "cancel_jog_input": "(self) -> 'None'",
        "set_homing_status": "(self, homed_axes: 'set[str]') -> 'None'",
        "set_pending_homing_actions": "(self, axes: 'object') -> 'None'",
        "set_limit_axes": "(self, axes: 'object') -> 'None'",
        "set_homing_action_started": "(self, axis_key: 'str') -> 'None'",
        "set_homing_action_finished": (
            "(self, success: 'bool', message: 'str', axis_key: 'str') -> 'None'"
        ),
        "set_needles_action_started": "(self, action: 'str') -> 'None'",
        "set_needles_action_finished": (
            "(self, success: 'bool', message: 'str', action: 'str') -> 'None'"
        ),
        "set_needle_contact_coordinate": (
            "(self, action: 'str', a_coordinate: 'float | None') -> 'None'"
        ),
        "send_command": "(self, command: 'str | bytes') -> 'bool'",
        "apply_control_bindings": (
            "(self, bindings: 'Dict[str, list[KeyBinding]]') -> 'None'"
        ),
    }
    assert {
        name: str(inspect.signature(getattr(JoystickWindow, name))) for name in expected
    } == expected
