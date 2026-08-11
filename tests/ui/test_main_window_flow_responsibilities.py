"""Architecture contract for serial and Coordinate System view adapters."""

from probe_station_gui.views import (
    main_window_connection_flow,
    main_window_coordinate_flow,
    main_window_design_workspace,
)


def test_view_flows_own_distinct_interfaces() -> None:
    assert main_window_connection_flow.on_serial_connected.__module__ == (
        "probe_station_gui.views.main_window_connection_flow"
    )
    assert main_window_coordinate_flow.apply_coordinate_transition.__module__ == (
        "probe_station_gui.views.main_window_coordinate_flow"
    )
    assert main_window_design_workspace.capture_design_workspace.__module__ == (
        "probe_station_gui.views.main_window_design_workspace"
    )


def test_connection_flow_is_not_a_coordinate_compatibility_facade() -> None:
    extracted_names = {
        "activate_current_design",
        "apply_coordinate_transition",
        "capture_design_workspace",
        "maybe_restore_persisted_design",
    }
    assert extracted_names.isdisjoint(vars(main_window_connection_flow))
