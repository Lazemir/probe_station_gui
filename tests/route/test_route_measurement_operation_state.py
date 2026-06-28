from __future__ import annotations

from probe_station_gui.dialogs.route_measurement_operation_state import (
    route_measurement_operation_state,
)
from probe_station_gui.route.operation_modes import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
)


def test_operation_state_measure_mode_enables_measurement_controls() -> None:
    state = route_measurement_operation_state(
        mode=ROUTE_OPERATION_MEASURE,
        running=False,
        waiting=False,
        autofocus_checked=True,
        previous_ok_only_checked=True,
    )

    assert state.photo_controls_enabled is False
    assert state.photo_autofocus_enabled is True
    assert state.photo_autofocus_range_enabled is True
    assert state.measurement_controls_enabled is True
    assert state.previous_csv_enabled is True


def test_operation_state_photo_mode_enables_photo_controls_without_meter() -> None:
    state = route_measurement_operation_state(
        mode=ROUTE_OPERATION_PHOTO,
        running=False,
        waiting=False,
        autofocus_checked=False,
        previous_ok_only_checked=True,
    )

    assert state.photo_controls_enabled is True
    assert state.photo_autofocus_enabled is True
    assert state.photo_autofocus_range_enabled is False
    assert state.measurement_controls_enabled is False
    assert state.previous_csv_enabled is False


def test_operation_state_running_waiting_allows_runtime_edits() -> None:
    running_state = route_measurement_operation_state(
        mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        running=True,
        waiting=False,
        autofocus_checked=True,
        previous_ok_only_checked=True,
    )
    waiting_state = route_measurement_operation_state(
        mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        running=True,
        waiting=True,
        autofocus_checked=True,
        previous_ok_only_checked=True,
    )

    assert running_state.photo_controls_enabled is False
    assert running_state.measurement_controls_enabled is False
    assert running_state.previous_csv_enabled is False

    assert waiting_state.photo_controls_enabled is True
    assert waiting_state.measurement_controls_enabled is True
    assert waiting_state.previous_csv_enabled is True
