import pytest

from probe_station_gui.route.session_start import (
    DEFAULT_EXTERNAL_SESSION_FOLLOWUP_MEASUREMENT_COUNT,
    DEFAULT_EXTERNAL_SESSION_INITIAL_MEASUREMENT_COUNT,
    DEFAULT_EXTERNAL_SESSION_PHOTO_FOCUS_RANGE_MM,
    DEFAULT_EXTERNAL_SESSION_PHOTO_SETTLE_S,
    route_contact_quality_limits_from_payload,
    route_external_session_start_settings_from_payload,
    route_max_relative_rms_from_payload,
)


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
