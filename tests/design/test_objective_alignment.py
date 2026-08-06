import math

from probe_station_gui.design.objective_offsets import ObjectiveOffsetReference
from probe_station_gui.design.objective_alignment import (
    alignment_capture_position_plan,
    design_alignment_capture_plan,
    manual_alignment_capture_plan,
    objective_change_offset_plan,
    objective_combo_sync_plan,
    objective_offset_reference_plan,
    profile_add_plan,
    profile_delete_plan,
    reset_active_objective_offset,
    save_active_objective_offset,
    select_active_objective,
    update_objective_calibration,
)
from probe_station_gui.design.session import AlignmentPreparation
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)


def _settings() -> Settings:
    settings = Settings()
    settings.objectives = ObjectivesSettings(
        active_name="X5",
        objectives={
            "X5": ObjectiveCalibrationSettings(
                name="X5",
                magnification=5.0,
                xy_offset_configured=True,
            ),
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                magnification=20.0,
                xy_offset_x_mm=0.12,
                xy_offset_y_mm=-0.08,
                xy_offset_configured=True,
            ),
        },
    )
    return settings


def _preparation(rotation_deg: float, ratio: float = 1.0) -> AlignmentPreparation:
    return AlignmentPreparation(
        design_marks=((0.0, 0.0), (1.0, 0.0)),
        stage_marks_before_rotation=((10.0, 20.0), (11.0, 20.0)),
        stage_marks_after_rotation=((10.0, 20.0), (11.0, 20.0)),
        pivot_stage=(0.0, 0.0),
        rotation_deg=rotation_deg,
        design_distance_mm=1.0,
        stage_distance_mm=ratio,
        distance_ratio=ratio,
        rms_residual_mm=0.0123,
        max_residual_mm=0.0456,
    )


def test_combo_sync_rebuilds_only_when_item_data_differs() -> None:
    rebuild = objective_combo_sync_plan(["X5"], ["X5", "X20"], "x20")
    unchanged = objective_combo_sync_plan(["X5", "X20"], ["X5", "X20"], "X5")

    assert rebuild.rebuild_items is True
    assert rebuild.selected_index == 1
    assert rebuild.refresh_calibration_ui is True
    assert unchanged.rebuild_items is False
    assert unchanged.selected_index == 0


def test_select_active_objective_creates_missing_profile_and_requests_motion() -> None:
    settings = _settings()
    plan = select_active_objective(settings, " x50 ", is_busy=False, apply_motion=True)

    assert plan.status == "Objective selected: X50."
    assert plan.settings is not None
    assert plan.settings.objectives.active_name == "X50"
    assert "X50" in plan.settings.objectives.objectives
    assert plan.apply_settings is True
    assert plan.apply_offset_motion is True
    assert plan.old_name == "X5"


def test_select_active_objective_rejects_busy_and_restores_combo() -> None:
    plan = select_active_objective(_settings(), "X20", is_busy=True, apply_motion=True)

    assert plan.settings is None
    assert plan.restore_combo_name == "X5"
    assert plan.status == "Stage is busy; objective not changed."


def test_objective_change_offset_plans_xy_and_optional_z_targets() -> None:
    settings = _settings().objectives
    settings.objectives["X5"].z_offset_configured = True
    settings.objectives["X5"].z_offset_mm = 0.5
    settings.objectives["X20"].z_offset_configured = True
    settings.objectives["X20"].z_offset_mm = 0.8

    plan = objective_change_offset_plan(
        settings,
        "X5",
        "X20",
        latest_position=(10.0, 20.0, 3.0),
        is_busy=False,
        display_axis_value_from_raw=lambda _axis, raw: raw + 100.0,
        raw_axis_value_from_display=lambda _axis, display: display - 100.0,
    )

    assert plan.raw_targets is not None
    assert plan.raw_targets["X"] == 10.12
    assert plan.raw_targets["Y"] == 19.92
    assert math.isclose(plan.raw_targets["Z"], 3.3)
    assert plan.accepted_status == "Applying X20 objective offset on X, Y, Z."
    assert plan.rejected_status == "Objective offset move was not accepted."


def test_objective_change_offset_reports_rejection_states() -> None:
    settings = _settings().objectives
    settings.objectives["X20"].xy_offset_configured = False
    missing = objective_change_offset_plan(settings, "X5", "X20", (1.0, 2.0), False)
    settings.objectives["X20"].xy_offset_configured = True
    unavailable = objective_change_offset_plan(settings, "X5", "X20", None, False)
    busy = objective_change_offset_plan(settings, "X5", "X20", (1.0, 2.0), True)

    assert missing.status == "Objective XY offset is not configured for both objectives."
    assert unavailable.status == "Stage position unavailable; objective offset not applied."
    assert busy.status == "Stage is busy; objective offset not applied."


def test_profile_add_delete_reset_and_calibration_plans_mutate_settings() -> None:
    settings = _settings()
    add = profile_add_plan(settings, " x10 ")
    assert add.settings is not None
    assert add.settings.objectives.active_name == "X10"
    assert add.status == "Objective added: X10."

    duplicate = profile_add_plan(add.settings, "X10")
    assert duplicate.select_existing_name == "X10"
    assert duplicate.status == "Objective already exists: X10."

    delete = profile_delete_plan(add.settings, "X10", confirmed=True)
    assert delete.settings is not None
    assert "X10" not in delete.settings.objectives.objectives
    assert delete.settings.objectives.active_name == "X5"

    reset = reset_active_objective_offset(settings)
    assert reset.settings is not None
    assert reset.settings.objectives.objectives["X5"].xy_offset_configured is True

    updated = update_objective_calibration(settings, "X20", [[1, 0], [0, 2]])
    assert updated.settings is not None
    assert updated.settings.objectives.objectives["X20"].pixels_to_mm == [
        [1.0, 0.0],
        [0.0, 2.0],
    ]


def test_update_objective_calibration_preserves_old_permissive_matrix_conversion() -> None:
    settings = _settings()

    singular = update_objective_calibration(settings, "X20", [[1, 2], [2, 4]])
    nonfinite = update_objective_calibration(settings, "X20", [[1, "nan"], [0, 1]])

    assert singular.settings is not None
    singular_profile = singular.settings.objectives.objectives["X20"]
    assert singular_profile.pixels_to_mm == [[1.0, 2.0], [2.0, 4.0]]
    assert singular_profile.xy_calibration_configured is True

    assert nonfinite.settings is not None
    nonfinite_profile = nonfinite.settings.objectives.objectives["X20"]
    assert nonfinite_profile.pixels_to_mm[0][0] == 1.0
    assert math.isnan(nonfinite_profile.pixels_to_mm[0][1])
    assert nonfinite_profile.pixels_to_mm[1] == [0.0, 1.0]
    assert nonfinite_profile.xy_calibration_configured is True


def test_objective_offset_reference_save_and_reset_base_and_non_base() -> None:
    settings = _settings()
    base = objective_offset_reference_plan(settings, (10.0, 20.0))
    assert base.settings is not None
    assert base.reference == ObjectiveOffsetReference("X5", (10.0, 20.0), (0.0, 0.0))
    assert base.status.startswith("Objective offset reference set with X5.")

    settings.objectives.active_name = "X20"
    reference = ObjectiveOffsetReference("X5", (10.0, 20.0), (0.0, 0.0))
    saved = save_active_objective_offset(settings, reference, (10.125, 19.75))
    assert saved.settings is not None
    profile = saved.settings.objectives.objectives["X20"]
    assert (profile.xy_offset_x_mm, profile.xy_offset_y_mm) == (0.125, -0.25)
    assert saved.status == "Saved X20 objective offset: X=+0.1250, Y=-0.2500 mm."

    reset = reset_active_objective_offset(saved.settings)
    assert reset.settings is not None
    assert reset.settings.objectives.objectives["X20"].xy_offset_configured is False


def test_alignment_capture_position_falls_back_for_unreadable_stage_position() -> None:
    fallback = alignment_capture_position_plan(
        error_message="Unable to read stage position.",
        latest_position=(1.25, 2.5, 0.0),
    )
    missing = alignment_capture_position_plan(
        error_message="Unable to read stage position.",
        latest_position=None,
    )

    assert fallback.stage_xy == (1.25, 2.5)
    assert fallback.status is None
    assert missing.stage_xy is None
    assert missing.request_status_refresh is True
    assert missing.status == "Unable to read stage position."


def test_manual_alignment_capture_handles_close_aligned_and_rotation_cases() -> None:
    close = manual_alignment_capture_plan(
        1,
        (1.0, 1.0),
        source="center",
        manual_points=[(1.0, 1.0), None],
    )
    first = manual_alignment_capture_plan(
        0,
        (1.0, 2.0),
        source="image",
        manual_points=[None, None],
    )
    aligned = manual_alignment_capture_plan(
        1,
        (2.0, 1.0),
        source="center",
        manual_points=[(1.0, 1.0), None],
    )
    rotate = manual_alignment_capture_plan(
        1,
        (2.0, 2.0),
        source="center",
        manual_points=[(1.0, 1.0), None],
    )

    assert close.status == "Chip alignment points are too close together. Capture two distinct points."
    assert first.status.endswith("Capture point 2 next.")
    assert aligned.rotation_deg == 0.0
    assert aligned.collapse_alignment_if_design_open is True
    assert rotate.rotation_deg == -45.0
    assert rotate.request_b_rotation is False
    assert rotate.invalidate_design_registration is False
    assert "unavailable" in str(rotate.status).lower()


def test_design_alignment_capture_plans_first_point_rejections_apply_and_rotation() -> None:
    first = design_alignment_capture_plan(
        slot=0,
        stage_xy=(10.0, 20.0),
        source="image",
        pair_count=1,
        preparation=None,
        preparation_error=None,
        spacing_reasonable=True,
    )
    prep_error = design_alignment_capture_plan(
        slot=1,
        stage_xy=(11.0, 20.0),
        source="center",
        pair_count=2,
        preparation=None,
        preparation_error="Exactly two mark pairs are required for calibration.",
        spacing_reasonable=True,
    )
    mismatch = design_alignment_capture_plan(
        slot=1,
        stage_xy=(11.0, 20.0),
        source="center",
        pair_count=2,
        preparation=_preparation(0.0, ratio=1.4),
        preparation_error=None,
        spacing_reasonable=False,
    )
    apply = design_alignment_capture_plan(
        slot=1,
        stage_xy=(11.0, 20.0),
        source="center",
        pair_count=2,
        preparation=_preparation(0.0005),
        preparation_error=None,
        spacing_reasonable=True,
    )
    rotate = design_alignment_capture_plan(
        slot=1,
        stage_xy=(11.0, 20.0),
        source="center",
        pair_count=2,
        preparation=_preparation(12.5),
        preparation_error=None,
        spacing_reasonable=True,
    )

    assert first.status.endswith("Capture 1 remaining point.")
    assert first.expand_alignment is True
    assert prep_error.status == "Exactly two mark pairs are required for calibration."
    assert mismatch.status == (
        "Design calibration aborted: mark spacing mismatch. "
        "Design 1.0000 mm vs chip 1.4000 mm."
    )
    assert apply.apply_prepared_alignment is True
    assert apply.disable_snap is True
    assert apply.status == (
        "Design calibration complete. Spacing ratio 1.000. "
        "RMS 0.0123 mm, max 0.0456 mm."
    )
    assert rotate.request_b_rotation is False
    assert rotate.pending_preparation is None
    assert "unavailable" in str(rotate.status).lower()
    assert math.isclose(rotate.rotation_deg or 0.0, 12.5)


def test_design_alignment_waits_for_every_multipoint_capture() -> None:
    plan = design_alignment_capture_plan(
        slot=1,
        stage_xy=(11.0, 20.0),
        source="center",
        pair_count=2,
        required_pair_count=3,
        preparation=None,
        preparation_error=None,
        spacing_reasonable=True,
    )

    assert plan.pending_preparation is None
    assert plan.request_b_rotation is False
    assert plan.status is not None
    assert plan.status.endswith("Capture 1 remaining point.")
