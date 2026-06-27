import math

from probe_station_gui.stage.manual_jog_prediction import (
    ManualJogPredictionConfig,
    ManualJogPredictionState,
)


AXIS_NAMES = ("X", "Y", "Z", "A", "B")


def _config() -> ManualJogPredictionConfig:
    return ManualJogPredictionConfig(
        axis_names=AXIS_NAMES,
        ignore_idle_after_command_s=0.3,
        reconcile_smooth_threshold_mm=0.25,
        reconcile_smooth_alpha=0.25,
        status_settle_hold_s=0.2,
        default_stop_tail_s=0.1,
        stop_tail_min_s=0.02,
        stop_tail_max_s=0.4,
        stop_tail_learn_alpha=0.5,
    )


def _state() -> ManualJogPredictionState:
    return ManualJogPredictionState(_config())


def test_command_normalization_ignores_invalid_items_and_computes_axis_velocities() -> None:
    state = _state()
    state.stage_position = (1.0, 2.0, 3.0)

    result = state.handle_command(
        (("x", 3.0), ("Y", 4.0), ("bad", 8.0), ("Z", "oops"), object()),
        feedrate=60.0,
        now=10.0,
        coordinate_move_stage_position=None,
        latest_stage_position=(9.0, 9.0, 9.0),
        current_design_stage_xy=(7.0, 8.0),
    )

    assert result.handled is True
    assert result.zero_distance is False
    assert result.motion_axes == frozenset({"X", "Y"})
    assert result.stage_source == "tracked"
    assert result.publish_position == (1.0, 2.0, 3.0)
    assert result.start_timer is True
    assert state.axis_velocities == {"X": 0.6, "Y": 0.8}
    assert state.velocity_xy == (0.6, 0.8)
    assert state.command_started_at == 10.0
    assert state.last_timestamp == 10.0


def test_zero_distance_command_clears_prediction_and_requests_stop_path() -> None:
    state = _state()
    state.axis_velocities = {"X": 1.0}
    state.stop_axis_velocities = {"Y": 2.0}
    state.velocity_xy = (1.0, 0.0)
    state.stop_prediction_until = 12.0
    state.stop_tail_position = (4.0, 5.0, 6.0)
    state.waiting_for_fresh_status = True
    state.settle_until = 9.0
    state.stop_status_timestamp = 5.0

    result = state.handle_command(
        (("X", 0.0), ("Y", 0.0)),
        feedrate=120.0,
        now=20.0,
        coordinate_move_stage_position=None,
        latest_stage_position=None,
        current_design_stage_xy=None,
    )

    assert result.handled is True
    assert result.zero_distance is True
    assert result.clear_motion_axes is True
    assert result.stop_timer is True
    assert result.schedule_status_refreshes is True
    assert state.axis_velocities == {}
    assert state.stop_axis_velocities == {}
    assert state.velocity_xy is None
    assert state.stop_prediction_until is None
    assert state.stop_tail_position is None
    assert state.waiting_for_fresh_status is False
    assert state.settle_until == 0.0
    assert state.stop_status_timestamp is None


def test_command_start_seeds_from_tracked_latest_or_current_design_sources() -> None:
    tracked = _state()
    tracked.stage_position = (1.0, 2.0, 3.0)

    tracked_result = tracked.handle_command(
        (("X", 1.0),),
        feedrate=60.0,
        now=1.0,
        coordinate_move_stage_position=None,
        latest_stage_position=(9.0, 9.0, 9.0),
        current_design_stage_xy=(8.0, 7.0),
    )

    latest = _state()
    latest_result = latest.handle_command(
        (("X", 1.0),),
        feedrate=60.0,
        now=2.0,
        coordinate_move_stage_position=None,
        latest_stage_position=(9.0, 8.0, 7.0),
        current_design_stage_xy=(5.0, 4.0),
    )

    current_design = _state()
    design_result = current_design.handle_command(
        (("X", 1.0),),
        feedrate=60.0,
        now=3.0,
        coordinate_move_stage_position=None,
        latest_stage_position=None,
        current_design_stage_xy=(5.0, 4.0),
    )

    assert tracked_result.stage_source == "tracked"
    assert tracked.stage_position == (1.0, 2.0, 3.0)
    assert latest_result.stage_source == "latest_status"
    assert latest.stage_position == (9.0, 8.0, 7.0)
    assert design_result.stage_source == "current_design"
    assert current_design.stage_position == (5.0, 4.0)


def test_prediction_predicates_follow_stop_tail_deadline_rules() -> None:
    state = _state()

    assert state.prediction_active(now=1.0) is False
    assert state.prediction_available(now=1.0) is False
    assert state.prediction_velocities(now=1.0) == {}

    state.axis_velocities = {"X": 1.0}
    assert state.prediction_active(now=1.0) is True
    assert state.prediction_available(now=1.0) is True
    assert state.prediction_velocities(now=1.0) == {"X": 1.0}

    state.axis_velocities.clear()
    state.stop_axis_velocities = {"Y": 2.0}
    state.stop_prediction_until = 10.0
    state.last_timestamp = 9.5
    assert state.prediction_active(now=11.0) is True
    assert state.prediction_available(now=11.0) is True
    assert state.prediction_velocities(now=11.0) == {"Y": 2.0}

    state.last_timestamp = 10.1
    assert state.prediction_active(now=11.0) is False
    assert state.prediction_available(now=11.0) is True
    assert state.prediction_velocities(now=11.0) == {}

    state.waiting_for_fresh_status = True
    state.stage_position = (1.0, 2.0, 3.0)
    assert state.prediction_available(now=11.0) is True


def test_stop_starts_stop_tail_prediction_only_with_velocities_and_stage_position() -> None:
    state = _state()
    state.axis_velocities = {"X": 1.0}
    state.velocity_xy = (1.0, 0.0)
    state.stage_position = (4.0, 5.0, 6.0)
    state.stage_xy = (4.0, 5.0)

    result = state.handle_stop(now=8.0, last_status_timestamp=12.0)

    assert result.start_timer is True
    assert result.stop_timer is False
    assert result.schedule_status_refreshes is True
    assert result.resume_live_poll is True
    assert state.axis_velocities == {}
    assert state.stop_axis_velocities == {"X": 1.0}
    assert state.stop_prediction_until == 8.1
    assert state.velocity_xy is None
    assert state.waiting_for_fresh_status is True
    assert state.settle_until == 8.2
    assert state.stop_status_timestamp == 12.0

    no_position = _state()
    no_position.axis_velocities = {"X": 1.0}
    no_position.velocity_xy = (1.0, 0.0)

    no_position_result = no_position.handle_stop(now=9.0, last_status_timestamp=13.0)

    assert no_position_result.start_timer is False
    assert no_position_result.stop_timer is True
    assert no_position.stop_axis_velocities == {}
    assert no_position.stop_prediction_until is None


def test_advance_integrates_active_prediction_and_updates_stage_xy() -> None:
    state = _state()
    state.stage_position = (1.0, 2.0, 3.0)
    state.stage_xy = (1.0, 2.0)
    state.axis_velocities = {"X": 1.0, "Y": -2.0}
    state.last_timestamp = 10.0

    result = state.advance(
        now=10.5,
        coordinate_move_stage_position=None,
        latest_stage_position=None,
        current_design_stage_xy=None,
    )

    assert result.publish_position == (1.5, 1.0, 3.0)
    assert result.stop_timer is False
    assert state.stage_position == (1.5, 1.0, 3.0)
    assert state.stage_xy == (1.5, 1.0)
    assert state.last_timestamp == 10.5


def test_stop_tail_advancement_clamps_to_deadline_and_records_tail_position() -> None:
    state = _state()
    state.stage_position = (0.0, 0.0, 0.0)
    state.stage_xy = (0.0, 0.0)
    state.stop_axis_velocities = {"X": 1.0}
    state.stop_prediction_until = 10.2
    state.last_timestamp = 10.1

    result = state.advance(
        now=10.5,
        coordinate_move_stage_position=None,
        latest_stage_position=None,
        current_design_stage_xy=None,
    )

    assert result.publish_position == (0.09999999999999964, 0.0, 0.0)
    assert result.stop_timer is True
    assert state.last_timestamp == 10.2
    assert state.stop_tail_position == state.stage_position


def test_stop_tail_learning_clamps_and_blends_tail_seconds() -> None:
    state = _state()
    state.stop_axis_velocities = {"X": 2.0}

    state.learn_stop_tail((0.0, 0.0), (1.0, 0.0))

    assert math.isclose(state.stop_tail_s, 0.25)


def test_idle_status_sample_filter_ignores_only_fresh_idle_samples() -> None:
    state = _state()
    state.axis_velocities = {"X": 1.0}
    state.command_started_at = 10.0

    fresh = state.ignore_idle_status_sample(
        actual_stage_xy=(1.0, 2.0),
        now=10.1,
        latest_state="idle",
        last_jog_write_timestamp=None,
    )
    stale = state.ignore_idle_status_sample(
        actual_stage_xy=(1.0, 2.0),
        now=10.5,
        latest_state="idle",
        last_jog_write_timestamp=None,
    )

    assert fresh.ignore is True
    assert math.isclose(fresh.age_s or 0.0, 0.1)
    assert stale.ignore is False


def test_smoothing_blends_only_large_run_or_jog_deltas() -> None:
    state = _state()

    assert state.smooth_actual_stage_xy((0.0, 0.0), (0.1, 0.1), latest_state="jog") == (
        0.1,
        0.1,
    )
    assert state.smooth_actual_stage_xy((0.0, 0.0), (1.0, 0.0), latest_state="idle") == (
        1.0,
        0.0,
    )
    assert state.smooth_actual_stage_xy((0.0, 0.0), (1.0, 0.0), latest_state="run") == (
        0.25,
        0.0,
    )
