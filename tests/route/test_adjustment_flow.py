from probe_station_gui.route.adjustment_flow import (
    route_contact_move_plan,
    route_shift_runner_offset_update,
    route_shift_save_plan,
    route_shift_save_status_plan,
    route_shift_stage_position_error_plan,
    route_shift_stage_xy_plan,
)
from probe_station_gui.route.control_state import ApiRouteControlState


def test_contact_move_plan_preserves_waiting_and_api_guards() -> None:
    running = route_contact_move_plan(
        contact_move_active=False,
        route_active=True,
        route_waiting=False,
        api_route_control=ApiRouteControlState(),
    )
    api_running = route_contact_move_plan(
        contact_move_active=False,
        route_active=False,
        route_waiting=False,
        api_route_control=ApiRouteControlState(active=True),
    )
    waiting = route_contact_move_plan(
        contact_move_active=False,
        route_active=True,
        route_waiting=True,
        api_route_control=ApiRouteControlState(),
    )

    assert running.message == (
        "Pause or wait for route measurement before moving to a contact."
    )
    assert api_running.message == "Pause API route control before moving to a contact."
    assert waiting.accepted is True
    assert waiting.set_resume_point is True
    assert waiting.set_adjustment_point is True


def test_contact_move_plan_allows_paused_api_control_without_runner_wait() -> None:
    plan = route_contact_move_plan(
        contact_move_active=False,
        route_active=False,
        route_waiting=False,
        api_route_control=ApiRouteControlState(active=True, paused=True),
    )

    assert plan.accepted is True
    assert plan.set_resume_point is True
    assert plan.set_adjustment_point is False


def test_shift_save_plan_uses_api_pause_as_authoritative_adjustment_state() -> None:
    running_api = route_shift_save_plan(
        runner_available=True,
        route_active=True,
        route_waiting=True,
        api_route_control=ApiRouteControlState(active=True),
        requested_point_number=7,
        dialog_current_point=None,
        current_point=None,
    )
    paused_api = route_shift_save_plan(
        runner_available=True,
        route_active=True,
        route_waiting=True,
        api_route_control=ApiRouteControlState(active=True, paused=True),
        requested_point_number=7,
        dialog_current_point=None,
        current_point=None,
    )

    assert running_api.message == "Pause API route control before saving shift."
    assert paused_api.accepted is True
    assert paused_api.runner_active is False
    assert paused_api.needs_api_context is True
    assert paused_api.point_number == 7


def test_shift_save_plan_resolves_point_and_preserves_runner_adjustment() -> None:
    runner_plan = route_shift_save_plan(
        runner_available=True,
        route_active=True,
        route_waiting=True,
        api_route_control=ApiRouteControlState(),
        requested_point_number=None,
        dialog_current_point=12,
        current_point=9,
    )
    idle_plan = route_shift_save_plan(
        runner_available=False,
        route_active=False,
        route_waiting=False,
        api_route_control=ApiRouteControlState(active=True, paused=True),
        requested_point_number=None,
        dialog_current_point=None,
        current_point=None,
    )

    assert runner_plan.accepted is True
    assert runner_plan.runner_active is True
    assert runner_plan.point_number == 12
    assert runner_plan.needs_runner_adjustment is True
    assert idle_plan.message == "Select a route point before saving shift."
    assert idle_plan.timeout_ms == 5000


def test_shift_stage_position_error_plan_blocks_busy_or_missing_latest_position() -> None:
    busy = route_shift_stage_position_error_plan(
        error_message="Stage is busy.",
        controller_busy=True,
        latest_position_available=True,
    )
    no_latest = route_shift_stage_position_error_plan(
        error_message="Unable to read stage position.",
        controller_busy=False,
        latest_position_available=False,
    )
    fallback = route_shift_stage_position_error_plan(
        error_message="Unable to read stage position.",
        controller_busy=False,
        latest_position_available=True,
    )

    assert busy.message == "Stage is busy."
    assert busy.timeout_ms == 6000
    assert busy.use_latest_position is False
    assert no_latest.message == "Unable to read stage position."
    assert no_latest.use_latest_position is False
    assert fallback.accepted is True
    assert fallback.use_latest_position is True


def test_shift_stage_xy_plan_blocks_missing_xy_position() -> None:
    missing = route_shift_stage_xy_plan(stage_xy_available=False)
    available = route_shift_stage_xy_plan(stage_xy_available=True)

    assert missing.message == "Current stage X/Y position is unavailable."
    assert missing.timeout_ms == 5000
    assert available.accepted is True


def test_shift_runner_offset_update_only_accepts_usable_xy_pairs() -> None:
    assert route_shift_runner_offset_update((0.5, -0.25)) == (0.5, -0.25)
    assert route_shift_runner_offset_update(["1.25", "2.5"]) == (1.25, 2.5)
    assert route_shift_runner_offset_update((1.0,)) is None
    assert route_shift_runner_offset_update(("bad", 2.5)) is None
    assert route_shift_runner_offset_update(None) is None


def test_shift_save_status_plan_marks_interrupt_pending_only_for_runner_save() -> None:
    runner_plan = route_shift_save_status_plan(
        runner_active=True,
        message="saved by runner",
    )
    api_plan = route_shift_save_status_plan(
        runner_active=False,
        message="saved by api",
    )

    assert runner_plan.message == "saved by runner"
    assert runner_plan.timeout_ms == 5000
    assert runner_plan.mark_interrupt_pending is True
    assert api_plan.mark_interrupt_pending is False
