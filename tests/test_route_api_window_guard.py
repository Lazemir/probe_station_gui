from probe_station_gui.route_api_window_guard import probe_route_api_requires_window


def test_probe_route_api_requires_window_for_contact_workflows() -> None:
    assert probe_route_api_requires_window("move_to_contact", {}) is True
    assert probe_route_api_requires_window("route_contact_focus", {}) is True
    assert probe_route_api_requires_window("move_to_coordinates", {"x": 1.0}) is False


def test_probe_route_api_requires_window_allows_windowless_session_controls() -> None:
    assert (
        probe_route_api_requires_window("route_session_action", {"action": "pause"})
        is False
    )
    assert (
        probe_route_api_requires_window("route_session_action", {"command": "stop"})
        is False
    )
    assert (
        probe_route_api_requires_window("route_session_action", {"action": "next"})
        is True
    )


def test_probe_route_api_requires_window_for_raw_voltage_route_options() -> None:
    assert (
        probe_route_api_requires_window("raw_voltage_sweep", {"contact_number": 3})
        is True
    )
    assert (
        probe_route_api_requires_window(
            "raw_voltage_sweep",
            {"move_to_contact": "yes"},
        )
        is True
    )
    assert (
        probe_route_api_requires_window(
            "raw_voltage_sweep",
            {"move_to_contact": "no", "lower": 0, "lift_after": False},
        )
        is False
    )
