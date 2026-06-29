from types import SimpleNamespace

from probe_station_gui.route.control_state import ApiRouteControlState
from probe_station_gui.route.operation_modes import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
)
from probe_station_gui.route.runtime_settings import (
    route_common_runtime_settings,
    route_measurement_runtime_settings,
)

from probe_station_gui.route.confirmation_flow import (
    route_confirmation_runtime_plan,
    route_confirmation_submission_plan,
)


class FakeMeter:
    def nplc_label(self) -> str:
        return "1"

    def measurement_type_label(self) -> str:
        return "4wire"


def _configuration(**overrides):
    data = {
        "measurement_count": 7,
        "initial_measurement_count": 3,
        "max_relative_rms": 0.02,
        "contact_quality_limits": "limits",
        "contact_seek_step_mm": 0.001,
        "contact_seek_range_mm": 0.05,
        "contact_settle_s": 0.2,
        "photo_settle_s": 0.4,
        "photo_autofocus_enabled": True,
        "csv_path": "route.csv",
        "operation_mode": ROUTE_OPERATION_MEASURE,
        "meter": FakeMeter(),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_confirmation_plan_routes_paused_api_control_action_first() -> None:
    plan = route_confirmation_submission_plan(
        "next",
        api_route_control=ApiRouteControlState(active=True, paused=True),
        runner_available=True,
        contact_move_active=False,
        waiting=True,
        pending_point_number=42,
    )

    assert plan.api_action == "resume"
    assert plan.confirmation is None
    assert plan.message == ""


def test_confirmation_plan_rewrites_waiting_actions_to_pending_jump() -> None:
    for action in ("next", "resume", "continue"):
        plan = route_confirmation_submission_plan(
            action,
            api_route_control=ApiRouteControlState(),
            runner_available=True,
            contact_move_active=False,
            waiting=True,
            pending_point_number=42,
        )

        assert plan.api_action is None
        assert plan.confirmation is not None
        assert plan.confirmation.action == "jump:42"
        assert plan.confirmation.replaced_pending_point is True
        assert plan.message == ""


def test_confirmation_plan_blocks_missing_runner_and_active_contact_move() -> None:
    missing_runner = route_confirmation_submission_plan(
        "next",
        api_route_control=ApiRouteControlState(),
        runner_available=False,
        contact_move_active=False,
        waiting=True,
        pending_point_number=None,
    )
    active_move = route_confirmation_submission_plan(
        "next",
        api_route_control=ApiRouteControlState(),
        runner_available=True,
        contact_move_active=True,
        waiting=True,
        pending_point_number=None,
    )

    assert missing_runner.message == "No route measurement is waiting."
    assert missing_runner.timeout_ms == 3000
    assert active_move.message == "Wait for route contact move to finish."
    assert active_move.timeout_ms == 3000


def test_runtime_plan_requires_restart_only_for_changed_waiting_gui_run() -> None:
    configuration = _configuration()

    assert (
        route_confirmation_runtime_plan(
            configuration,
            external_session=False,
            waiting=True,
            setup_changed=True,
        ).restart_required
        is True
    )
    assert (
        route_confirmation_runtime_plan(
            configuration,
            external_session=True,
            waiting=True,
            setup_changed=True,
        ).restart_required
        is False
    )
    assert (
        route_confirmation_runtime_plan(
            configuration,
            external_session=False,
            waiting=False,
            setup_changed=True,
        ).restart_required
        is False
    )
    assert (
        route_confirmation_runtime_plan(
            configuration,
            external_session=False,
            waiting=True,
            setup_changed=False,
        ).restart_required
        is False
    )


def test_runtime_plan_uses_external_runtime_settings_for_external_runner() -> None:
    configuration = _configuration()

    plan = route_confirmation_runtime_plan(
        configuration,
        external_session=True,
        waiting=False,
        setup_changed=False,
    )

    assert plan.runtime_settings == route_common_runtime_settings(configuration)
    assert plan.meter_configuration_required is True


def test_runtime_plan_uses_measurement_runtime_settings_for_gui_runner() -> None:
    configuration = _configuration(operation_mode=ROUTE_OPERATION_PHOTO)

    plan = route_confirmation_runtime_plan(
        configuration,
        external_session=False,
        waiting=False,
        setup_changed=False,
    )

    assert plan.runtime_settings == route_measurement_runtime_settings(configuration)
    assert plan.meter_configuration_required is False

