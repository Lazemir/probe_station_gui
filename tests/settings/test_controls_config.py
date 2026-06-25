from probe_station_gui.settings.controls_config import CONTROL_ACTIONS, KeyBinding


def test_key_binding_round_trip_preserves_native_scan_code() -> None:
    binding = KeyBinding(qt_key=68, modifiers=1, native_scan_code=32, text="d")

    restored = KeyBinding.from_dict(binding.to_dict())

    assert restored == binding


def test_control_actions_expose_stable_default_keys() -> None:
    actions = {action.key: action for action in CONTROL_ACTIONS}

    assert set(actions) == {
        "move_y_positive",
        "move_y_negative",
        "move_x_negative",
        "move_x_positive",
        "toggle_jog_step",
    }
    assert actions["toggle_jog_step"].default_text == "j"
