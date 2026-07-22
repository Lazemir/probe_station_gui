import math
from types import SimpleNamespace

from probe_station_gui.stage.position_presenter import (
    BAxisRegistrationPlan,
    StageReconcilePlan,
    stage_position_signal_plan,
    predicted_stage_position_plan,
    b_axis_registration_plan,
    stage_position_display_plan,
    stage_position_status_plan,
    stage_reconcile_plan,
)


AXIS_NAMES = ("X", "Y", "Z", "A", "B")


def _display_axis_value(axis: str, raw_value: float) -> float:
    return float(raw_value) + (0.5 if axis == "X" else 1.0)


def _stage_xy_from_position(position: object | None) -> tuple[float, float] | None:
    if not isinstance(position, tuple) or len(position) < 2:
        return None
    return (float(position[0]), float(position[1]))


def _position_with_stage_xy(
    stage_xy: tuple[float, float],
    *,
    base_position: tuple[float, ...],
) -> tuple[float, ...]:
    return (float(stage_xy[0]), float(stage_xy[1]), *tuple(base_position[2:]))


def test_invalid_position_display_plan_resets_caches_and_disables_fields() -> None:
    plan = stage_position_display_plan(
        None,
        axis_names=AXIS_NAMES,
        homed_axes={"X", "Y"},
        limit_axes={"Z"},
        pending_targets={"X": (1.0, 2.0)},
        display_axis_value=_display_axis_value,
        feedrate_mm_min=120.0,
    )

    assert plan.valid is False
    assert plan.reset_all is True
    assert plan.fields_available is False
    assert plan.axis_updates == ()
    assert plan.missing_axes == ()
    assert plan.homed_axes == frozenset()


def test_valid_display_plan_uses_homed_unhomed_and_limit_styles() -> None:
    plan = stage_position_display_plan(
        (1.0, 2.0, 3.0),
        axis_names=AXIS_NAMES,
        homed_axes={"X", "Z"},
        limit_axes={"Z"},
        pending_targets={},
        display_axis_value=_display_axis_value,
        feedrate_mm_min=240.0,
    )

    updates = {item.axis: item for item in plan.axis_updates}

    assert updates["X"].base_background == "#1565c0"
    assert updates["X"].base_foreground == "#f5f5f5"
    assert updates["Y"].base_background == "#f0b429"
    assert updates["Y"].base_foreground == "#1f1f1f"
    assert updates["Z"].base_background == "#c62828"
    assert updates["Z"].base_foreground == "#ffffff"
    assert (
        updates["X"].tooltip
        == "X coordinate. Enter targets and press Apply. Move feedrate: 240.0 mm/min."
    )
    assert plan.missing_axes == ("A", "B")
    assert plan.fields_available is True


def test_display_plan_assigns_confidence_roles_only_to_enabled_available_axes() -> None:
    plan = stage_position_display_plan(
        (1.0, 2.0, 3.0, 4.0),
        axis_names=("X", "Y", "Z", "A"),
        homed_axes={"X", "Y", "Z", "A"},
        limit_axes={"A"},
        pending_targets={"X": (8.0, 8.0)},
        display_axis_value=_display_axis_value,
        feedrate_mm_min=120.0,
        precision_enabled_axes={"X", "Y", "A"},
        coordinate_confidence={
            "X": SimpleNamespace(exact=True),
            "Y": SimpleNamespace(exact=False),
            "Z": SimpleNamespace(exact=True),
            "A": SimpleNamespace(exact=True),
        },
    )

    updates = {item.axis: item for item in plan.axis_updates}
    assert updates["X"].confidence_role == "exact"
    assert updates["Y"].confidence_role == "approximate"
    assert updates["Z"].confidence_role is None
    assert updates["A"].confidence_role is None
    assert "Accuracy: Exact" in updates["X"].tooltip
    assert "confirmed by the configured backlash approach" in updates["X"].tooltip
    assert "Accuracy: Approximate" in updates["Y"].tooltip
    assert "backlash approach has not completed" in updates["Y"].tooltip
    assert "Accuracy:" not in updates["Z"].tooltip


def test_display_plan_hides_confidence_when_coordinate_is_unavailable() -> None:
    plan = stage_position_display_plan(
        (1.0, "bad"),
        axis_names=("X", "Y"),
        homed_axes={"X", "Y"},
        limit_axes=set(),
        pending_targets={},
        display_axis_value=_display_axis_value,
        feedrate_mm_min=120.0,
        precision_enabled_axes={"X", "Y"},
        coordinate_confidence={
            "X": SimpleNamespace(exact=True),
            "Y": SimpleNamespace(exact=False),
        },
    )

    assert plan.axis_updates[0].confidence_role == "exact"
    assert plan.missing_axes == ("Y",)


def test_pending_target_visible_value_overrides_live_display_value() -> None:
    plan = stage_position_display_plan(
        (1.0, 2.0),
        axis_names=("X", "Y"),
        homed_axes={"X", "Y"},
        limit_axes=set(),
        pending_targets={"X": (10.0, 12.5)},
        display_axis_value=_display_axis_value,
        feedrate_mm_min=120.0,
    )

    updates = {item.axis: item for item in plan.axis_updates}

    assert math.isclose(updates["X"].display_value, 1.5)
    assert math.isclose(updates["X"].visible_value, 12.5)
    assert math.isclose(updates["Y"].visible_value, 3.0)


def test_invalid_axis_value_is_skipped_and_marked_missing() -> None:
    plan = stage_position_display_plan(
        ("bad", 2.0),
        axis_names=("X", "Y"),
        homed_axes={"Y"},
        limit_axes=set(),
        pending_targets={},
        display_axis_value=_display_axis_value,
        feedrate_mm_min=120.0,
    )

    assert [item.axis for item in plan.axis_updates] == ["Y"]
    assert plan.missing_axes == ("X",)
    assert plan.fields_available is True


def test_display_plan_ignores_axes_without_fields() -> None:
    plan = stage_position_display_plan(
        (1.0, 2.0, 3.0),
        axis_names=("X", "Y", "Z"),
        available_axes={"X", "Y"},
        homed_axes={"X", "Y", "Z"},
        limit_axes=set(),
        pending_targets={},
        display_axis_value=_display_axis_value,
        feedrate_mm_min=120.0,
    )

    assert [item.axis for item in plan.axis_updates] == ["X", "Y"]
    assert plan.missing_axes == ()
    assert plan.fields_available is True


def test_display_plan_with_no_updated_axes_disables_fields() -> None:
    plan = stage_position_display_plan(
        ("bad", object()),
        axis_names=("X", "Y"),
        homed_axes={"X"},
        limit_axes=set(),
        pending_targets={"X": (1.0, 2.0)},
        display_axis_value=_display_axis_value,
        feedrate_mm_min=120.0,
    )

    assert plan.valid is True
    assert plan.axis_updates == ()
    assert plan.missing_axes == ("X", "Y")
    assert plan.fields_available is False
    assert plan.reset_all is False


def test_contact_calibration_position_requires_xyz_homing_and_three_axes() -> None:
    plan = stage_position_status_plan(
        (1.0, 2.0, 3.0),
        latest_state="idle",
        coordinate_move_axis_active=False,
        xy_homed=True,
        xyz_homed=True,
        manual_jog_prediction_available=False,
        can_display_design_position=True,
    )

    assert plan.contact_calibration_position == (1.0, 2.0, 3.0)

    not_homed = stage_position_status_plan(
        (1.0, 2.0, 3.0),
        latest_state="idle",
        coordinate_move_axis_active=False,
        xy_homed=True,
        xyz_homed=False,
        manual_jog_prediction_available=False,
        can_display_design_position=True,
    )
    short_position = stage_position_status_plan(
        (1.0, 2.0),
        latest_state="idle",
        coordinate_move_axis_active=False,
        xy_homed=True,
        xyz_homed=True,
        manual_jog_prediction_available=False,
        can_display_design_position=True,
    )

    assert not_homed.contact_calibration_position is None
    assert short_position.contact_calibration_position is None


def test_b_axis_status_preserves_durable_registration_across_motion() -> None:
    plan = b_axis_registration_plan(
        (1.0, 2.0, 3.0, 4.0, 5.5),
        last_reported_b_position=5.0,
        tolerance_deg=0.1,
        pending_alignment_preparation=None,
        registration_valid=True,
    )

    assert plan == BAxisRegistrationPlan(
        current_b=5.5,
        invalidate_registration=False,
        invalidate_reason=None,
    )

    below_tolerance = b_axis_registration_plan(
        (1.0, 2.0, 3.0, 4.0, 5.05),
        last_reported_b_position=5.0,
        tolerance_deg=0.1,
        pending_alignment_preparation=None,
        registration_valid=True,
    )
    pending_alignment = b_axis_registration_plan(
        (1.0, 2.0, 3.0, 4.0, 5.5),
        last_reported_b_position=5.0,
        tolerance_deg=0.1,
        pending_alignment_preparation=object(),
        registration_valid=True,
    )
    invalid_registration = b_axis_registration_plan(
        (1.0, 2.0, 3.0, 4.0, 5.5),
        last_reported_b_position=5.0,
        tolerance_deg=0.1,
        pending_alignment_preparation=None,
        registration_valid=False,
    )

    assert below_tolerance.invalidate_registration is False
    assert pending_alignment.invalidate_registration is False
    assert invalid_registration.invalidate_registration is False


def test_predicted_position_source_selection_prefers_manual_then_coordinate_then_planned_then_raw() -> None:
    manual = predicted_stage_position_plan(
        manual_jog_prediction_available=True,
        manual_jog_stage_position=(9.0, 8.0, 7.0),
        coordinate_move_stage_position=(4.0, 5.0, 6.0),
        planned_move_started_at=1.0,
        planned_move_waiting_for_fresh_status=True,
        planned_move_stage_xy=(3.0, 2.0),
        position=(1.0, 2.0, 3.0),
        stage_xy_from_position=_stage_xy_from_position,
        position_with_stage_xy=_position_with_stage_xy,
    )
    coordinate = predicted_stage_position_plan(
        manual_jog_prediction_available=False,
        manual_jog_stage_position=None,
        coordinate_move_stage_position=(4.0, 5.0, 6.0),
        planned_move_started_at=1.0,
        planned_move_waiting_for_fresh_status=True,
        planned_move_stage_xy=(3.0, 2.0),
        position=(1.0, 2.0, 3.0),
        stage_xy_from_position=_stage_xy_from_position,
        position_with_stage_xy=_position_with_stage_xy,
    )
    planned = predicted_stage_position_plan(
        manual_jog_prediction_available=False,
        manual_jog_stage_position=None,
        coordinate_move_stage_position=None,
        planned_move_started_at=1.0,
        planned_move_waiting_for_fresh_status=False,
        planned_move_stage_xy=(3.0, 2.0),
        position=(1.0, 2.0, 3.0),
        stage_xy_from_position=_stage_xy_from_position,
        position_with_stage_xy=_position_with_stage_xy,
    )
    raw = predicted_stage_position_plan(
        manual_jog_prediction_available=False,
        manual_jog_stage_position=None,
        coordinate_move_stage_position=None,
        planned_move_started_at=None,
        planned_move_waiting_for_fresh_status=False,
        planned_move_stage_xy=(3.0, 2.0),
        position=(1.0, 2.0, 3.0),
        stage_xy_from_position=_stage_xy_from_position,
        position_with_stage_xy=_position_with_stage_xy,
    )

    assert manual.source == "manual_jog"
    assert manual.smooth_predicted_status is True
    assert coordinate.source == "coordinate_move"
    assert coordinate.smooth_predicted_status is False
    assert planned.source == "planned_move"
    assert planned.predicted_position == (3.0, 2.0, 3.0)
    assert planned.predicted_stage_xy == (3.0, 2.0)
    assert raw.source == "raw_status"
    assert raw.predicted_position is None
    assert raw.predicted_stage_xy is None


def test_active_planned_move_reconcile_uses_predicted_xy_directly() -> None:
    prediction = predicted_stage_position_plan(
        manual_jog_prediction_available=False,
        manual_jog_stage_position=None,
        coordinate_move_stage_position=None,
        planned_move_started_at=1.0,
        planned_move_waiting_for_fresh_status=False,
        planned_move_stage_xy=(5.0, 5.0),
        position=(0.0, 0.0, 4.0),
        stage_xy_from_position=_stage_xy_from_position,
        position_with_stage_xy=_position_with_stage_xy,
    )

    plan = stage_reconcile_plan(
        actual_stage_xy=(0.0, 0.0),
        predicted_stage_xy=prediction.predicted_stage_xy,
        smooth_predicted_status=prediction.smooth_predicted_status,
        planned_move_active=prediction.planned_move_active,
    )

    assert plan == StageReconcilePlan(
        actual_stage_xy=(0.0, 0.0),
        predicted_stage_xy=(5.0, 5.0),
        log_reconcile=True,
        use_predicted_xy_directly=True,
        smooth_to_actual=False,
    )


def test_signal_plan_marks_unhomed_fallback_and_deferred_stop_sample() -> None:
    plan = stage_position_signal_plan(
        (1.0, 2.0, 3.0),
        latest_state="run",
        coordinate_move_axis_active=True,
        xy_homed=False,
        xyz_homed=False,
        manual_jog_prediction_available=False,
        can_display_design_position=True,
        last_reported_b_position=None,
        tolerance_deg=0.1,
        pending_alignment_preparation=None,
        registration_valid=False,
        manual_jog_stage_position=None,
        coordinate_move_stage_position=None,
        planned_move_started_at=None,
        planned_move_waiting_for_fresh_status=False,
        planned_move_stage_xy=None,
        manual_jog_waiting_for_fresh_status=True,
        stage_xy_from_position=_stage_xy_from_position,
        position_with_stage_xy=_position_with_stage_xy,
    )

    assert plan.status.mark_coordinate_move_active is True
    assert plan.status.use_unhomed_fallback is True
    assert plan.status.unhomed_design_position == (1.0, 2.0)
    assert plan.defer_manual_jog_stop_sample is True
    assert plan.prediction.source == "raw_status"
    assert plan.reconcile.log_reconcile is False


def test_signal_plan_carries_planned_move_prediction_and_reconcile_mode() -> None:
    plan = stage_position_signal_plan(
        (0.0, 0.0, 4.0),
        latest_state="run",
        coordinate_move_axis_active=False,
        xy_homed=True,
        xyz_homed=True,
        manual_jog_prediction_available=False,
        can_display_design_position=False,
        last_reported_b_position=4.0,
        tolerance_deg=0.1,
        pending_alignment_preparation=None,
        registration_valid=True,
        manual_jog_stage_position=None,
        coordinate_move_stage_position=None,
        planned_move_started_at=1.0,
        planned_move_waiting_for_fresh_status=False,
        planned_move_stage_xy=(5.0, 5.0),
        manual_jog_waiting_for_fresh_status=False,
        stage_xy_from_position=_stage_xy_from_position,
        position_with_stage_xy=_position_with_stage_xy,
    )

    assert plan.prediction.source == "planned_move"
    assert plan.prediction.predicted_stage_xy == (5.0, 5.0)
    assert plan.reconcile.use_predicted_xy_directly is True
    assert plan.defer_manual_jog_stop_sample is False
