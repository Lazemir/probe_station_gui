from probe_station_gui.route_contact_quality import (
    RouteContactQualityLimits,
    RouteMeasurementSample,
    route_measurement_sample_from_raw,
    summarize_route_contact_quality,
)


def test_contact_quality_thresholds_are_configurable_from_new_module() -> None:
    samples = [
        RouteMeasurementSample(index, value)
        for index, value in enumerate(
            (18_600.0, 19_800.0, 18_900.0, 20_100.0),
            start=1,
        )
    ]

    default_quality = summarize_route_contact_quality(samples)
    relaxed_quality = summarize_route_contact_quality(
        samples,
        contact_quality_limits=RouteContactQualityLimits(
            max_mad_sigma_ohm=2_000.0,
            max_p95_abs_step_ohm=3_000.0,
            max_relative_mad_sigma=0.20,
            max_relative_p95_abs_step=0.25,
        ),
    )

    assert default_quality.good is False
    assert "mad_sigma_too_high" in default_quality.reasons
    assert default_quality.failure_criteria
    assert relaxed_quality.good is True
    assert relaxed_quality.reasons == ()


def test_route_measurement_sample_from_raw_preserves_polarity_details() -> None:
    sample = route_measurement_sample_from_raw(
        {
            "differential_resistance_ohm": 123.0,
            "short_detected": True,
            "negative": {
                "source_voltage_v": -0.1,
                "measured_current_a": -2.0e-6,
            },
            "positive": {
                "bias_voltage_v": 0.1,
                "current_a": -5.0e-8,
            },
        },
        sample_index=7,
    )

    assert sample.sample_index == 7
    assert sample.differential_resistance_ohm == 123.0
    assert sample.compliance_hit is True
    assert sample.negative_source_voltage_v == -0.1
    assert sample.negative_current_a == -2.0e-6
    assert sample.positive_source_voltage_v == 0.1
    assert sample.positive_current_a == -5.0e-8
