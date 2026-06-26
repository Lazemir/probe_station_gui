import pytest
import types

from probe_station_gui.route.session_start import (
    DEFAULT_EXTERNAL_SESSION_FOLLOWUP_MEASUREMENT_COUNT,
    DEFAULT_EXTERNAL_SESSION_INITIAL_MEASUREMENT_COUNT,
    DEFAULT_EXTERNAL_SESSION_PHOTO_FOCUS_RANGE_MM,
    DEFAULT_EXTERNAL_SESSION_PHOTO_SETTLE_S,
    api_route_session_start_decision,
    route_launch_presentation,
    route_contact_quality_limits_from_payload,
    route_external_session_start_settings_from_payload,
    route_max_relative_rms_from_payload,
)
from probe_station_gui.route.measurement_records import RouteMeasurementPoint


def test_route_external_session_start_settings_apply_defaults() -> None:
    settings = route_external_session_start_settings_from_payload(
        {},
        default_start_point=3,
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=-0.002,
        default_contact_settle_s=0.4,
    )

    assert settings.start_point == 3
    assert (
        settings.initial_measurement_count
        == DEFAULT_EXTERNAL_SESSION_INITIAL_MEASUREMENT_COUNT
    )
    assert (
        settings.followup_measurement_count
        == DEFAULT_EXTERNAL_SESSION_FOLLOWUP_MEASUREMENT_COUNT
    )
    assert (
        settings.measurement_count
        == DEFAULT_EXTERNAL_SESSION_INITIAL_MEASUREMENT_COUNT
        + DEFAULT_EXTERNAL_SESSION_FOLLOWUP_MEASUREMENT_COUNT
    )
    assert settings.contact_seek_range_mm == 0.05
    assert settings.contact_seek_step_mm == 0.002
    assert settings.contact_settle_s == 0.4
    assert settings.photo_settle_s == DEFAULT_EXTERNAL_SESSION_PHOTO_SETTLE_S
    assert settings.photo_focus_range_mm == DEFAULT_EXTERNAL_SESSION_PHOTO_FOCUS_RANGE_MM
    assert settings.photo_enabled is True
    assert settings.photo_focus_enabled is True
    assert settings.max_relative_rms is None


def test_route_external_session_start_settings_accept_aliases() -> None:
    settings = route_external_session_start_settings_from_payload(
        {
            "current_point": 5,
            "initial_samples": 2,
            "followup_samples": 4,
            "samples": 8,
            "seek_range_mm": 0.07,
            "seek_step_mm": 0.003,
            "settle_s": 0.2,
            "photo": "0",
            "focus": "yes",
            "focus_range_mm": 0.04,
            "max_relative_rms_percent": 1.5,
        },
        default_start_point=1,
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
    )

    assert settings.start_point == 5
    assert settings.initial_measurement_count == 2
    assert settings.followup_measurement_count == 4
    assert settings.measurement_count == 8
    assert settings.contact_seek_range_mm == 0.07
    assert settings.contact_seek_step_mm == 0.003
    assert settings.contact_settle_s == 0.2
    assert settings.photo_enabled is False
    assert settings.photo_focus_enabled is True
    assert settings.photo_focus_range_mm == 0.04
    assert settings.max_relative_rms == pytest.approx(0.015)


def test_route_contact_quality_limits_from_payload_merges_nested_values() -> None:
    limits = route_contact_quality_limits_from_payload(
        {
            "max_mad_sigma_ohm": 100.0,
            "contact_quality": {
                "max_mad_sigma_ohm": 200.0,
                "max_p95_abs_step_ohm": 300.0,
                "max_relative_mad_sigma": 0.03,
                "max_relative_p95_abs_step": 0.04,
            },
        }
    )

    assert limits.max_mad_sigma_ohm == 200.0
    assert limits.max_p95_abs_step_ohm == 300.0
    assert limits.max_relative_mad_sigma == 0.03
    assert limits.max_relative_p95_abs_step == 0.04


def test_route_session_start_payload_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="contact_quality must be an object"):
        route_contact_quality_limits_from_payload({"contact_quality": "bad"})

    with pytest.raises(ValueError, match="measurement_count must be at least"):
        route_external_session_start_settings_from_payload(
            {"initial_measurement_count": 5, "measurement_count": 4},
            default_start_point=1,
            default_contact_seek_range_mm=0.05,
            default_contact_seek_step_mm=0.002,
            default_contact_settle_s=0.4,
        )


def test_route_max_relative_rms_from_payload_accepts_fraction_and_percent() -> None:
    assert route_max_relative_rms_from_payload({}) is None
    assert route_max_relative_rms_from_payload({"max_rel_rms": 0.02}) == 0.02
    assert route_max_relative_rms_from_payload(
        {"max_relative_rms_percent": 2.5}
    ) == pytest.approx(0.025)


def test_api_route_session_start_decision_builds_points_settings_and_contact_selection() -> None:
    points = [
        RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(0.0, 0.0),
            stage_xy=(1.0, 1.0),
            needle_1_design=(0.0, 0.0),
            needle_2_design=(0.0, 0.0),
        ),
        RouteMeasurementPoint(
            index=2,
            point_id="contact-105",
            label="Pad 105",
            design_center=(10.0, 20.0),
            stage_xy=(2.0, 3.0),
            needle_1_design=(10.0, 20.0),
            needle_2_design=(10.0, 20.0),
        ),
    ]

    decision = api_route_session_start_decision(
        route=types.SimpleNamespace(points=[object()]),
        registration_valid=True,
        payload={
            "contact_number": 105,
            "initial_samples": 3,
            "followup_samples": 4,
            "samples": 9,
            "seek_range_mm": 0.07,
            "seek_step_mm": 0.003,
            "settle_s": 0.2,
            "photo": False,
            "focus": True,
            "focus_range_mm": 0.04,
            "max_relative_rms_percent": 1.5,
        },
        current_point=1,
        points_factory=lambda _route: points,
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
    )

    assert decision.accepted is True
    assert decision.plan is not None
    assert decision.plan.points == points
    assert decision.plan.selected_point == points[1]
    assert decision.plan.start_settings.start_point == 105
    assert decision.plan.start_settings.initial_measurement_count == 3
    assert decision.plan.start_settings.followup_measurement_count == 4
    assert decision.plan.start_settings.measurement_count == 9
    assert decision.plan.start_settings.contact_seek_range_mm == 0.07
    assert decision.plan.start_settings.contact_seek_step_mm == 0.003
    assert decision.plan.start_settings.contact_settle_s == 0.2
    assert decision.plan.start_settings.photo_enabled is False
    assert decision.plan.start_settings.photo_focus_enabled is True
    assert decision.plan.start_settings.photo_focus_range_mm == 0.04
    assert decision.plan.start_settings.max_relative_rms == pytest.approx(0.015)


@pytest.mark.parametrize(
    ("route", "registration_valid", "expected_status", "expected_message"),
    [
        (
            None,
            True,
            409,
            "Create or load a probe route before starting a route session.",
        ),
        (
            types.SimpleNamespace(points=[object()]),
            False,
            409,
            "Design registration is required before using contacts.",
        ),
    ],
)
def test_api_route_session_start_decision_rejects_invalid_route_context(
    route: object | None,
    registration_valid: bool,
    expected_status: int,
    expected_message: str,
) -> None:
    decision = api_route_session_start_decision(
        route=route,
        registration_valid=registration_valid,
        payload={},
        current_point=1,
        points_factory=lambda _route: [],
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
    )

    assert decision.accepted is False
    assert decision.plan is None
    assert decision.status_code == expected_status
    assert decision.message == expected_message


def test_route_launch_presentation_formats_gui_messages_and_remaining_count() -> None:
    points = [
        RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(0.0, 0.0),
            stage_xy=(1.0, 1.0),
            needle_1_design=(0.0, 0.0),
            needle_2_design=(0.0, 0.0),
        ),
        RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 10.0),
            stage_xy=(2.0, 2.0),
            needle_1_design=(10.0, 10.0),
            needle_2_design=(10.0, 10.0),
        ),
        RouteMeasurementPoint(
            index=9,
            point_id="p009",
            label="P009",
            design_center=(20.0, 20.0),
            stage_xy=(3.0, 3.0),
            needle_1_design=(20.0, 20.0),
            needle_2_design=(20.0, 20.0),
        ),
    ]

    started = route_launch_presentation(
        points,
        points[1],
        wait_before_first_point=False,
        previous_ok_skipped_count=2,
    )
    waiting = route_launch_presentation(
        points,
        points[1],
        wait_before_first_point=True,
    )
    api_ready = route_launch_presentation(points, points[1], api_session=True)

    assert started.point_numbers == [1, 7, 9]
    assert started.remaining_count == 2
    assert (
        started.message
        == "Route measurement starting at point 7 P007; 2 points remaining. Previous filter skipped 2 points."
    )
    assert waiting.message == "Preparing route measurement: point 7 P007; 2 points selected."
    assert api_ready.message == "Route API session ready at point 7 P007; 3 points selected."
