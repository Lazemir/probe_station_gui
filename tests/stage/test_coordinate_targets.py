import math

from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetConfig,
    CoordinateTargetMoveState,
    coordinate_position_is_at_target,
    coordinate_target_common_feedrate_plan,
    plan_coordinate_target_start,
    resolve_stage_axis_target,
    stage_axis_target_limit_error,
)


AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")


def _config() -> CoordinateTargetConfig:
    return CoordinateTargetConfig(
        axis_names=AXIS_NAMES,
        min_feedrate_mm_min=1.0,
        duration_padding_s=0.25,
        min_idle_accept_s=0.2,
        target_tolerance_mm=0.01,
    )


def _state() -> CoordinateTargetMoveState:
    return CoordinateTargetMoveState(config=_config())


def test_start_plan_orders_targets_by_axis_names_and_filters_unknown_axes() -> None:
    decision = plan_coordinate_target_start(
        _config(),
        targets={"B": (4.0, 4.0), "Q": (9.0, 9.0), "X": (1.0, 1.0)},
        feedrate_mm_min=120.0,
        source_label="API",
        seed_position=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        latest_stage_position=(9.0, 9.0, 9.0, 9.0, 9.0, 9.0),
        axis_target_limit_error=lambda _axis, _target: None,
        axis_max_feedrates={"X": 100.0, "B": 60.0},
        monotonic_s=10.0,
    )

    assert decision.accepted is True
    assert decision.plan is not None
    assert decision.plan.ordered_axes == ("X", "B")
    assert decision.plan.raw_targets == {"X": 1.0, "B": 4.0}
    assert decision.plan.display_targets == {"X": 1.0, "B": 4.0}

    state = _state()
    state.apply_start_plan(decision.plan)
    assert state.display_targets == {"X": 1.0, "B": 4.0}
    state.clear_tracking()
    assert state.display_targets == {}


def test_start_plan_rejects_empty_targets_unavailable_origin_limit_errors_and_bad_target_position() -> None:
    empty = plan_coordinate_target_start(
        _config(),
        targets={},
        feedrate_mm_min=120.0,
        source_label="coordinate fields",
        seed_position=(0.0, 0.0, 0.0),
        latest_stage_position=(0.0, 0.0, 0.0),
        axis_target_limit_error=lambda _axis, _target: None,
        axis_max_feedrates={},
        monotonic_s=10.0,
    )
    assert empty.accepted is False
    assert empty.status is None

    unavailable_origin = plan_coordinate_target_start(
        _config(),
        targets={"X": (1.0, 1.0)},
        feedrate_mm_min=120.0,
        source_label="coordinate fields",
        seed_position="bad",
        latest_stage_position=(0.0, 0.0, 0.0),
        axis_target_limit_error=lambda _axis, _target: None,
        axis_max_feedrates={},
        monotonic_s=10.0,
    )
    assert unavailable_origin.accepted is False
    assert unavailable_origin.status is not None
    assert unavailable_origin.status.message == "Stage coordinates are unavailable."
    assert unavailable_origin.status.timeout_ms == 3000

    limit_error = plan_coordinate_target_start(
        _config(),
        targets={"X": (1.0, 1.25)},
        feedrate_mm_min=120.0,
        source_label="coordinate fields",
        seed_position=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        latest_stage_position=None,
        axis_target_limit_error=lambda axis, target: (
            f"{axis} target {target:+.3f} exceeds software limits (-1.000..1.000)."
        ),
        axis_max_feedrates={},
        monotonic_s=10.0,
    )
    assert limit_error.accepted is False
    assert limit_error.status is not None
    assert (
        limit_error.status.message
        == "X target +1.250 exceeds software limits (-1.000..1.000)."
    )
    assert limit_error.status.timeout_ms == 4000

    target_position_failure = plan_coordinate_target_start(
        _config(),
        targets={"B": (4.0, 4.0)},
        feedrate_mm_min=120.0,
        source_label="coordinate fields",
        seed_position=(0.0, 0.0, 0.0),
        latest_stage_position=None,
        axis_target_limit_error=lambda _axis, _target: None,
        axis_max_feedrates={},
        monotonic_s=10.0,
    )
    assert target_position_failure.accepted is False
    assert target_position_failure.status is not None
    assert target_position_failure.status.message == "Stage coordinates are unavailable."
    assert target_position_failure.status.timeout_ms == 3000


def test_start_plan_computes_tracking_maps_duration_status_and_b_axis_invalidation() -> None:
    decision = plan_coordinate_target_start(
        _config(),
        targets={"B": (4.0, 4.5), "X": (1.0, 1.25)},
        feedrate_mm_min=120.0,
        source_label="API",
        seed_position=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        latest_stage_position=(9.0, 9.0, 9.0, 9.0, 9.0, 9.0),
        axis_target_limit_error=lambda _axis, _target: None,
        axis_max_feedrates={"X": 100.0, "B": 60.0},
        monotonic_s=10.0,
    )

    assert decision.accepted is True
    assert decision.plan is not None
    assert decision.plan.active_axis == "X"
    assert decision.plan.active_axes == frozenset({"X", "B"})
    assert decision.plan.origin_position == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert decision.plan.target_position == (1.0, 0.0, 0.0, 0.0, 4.0, 0.0)
    assert decision.plan.remove_pending_axes == ("X", "B")
    assert decision.plan.invalidate_design_registration is True
    assert decision.plan.publish_position == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert decision.plan.started_at == 10.0
    assert math.isclose(
        decision.plan.duration_s,
        (math.sqrt(17.0) / (120.0 / 60.0)) + 0.25,
    )
    assert math.isclose(
        decision.plan.ends_at,
        10.0 + max(decision.plan.duration_s, 0.05),
    )
    assert decision.plan.status.message == "Moving X=1.250, B=4.500 at F120.0 from API."
    assert decision.plan.status.timeout_ms == 3000
    assert decision.plan.common_feedrate.clear_common_target is False
    assert decision.plan.common_feedrate.feedrate_mm_min == 120.0
    assert decision.plan.common_feedrate.max_feedrate_mm_min == 100.0


def test_common_feedrate_target_plan_clears_for_single_axis_and_xy_only_and_uses_limits_or_fallback_for_mixed_axes() -> None:
    single_axis = coordinate_target_common_feedrate_plan(
        ("X",),
        120.0,
        axis_max_feedrates={"X": 100.0},
        min_feedrate_mm_min=1.0,
    )
    xy_only = coordinate_target_common_feedrate_plan(
        ("X", "Y"),
        120.0,
        axis_max_feedrates={"X": 100.0, "Y": 150.0},
        min_feedrate_mm_min=1.0,
    )
    mixed = coordinate_target_common_feedrate_plan(
        ("X", "Z"),
        120.0,
        axis_max_feedrates={"X": 100.0, "Z": math.inf, "B": 60.0},
        min_feedrate_mm_min=1.0,
    )
    fallback = coordinate_target_common_feedrate_plan(
        ("A", "Z"),
        0.5,
        axis_max_feedrates={"A": 0.0, "Z": math.nan},
        min_feedrate_mm_min=1.0,
    )

    assert single_axis.clear_common_target is True
    assert xy_only.clear_common_target is True
    assert mixed.clear_common_target is False
    assert mixed.feedrate_mm_min == 120.0
    assert mixed.max_feedrate_mm_min == 100.0
    assert fallback.clear_common_target is False
    assert fallback.feedrate_mm_min == 0.5
    assert fallback.max_feedrate_mm_min == 1.0


def test_coordinate_position_target_check_honors_active_axes_and_tolerance() -> None:
    state = _state()
    state.target_position = (1.0, 2.0, 3.0, 0.0, 0.0, 0.0)
    state.active_axes = {"X", "Y"}
    state.active_axis = "X"

    assert (
        coordinate_position_is_at_target(
            state.config,
            target_position=state.target_position,
            active_axes=state.active_axes_with_fallback(),
            position=(1.005, 1.995, 99.0, 0.0, 0.0, 0.0),
        )
        is True
    )
    assert (
        coordinate_position_is_at_target(
            state.config,
            target_position=state.target_position,
            active_axes=state.active_axes_with_fallback(),
            position=(1.02, 2.0, 3.0, 0.0, 0.0, 0.0),
        )
        is False
    )


def test_feedrate_reissue_planning_handles_stale_invalid_and_accepted_reissue_paths() -> None:
    state = _state()
    state.active_axis = "X"
    state.active_axes = {"X", "Y"}
    state.origin_position = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    state.stage_position = (0.5, -0.5, 0.0, 0.0, 0.0, 0.0)
    state.target_position = (5.0, -2.0, 0.0, 0.0, 0.0, 0.0)
    state.programmed_feedrate = 120.0
    state.effective_feedrate = 120.0

    stale = state.plan_feedrate_reissue(
        controller_busy=False,
        latest_stage_state="idle",
        requested_feedrate_mm_min=180.0,
        monotonic_s=20.0,
    )
    assert stale.clear_stale_tracking is True
    assert stale.clear_stage_motion_axes is True
    assert stale.log_debug_message == "Ignoring feedrate change for stale coordinate move tracking."

    invalid = state.plan_feedrate_reissue(
        controller_busy=True,
        latest_stage_state="run",
        requested_feedrate_mm_min="bad",
        monotonic_s=20.0,
    )
    assert invalid.request is None
    assert invalid.clear_stale_tracking is False

    accepted = state.plan_feedrate_reissue(
        controller_busy=True,
        latest_stage_state="run",
        requested_feedrate_mm_min=180.0,
        monotonic_s=20.0,
    )
    assert accepted.request is not None
    assert accepted.request.raw_targets == {"X": 5.0, "Y": -2.0}
    assert accepted.request.set_reissue_cancel_pending is True

    state.apply_feedrate_reissue_success(accepted.request)

    assert state.reissue_cancel_pending is True
    assert state.origin_position == (0.5, -0.5, 0.0, 0.0, 0.0, 0.0)
    assert state.programmed_feedrate == 180.0
    assert state.effective_feedrate == 180.0
    assert state.started_at == 20.0
    assert state.ends_at == accepted.request.ends_at


def test_finish_if_idle_requires_idle_on_target_and_minimum_age() -> None:
    state = _state()
    state.active_axis = "X"
    state.active_axes = {"X", "Y"}
    state.target_position = (5.0, -2.0, 0.0, 0.0, 0.0, 0.0)
    state.started_at = 10.0

    assert state.finish_if_idle_decision(
        latest_stage_state="run",
        position=(5.0, -2.0, 0.0, 0.0, 0.0, 0.0),
        monotonic_s=11.0,
    ).finish is False
    assert state.finish_if_idle_decision(
        latest_stage_state="idle",
        position=(4.0, -2.0, 0.0, 0.0, 0.0, 0.0),
        monotonic_s=11.0,
    ).finish is False
    assert state.finish_if_idle_decision(
        latest_stage_state="idle",
        position=(5.0, -2.0, 0.0, 0.0, 0.0, 0.0),
        monotonic_s=10.1,
    ).finish is False

    finished = state.finish_if_idle_decision(
        latest_stage_state="idle",
        position=(5.0, -2.0, 0.0, 0.0, 0.0, 0.0),
        monotonic_s=10.5,
    )
    assert finished.finish is True
    assert finished.stage_position == (5.0, -2.0, 0.0, 0.0, 0.0, 0.0)


def test_axis_target_resolution_handles_g90_g91_unknown_axes_and_unavailable_values() -> None:
    raw_lookup = {
        "X": 11.0,
        "Y": 12.0,
    }

    def raw_target_from_display_value(axis: str, display_target: float) -> float | None:
        _ = display_target
        return raw_lookup.get(axis)

    raw_target, display_target = resolve_stage_axis_target(
        AXIS_NAMES,
        raw_target_from_display_value=raw_target_from_display_value,
        display_values={"X": 1.0, "Y": 2.0},
        axis_name="X",
        input_value=5.0,
        input_mode="G90",
    )
    assert (raw_target, display_target) == (11.0, 5.0)

    raw_target, display_target = resolve_stage_axis_target(
        AXIS_NAMES,
        raw_target_from_display_value=raw_target_from_display_value,
        display_values={"X": 1.0, "Y": 2.0},
        axis_name="Y",
        input_value=5.0,
        input_mode="G91",
    )
    assert (raw_target, display_target) == (12.0, 7.0)

    raw_target, display_target = resolve_stage_axis_target(
        AXIS_NAMES,
        raw_target_from_display_value=raw_target_from_display_value,
        display_values={},
        axis_name="Y",
        input_value=5.0,
        input_mode="G91",
    )
    assert (raw_target, display_target) == (None, 5.0)

    raw_target, display_target = resolve_stage_axis_target(
        AXIS_NAMES,
        raw_target_from_display_value=raw_target_from_display_value,
        display_values={"X": 1.0},
        axis_name="Q",
        input_value=5.0,
        input_mode="G90",
    )
    assert (raw_target, display_target) == (None, 5.0)


def test_software_limit_error_message_matches_main_text() -> None:
    error = stage_axis_target_limit_error(
        "X",
        1.25,
        homed_axes={"X"},
        axis_display_limits=lambda axis: (-1.0, 1.0) if axis == "X" else None,
    )
    assert error == "X target +1.250 exceeds software limits (-1.000..1.000)."
