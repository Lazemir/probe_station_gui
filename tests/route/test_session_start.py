import pytest
import types

import probe_station_gui.route.session_start as session_start_module
from probe_station_gui.design.model import DesignModelError
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
    snapshot_route_design_frame,
)
from probe_station_gui.route.measurement_records import RouteMeasurementPoint


DURABLE_FRAME_SNAPSHOT = snapshot_route_design_frame(
    frame_id="design-a",
    frame_version=4,
)


def _new_helper(name: str):
    helper = getattr(session_start_module, name, None)
    assert helper is not None
    return helper


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
        design_frame_snapshot=DURABLE_FRAME_SNAPSHOT,
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


def test_route_start_snapshots_active_design_frame_identity_and_version() -> None:
    frame = snapshot_route_design_frame(frame_id="design-a", frame_version=4)
    point = RouteMeasurementPoint(
        index=1,
        point_id="p001",
        label="P001",
        design_center=(0.0, 0.0),
        stage_xy=(1.0, 1.0),
        needle_1_design=(0.0, 0.0),
        needle_2_design=(0.0, 0.0),
    )

    result = api_route_session_start_decision(
        route=types.SimpleNamespace(points=[object()]),
        registration_valid=True,
        payload={},
        current_point=1,
        points_factory=lambda _route: [point],
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
        design_frame_snapshot=frame,
    )

    assert result.plan is not None
    assert result.plan.design_frame_snapshot is frame
    assert result.plan.design_frame_snapshot.frame_id == "design-a"
    assert result.plan.design_frame_snapshot.frame_version == 4


def test_api_route_start_rejects_missing_durable_frame_snapshot() -> None:
    decision = api_route_session_start_decision(
        route=types.SimpleNamespace(points=[object()]),
        registration_valid=True,
        payload={},
        current_point=1,
        points_factory=lambda _route: pytest.fail(
            "points must not resolve without a durable frame"
        ),
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
        design_frame_snapshot=None,
    )

    assert decision.accepted is False
    assert decision.plan is None
    assert decision.status_code == 409
    assert decision.message == (
        "Design coordinate frame is required before using contacts."
    )


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


def test_api_route_session_start_decision_rejects_points_factory_model_error() -> None:
    def failing_points_factory(_route: object) -> list[RouteMeasurementPoint]:
        raise DesignModelError("Design registration is required before measuring a route.")

    decision = api_route_session_start_decision(
        route=types.SimpleNamespace(points=[object()]),
        registration_valid=True,
        payload={},
        current_point=1,
        points_factory=failing_points_factory,
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
        design_frame_snapshot=DURABLE_FRAME_SNAPSHOT,
    )

    assert decision.accepted is False
    assert decision.plan is None
    assert decision.status_code == 409
    assert (
        decision.message
        == "Design registration is required before measuring a route."
    )


def test_api_route_session_start_decision_rejects_empty_resolved_points() -> None:
    decision = api_route_session_start_decision(
        route=types.SimpleNamespace(points=[object()]),
        registration_valid=True,
        payload={},
        current_point=1,
        points_factory=lambda _route: [],
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
        design_frame_snapshot=DURABLE_FRAME_SNAPSHOT,
    )

    assert decision.accepted is False
    assert decision.plan is None
    assert decision.status_code == 409
    assert decision.message == "Route has no enabled points."


def test_api_route_session_start_decision_rejects_invalid_payload_with_400() -> None:
    points = [
        RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(0.0, 0.0),
            stage_xy=(1.0, 1.0),
            needle_1_design=(0.0, 0.0),
            needle_2_design=(0.0, 0.0),
        )
    ]

    decision = api_route_session_start_decision(
        route=types.SimpleNamespace(points=[object()]),
        registration_valid=True,
        payload={"measurement_count": "bad"},
        current_point=1,
        points_factory=lambda _route: points,
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
        design_frame_snapshot=DURABLE_FRAME_SNAPSHOT,
    )

    assert decision.accepted is False
    assert decision.plan is None
    assert decision.status_code == 400
    assert decision.message == "Invalid integer value for measurement_count."


def test_api_route_session_start_decision_rejects_filtered_out_selected_contact() -> None:
    points = [
        RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(0.0, 0.0),
            stage_xy=(1.0, 1.0),
            needle_1_design=(0.0, 0.0),
            needle_2_design=(0.0, 0.0),
        )
    ]

    decision = api_route_session_start_decision(
        route=types.SimpleNamespace(points=[object()]),
        registration_valid=True,
        payload={"start_point": 2},
        current_point=1,
        points_factory=lambda _route: points,
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
        design_frame_snapshot=DURABLE_FRAME_SNAPSHOT,
    )

    assert decision.accepted is False
    assert decision.plan is None
    assert decision.status_code == 409
    assert (
        decision.message
        == "Contact 2 is not enabled or not included by the current route filter."
    )


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


def test_api_route_existing_session_response_attaches_or_rejects() -> None:
    existing_response = _new_helper("api_route_existing_session_response")
    active_status = {
        "accepted": True,
        "session_id": "session-123",
        "state": "waiting_paused",
        "position": 4,
        "current_contact": {
            "label": "P004",
            "contact_number": 4,
        },
    }

    for alias in ("attach_existing_session", "attach_existing", "resume_existing"):
        attached = existing_response(
            payload={alias: True},
            status=active_status,
        )
        assert attached == active_status

    rejected = existing_response(
        payload={},
        status=active_status,
    )

    assert rejected == {
        "accepted": False,
        "status_code": 409,
        "message": (
            "External route session is already active at P004. "
            "Stop it first or pass attach_existing_session=true to attach explicitly."
        ),
        "active_session": active_status,
    }


def test_gui_route_start_availability_preserves_rejection_messages() -> None:
    start_availability = _new_helper("gui_route_start_availability")
    active = start_availability(
        route_thread_active=True,
        serial_connected=True,
    )
    disconnected = start_availability(
        route_thread_active=False,
        serial_connected=False,
    )

    assert active.accepted is False
    assert active.message == "Route measurement is already active."
    assert active.timeout_ms == 4000
    assert disconnected.accepted is False
    assert disconnected.message == "Connect the stage controller before measuring a route."
    assert disconnected.timeout_ms == 5000


def test_gui_route_start_preflight_requires_scale_only_for_photo_routes() -> None:
    start_preflight = _new_helper("gui_route_start_preflight")
    photo = start_preflight(
        photo_enabled=True,
        photo_autofocus_enabled=False,
        wait_before_first_point=False,
        objective_scale_available=False,
    )
    measure_autofocus = start_preflight(
        photo_enabled=False,
        photo_autofocus_enabled=True,
        wait_before_first_point=False,
        objective_scale_available=False,
    )

    assert photo.accepted is False
    assert (
        photo.message
        == "Calibrate click-to-move for the active objective before saving "
        "microscope photos with a scale bar."
    )
    assert photo.timeout_ms == 8000
    assert photo.dialog_status is True
    assert measure_autofocus.accepted is True
    assert measure_autofocus.check_camera_frame is True


def test_gui_route_camera_frame_preflight_formats_photo_and_autofocus_failures() -> None:
    camera_frame_preflight = _new_helper("gui_route_camera_frame_preflight")
    photo = camera_frame_preflight(
        photo_enabled=True,
        photo_autofocus_enabled=False,
        camera_frame_available=False,
    )
    autofocus = camera_frame_preflight(
        photo_enabled=False,
        photo_autofocus_enabled=True,
        camera_frame_available=False,
    )

    assert photo.accepted is False
    assert photo.message == "Camera frame is unavailable; cannot capture route photos."
    assert photo.telegram_failure_text == (
        "Probe route could not start:\n"
        "Camera frame is unavailable; cannot capture route photos."
    )
    assert autofocus.accepted is False
    assert autofocus.message == "Camera frame is unavailable; cannot autofocus route points."
    assert autofocus.telegram_failure_text == (
        "Probe route could not start:\n"
        "Camera frame is unavailable; cannot autofocus route points."
    )


def test_gui_route_launch_state_combines_mode_flags_presentation_and_telegram() -> None:
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
            point_id="p002",
            label="P002",
            design_center=(10.0, 20.0),
            stage_xy=(2.0, 3.0),
            needle_1_design=(10.0, 20.0),
            needle_2_design=(10.0, 20.0),
        ),
    ]

    launch_state = _new_helper("gui_route_launch_state")
    immediate = launch_state(
        operation_mode="photo_then_measure",
        points=points,
        selected_point=points[1],
        wait_before_first_point=False,
        previous_ok_skipped_count=1,
    )
    waiting = launch_state(
        operation_mode="photo",
        points=points,
        selected_point=points[1],
        wait_before_first_point=True,
        previous_ok_skipped_count=None,
    )

    assert immediate.photo_enabled is True
    assert immediate.measure_enabled is True
    assert immediate.send_start_telegram is True
    assert immediate.presentation.point_numbers == [1, 2]
    assert "Previous filter skipped 1 points." in immediate.presentation.message
    assert waiting.photo_enabled is True
    assert waiting.measure_enabled is False
    assert waiting.send_start_telegram is False
    assert waiting.presentation.message == "Preparing route measurement: point 2 P002; 1 points selected."
