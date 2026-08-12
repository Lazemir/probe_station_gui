from __future__ import annotations

from inspect import signature
from pathlib import Path

import pytest

from probe_station_gui.route.measurement import RouteMeasurementRunner
from probe_station_gui.route.run_control_mailbox import _RouteRunControlMailbox


def _runner(csv_path: Path) -> RouteMeasurementRunner:
    return RouteMeasurementRunner(
        points=[],
        csv_path=csv_path,
        stage_controller=object(),
        lcr_controller=object(),
        needle_feedrate=None,
    )


def test_runner_composes_mailbox_without_legacy_sync_state(tmp_path: Path) -> None:
    runner = _runner(tmp_path / "route.csv")

    assert type(runner._run_control) is _RouteRunControlMailbox
    assert {
        "_stop_requested",
        "_point_interrupt_requested",
        "_pause_requested",
        "_confirmation_condition",
        "_pending_confirmation",
        "_waiting_condition",
        "_waiting",
    }.isdisjoint(vars(runner))
    assert not hasattr(RouteMeasurementRunner, "_route_point_stop_requested")
    assert not hasattr(RouteMeasurementRunner, "_consume_pause_request")
    assert not hasattr(RouteMeasurementRunner, "_set_waiting")
    assert not hasattr(RouteMeasurementRunner, "_wait_for_confirmation")


@pytest.mark.parametrize(
    ("name", "expected_signature"),
    [
        ("is_waiting", "(self) -> 'bool'"),
        ("wait_until_waiting", "(self, timeout_s: 'float') -> 'bool'"),
        ("stop", "(self) -> 'None'"),
        ("submit_confirmation", "(self, action: 'str') -> 'bool'"),
        ("submit_jump", "(self, point_number: 'int') -> 'bool'"),
        ("request_current_point_correction", "(self) -> 'None'"),
        ("current_point_correction_requested", "(self) -> 'bool'"),
        ("clear_current_point_correction_request", "(self) -> 'None'"),
        ("request_pause_after_current_point", "(self) -> 'None'"),
    ],
)
def test_runner_keeps_direct_canonical_run_control_methods(
    name: str,
    expected_signature: str,
) -> None:
    method = RouteMeasurementRunner.__dict__[name]

    assert method.__module__ == "probe_station_gui.route.measurement"
    assert str(signature(method)) == expected_signature
