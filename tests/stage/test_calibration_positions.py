from __future__ import annotations

from types import SimpleNamespace

from probe_station_gui.stage import calibration_positions as positions


def test_a_position_failure_status_preserves_reason_mapping() -> None:
    assert (
        positions.a_position_failure_status("stage task is active")
        == "Unable to read A position: stage is busy."
    )
    assert (
        positions.a_position_failure_status("serial connection closed")
        == "Unable to read A position: serial connection is unavailable."
    )
    assert (
        positions.a_position_failure_status("status query failed")
        == "Unable to read A position: controller status was unavailable."
    )
    assert (
        positions.a_position_failure_status("does not include A axis")
        == "Unable to read A position: controller status did not include A."
    )
    assert (
        positions.a_position_failure_status("other")
        == "Unable to read A position: see log for details."
    )


def test_needle_target_save_plan_validates_and_formats_status() -> None:
    plan = positions.needle_target_save_plan(
        " Lower ",
        "-2.5",
        lowering_for_raw_a=lambda raw_a: abs(raw_a) + 0.25,
        display_a_for_lowering=lambda lowering: -lowering,
    )

    assert not isinstance(plan, str)
    assert plan.action_key == "lower"
    assert plan.lowering_mm == 2.75
    assert plan.status_message == (
        "Saved needle lower target A=-2.7500 (2.7500 mm lowering)."
    )
    assert (
        positions.needle_target_save_plan(
            "park",
            1.0,
            lowering_for_raw_a=lambda value: value,
            display_a_for_lowering=lambda value: value,
        )
        == "Unknown needle target 'park'."
    )
    assert (
        positions.needle_target_save_plan(
            "raise",
            "bad",
            lowering_for_raw_a=lambda value: value,
            display_a_for_lowering=lambda value: value,
        )
        == "Invalid A coordinate."
    )


def test_apply_needle_target_updates_only_selected_target() -> None:
    settings = SimpleNamespace(
        raise_position_mm=0.0,
        raise_position_configured=False,
        down_position_mm=0.0,
        down_position_configured=False,
    )
    plan = positions.NeedleTargetSavePlan(
        action_key="raise",
        lowering_mm=1.25,
        display_a=None,
        status_message="saved",
    )

    positions.apply_needle_target(settings, plan)

    assert settings.raise_position_mm == 1.25
    assert settings.raise_position_configured is True
    assert settings.down_position_configured is False


def test_surface_position_save_plan_and_apply_payload() -> None:
    plan = positions.surface_position_save_plan("Chip", (1, 2, 3, 4))
    settings = SimpleNamespace(
        chip_position=SimpleNamespace(),
        stone_position=SimpleNamespace(),
    )

    assert not isinstance(plan, str)
    positions.apply_surface_position(settings, plan)

    assert plan.target_key == "chip"
    assert plan.status_message == "Saved chip focus at X=1.0000, Y=2.0000, Z=3.0000 mm."
    assert settings.chip_position.x_mm == 1.0
    assert settings.chip_position.y_mm == 2.0
    assert settings.chip_position.z_mm == 3.0
    assert settings.chip_position.configured is True
    assert positions.surface_position_save_plan("bad", (1, 2, 3)) == (
        "Unknown calibration position 'bad'."
    )
    assert positions.surface_position_save_plan("chip", (1, 2)) == (
        "Controller did not report X/Y/Z coordinates."
    )


def test_surface_move_plan_uses_lower_transit_z_when_both_positions_exist() -> None:
    settings = SimpleNamespace(
        chip_position=SimpleNamespace(
            x_mm=1.0,
            y_mm=2.0,
            z_mm=5.0,
            configured=True,
        ),
        stone_position=SimpleNamespace(
            x_mm=10.0,
            y_mm=20.0,
            z_mm=3.0,
            configured=True,
        ),
    )

    plan = positions.surface_move_plan("chip", settings)

    assert not isinstance(plan, str)
    assert plan.x_mm == 1.0
    assert plan.y_mm == 2.0
    assert plan.z_mm == 5.0
    assert plan.transit_z_mm == 3.0
    assert plan.label == "chip position"
    settings.chip_position.configured = False
    assert positions.surface_move_plan("chip", settings) == (
        "Save the chip focus position first."
    )
