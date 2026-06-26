from __future__ import annotations

from types import SimpleNamespace

import pytest

from probe_station_gui.route.contact_quality import (
    RouteContactQuality,
    RouteContactQualityLimits,
    RouteMeasurementSample,
)
from probe_station_gui.route.measurement import RouteMeasurementRunner
from probe_station_gui.route.measurement_records import (
    RouteContactPlacementResult,
    RouteContactSeekResult,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)
from probe_station_gui.route.api_measurement import (
    ApiCurrentContactFailureAlert,
    ApiCurrentContactSettings,
    api_contact_number_from_payload,
    api_current_contact_failure_alert,
    api_current_contact_response,
    api_current_contact_settings_from_payload,
)


def _contact_payload() -> dict[str, object]:
    return {
        "contact_number": 7,
        "route_index": 7,
        "structure_number": 107,
        "point_id": "p007",
        "label": "Pad 107",
        "design_center": {"x": 10.0, "y": 20.0},
        "stage_xy": {"x_mm": 1.0, "y_mm": 2.0},
        "route_offset_xy": {"dx_mm": 0.25, "dy_mm": -0.5},
        "adjusted_stage_xy": {"x_mm": 1.25, "y_mm": 1.5},
        "needle_contacts": [
            {"needle": 1, "design": {"x": 11.0, "y": 21.0}},
            {"needle": 2, "design": {"x": 9.0, "y": 19.0}},
        ],
    }


def _record() -> RouteMeasurementRecord:
    return RouteMeasurementRecord(
        timestamp="2026-06-25T12:00:00+00:00",
        structure_number=107,
        nplc="1",
        measurement_type="resistance",
        n_measurements=5,
        resistance_ohm=123.4,
        resistance_rms_ohm=0.5,
        relative_rms=0.004,
        status="good",
        contact_quality=RouteContactQuality(
            assessed=True,
            good=True,
            status="good",
            median_ohm=123.4,
            mad_sigma_ohm=0.1,
            p95_abs_step_ohm=0.2,
            span_ohm=0.3,
            compliance_hits=0,
            polarity_sign_mismatch_count=0,
            reasons=(),
            failure_criteria=(),
        ),
        raw_samples=(
            RouteMeasurementSample(
                sample_index=1,
                differential_resistance_ohm=123.4,
                compliance_hit=False,
            ),
        ),
    )


def _result(
    *,
    success: bool,
    message: str,
    contact_seek: RouteContactSeekResult | None = None,
) -> RouteContactPlacementResult:
    point = RouteMeasurementPoint(
        index=7,
        point_id="p007",
        label="Pad 107",
        design_center=(10.0, 20.0),
        stage_xy=(1.0, 2.0),
        needle_1_design=(11.0, 21.0),
        needle_2_design=(9.0, 19.0),
    )
    return RouteContactPlacementResult(
        success=success,
        message=message,
        point=point,
        record=_record(),
        contact_seek=contact_seek,
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"contact_number": 5}, 5),
        ({"contact": "6"}, 6),
        ({"point_number": 7.4}, 7),
        ({"structure_number": 8}, 8),
        ({}, None),
        ({"contact_number": 0}, None),
        ({"contact_number": -1}, None),
        ({"contact_number": "bad"}, None),
    ],
)
def test_api_contact_number_from_payload_accepts_aliases_and_rejects_invalid_values(
    payload: dict[str, object],
    expected: int | None,
) -> None:
    assert api_contact_number_from_payload(payload) == expected


def test_api_current_contact_settings_apply_defaults() -> None:
    settings = api_current_contact_settings_from_payload(
        {},
        default_check_sample_count=RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT,
        default_contact_seek_range_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
        default_contact_seek_step_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM,
        default_contact_settle_s=RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S,
    )

    assert settings == ApiCurrentContactSettings(
        check_sample_count=RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT,
        measurement_count=RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT,
        contact_seek_range_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
        contact_seek_step_mm=abs(RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM),
        contact_settle_s=RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S,
        contact_quality_limits=RouteContactQualityLimits(),
        max_relative_rms=None,
    )


def test_api_current_contact_settings_accept_aliases() -> None:
    settings = api_current_contact_settings_from_payload(
        {
            "initial_samples": 3,
            "samples": 6,
            "seek_range_mm": 0.07,
            "seek_step_mm": 0.003,
            "settle_s": 0.2,
            "contact_quality": {
                "max_mad_sigma_ohm": 200.0,
                "max_p95_abs_step_ohm": 300.0,
                "max_relative_mad_sigma": 0.03,
                "max_relative_p95_abs_step": 0.04,
            },
            "max_relative_rms_percent": 1.5,
        },
        default_check_sample_count=2,
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=-0.002,
        default_contact_settle_s=0.4,
    )

    assert settings.check_sample_count == 3
    assert settings.measurement_count == 6
    assert settings.contact_seek_range_mm == 0.07
    assert settings.contact_seek_step_mm == 0.003
    assert settings.contact_settle_s == 0.2
    assert settings.max_relative_rms == pytest.approx(0.015)
    assert settings.contact_quality_limits.as_dict() == {
        "max_mad_sigma_ohm": 200.0,
        "max_p95_abs_step_ohm": 300.0,
        "max_relative_mad_sigma": 0.03,
        "max_relative_p95_abs_step": 0.04,
    }


def test_api_current_contact_settings_require_measurement_count_at_least_check_count() -> None:
    with pytest.raises(ValueError, match="measurement_count must be at least 5"):
        api_current_contact_settings_from_payload(
            {
                "check_sample_count": 5,
                "measurement_count": 4,
            },
            default_check_sample_count=2,
            default_contact_seek_range_mm=0.05,
            default_contact_seek_step_mm=0.002,
            default_contact_settle_s=0.4,
        )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, None),
        ({"max_rel_rms": 0.02}, 0.02),
        ({"max_relative_rms_percent": 2.5}, pytest.approx(0.025)),
    ],
)
def test_api_current_contact_settings_parse_max_relative_rms_aliases(
    payload: dict[str, object],
    expected: object,
) -> None:
    settings = api_current_contact_settings_from_payload(
        payload,
        default_check_sample_count=2,
        default_contact_seek_range_mm=0.05,
        default_contact_seek_step_mm=0.002,
        default_contact_settle_s=0.4,
    )

    assert settings.max_relative_rms == expected


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"measurement_count": "bad"}, "Invalid integer value for measurement_count."),
        ({"contact_seek_step_mm": -0.1}, "contact_seek_step_mm must be at least 0.0."),
        ({"max_relative_rms_percent": -1}, "max_relative_rms_percent must be at least 0.0."),
        ({"contact_quality": "bad"}, "contact_quality must be an object."),
    ],
)
def test_api_current_contact_settings_raise_existing_parser_errors(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        api_current_contact_settings_from_payload(
            payload,
            default_check_sample_count=2,
            default_contact_seek_range_mm=0.05,
            default_contact_seek_step_mm=0.002,
            default_contact_settle_s=0.4,
        )


def test_api_current_contact_response_formats_check_success() -> None:
    settings = ApiCurrentContactSettings(
        check_sample_count=3,
        measurement_count=5,
        contact_seek_range_mm=0.01,
        contact_seek_step_mm=0.001,
        contact_settle_s=0.2,
        contact_quality_limits=RouteContactQualityLimits(
            max_mad_sigma_ohm=200.0,
            max_p95_abs_step_ohm=300.0,
            max_relative_mad_sigma=0.03,
            max_relative_p95_abs_step=0.04,
        ),
        max_relative_rms=0.02,
    )

    response = api_current_contact_response(
        _result(success=True, message="Contact check complete."),
        contact=_contact_payload(),
        settings=settings,
        needle_feedrate=7.5,
        timestamp_utc="2026-06-25T12:00:00+00:00",
        seek=False,
    )

    assert response["accepted"] is True
    assert response["message"] == "Contact check complete."
    assert response["timestamp_utc"] == "2026-06-25T12:00:00+00:00"
    assert response["contact"] == _contact_payload()
    assert response["needle_feedrate_mm_min"] == 7.5
    assert response["contact_ok"] is True
    assert response["check_sample_count"] == 3
    assert response["measurement_count"] == 5
    assert response["contact_settle_s"] == 0.2
    assert response["contact_seek_range_mm"] == 0.01
    assert response["contact_seek_step_mm"] == 0.001
    assert response["contact_quality_limits"] == {
        "max_mad_sigma_ohm": 200.0,
        "max_p95_abs_step_ohm": 300.0,
        "max_relative_mad_sigma": 0.03,
        "max_relative_p95_abs_step": 0.04,
    }
    assert response["measurement"]["structure_number"] == 107
    assert response["contact_seek"] is None
    assert "contact_found" not in response


def test_api_current_contact_response_formats_seek_success_with_contact_found() -> None:
    settings = ApiCurrentContactSettings(
        check_sample_count=2,
        measurement_count=2,
        contact_seek_range_mm=0.02,
        contact_seek_step_mm=0.001,
        contact_settle_s=0.1,
        contact_quality_limits=RouteContactQualityLimits(),
        max_relative_rms=None,
    )
    seek_result = RouteContactSeekResult(
        found=True,
        status="found",
        attempts=2,
        initial_status="bad_contact",
        final_status="good",
        depth_below_down_mm=0.001,
        axis_a_lowering_mm=1.001,
        step_mm=0.001,
        max_depth_mm=0.002,
    )

    response = api_current_contact_response(
        _result(
            success=False,
            message="Contact seek recovered contact.",
            contact_seek=seek_result,
        ),
        contact=_contact_payload(),
        settings=settings,
        needle_feedrate=None,
        timestamp_utc="2026-06-25T12:05:00+00:00",
        seek=True,
    )

    assert response["accepted"] is True
    assert response["contact_ok"] is False
    assert response["contact_found"] is True
    assert response["contact_seek"] == {
        "found": True,
        "status": "found",
        "attempts": 2,
        "initial_status": "bad_contact",
        "final_status": "good",
        "depth_below_down_mm": 0.001,
        "axis_a_lowering_mm": 1.001,
        "step_mm": 0.001,
        "max_depth_mm": 0.002,
    }


def test_api_current_contact_failure_alert_is_absent_for_success_or_disabled_notifications() -> None:
    failure_result = _result(
        success=False,
        message="Contact seek could not find contact.",
        contact_seek=RouteContactSeekResult(
            found=False,
            status="not_found",
            attempts=3,
            initial_status="bad_contact",
            final_status="bad_contact",
            depth_below_down_mm=0.002,
        ),
    )

    assert (
        api_current_contact_failure_alert(
            {"telegram_on_failure": True},
            contact_number=7,
            result=_result(success=True, message="ok"),
            reply_markup="markup",
        )
        is None
    )
    assert (
        api_current_contact_failure_alert(
            {},
            contact_number=7,
            result=failure_result,
            reply_markup="markup",
        )
        is None
    )


def test_api_current_contact_failure_alert_uses_exact_route_attention_message() -> None:
    alert = api_current_contact_failure_alert(
        {"notify_on_failure": True},
        contact_number=7,
        result=_result(
            success=False,
            message="Contact seek could not find contact.",
            contact_seek=RouteContactSeekResult(
                found=False,
                status="not_found",
                attempts=3,
                initial_status="bad_contact",
                final_status="bad_contact",
                depth_below_down_mm=0.002,
            ),
        ),
        reply_markup=SimpleNamespace(name="actions"),
    )

    assert alert == ApiCurrentContactFailureAlert(
        channel="route_attention",
        text=(
            "Probe route needs attention:\n"
            "Contact seek failed for contact 7: "
            "Contact seek could not find contact."
        ),
        attach_photo=True,
        reply_markup=SimpleNamespace(name="actions"),
    )
