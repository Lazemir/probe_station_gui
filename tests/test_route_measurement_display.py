import math
from types import SimpleNamespace

from probe_station_gui.route_measurement_display import (
    axis_tick_decimals,
    count_axis_ticks,
    histogram_counts,
    parse_tqdm_interval,
    raw_data_rows,
    resistance_axis_unit,
    resistance_x_axis_label,
    sample_values,
)


def test_sample_values_filters_missing_and_nonfinite_values() -> None:
    samples = (
        SimpleNamespace(value=1.0),
        SimpleNamespace(value=float("nan")),
        SimpleNamespace(other=2.0),
    )

    assert sample_values(samples, "value") == [1.0]


def test_histogram_counts_clamps_boundary_values() -> None:
    assert histogram_counts([0.0, 0.1, 0.9, 1.0], 2, 0.0, 1.0) == [2, 2]
    assert histogram_counts([0.0], 2, 1.0, 1.0) == [0, 0]


def test_resistance_axis_unit_uses_largest_finite_magnitude() -> None:
    assert resistance_axis_unit([1_200.0, math.nan]) == (1e3, "kOhm")
    assert resistance_axis_unit([]) == (1.0, "Ohm")


def test_axis_tick_decimals_and_count_ticks() -> None:
    assert axis_tick_decimals(100.0) == 0
    assert axis_tick_decimals(0.001) == 5
    assert axis_tick_decimals(float("nan")) == 3
    assert count_axis_ticks(1) == [0, 1]
    assert count_axis_ticks(7) == [0, 3, 7]


def test_tqdm_interval_parser_and_axis_label_helpers() -> None:
    assert parse_tqdm_interval("01:02") == 62.0
    assert parse_tqdm_interval("1:02:03") == 3723.0
    assert parse_tqdm_interval("2 days, 1:02:03") == 176523.0
    assert parse_tqdm_interval("?") is None
    assert parse_tqdm_interval("bad") is None
    assert resistance_x_axis_label("polarity", "kOhm") == "V/I resistance (kOhm)"
    assert resistance_x_axis_label("differential", "Ohm") == (
        "dV/dI resistance (Ohm)"
    )


def test_raw_data_rows_expands_polarity_samples_and_formats_numbers() -> None:
    samples = (
        SimpleNamespace(
            sample_index=1,
            differential_resistance_ohm=123.456789,
            compliance_hit=True,
            negative_source_voltage_v=-0.03,
            negative_measured_voltage_v=-0.02,
            negative_current_a=1e-6,
            negative_resistance_ohm=20_000.0,
            positive_source_voltage_v=None,
            positive_measured_voltage_v=None,
            positive_current_a=None,
            positive_resistance_ohm=None,
        ),
        SimpleNamespace(
            sample_index=2,
            differential_resistance_ohm=math.nan,
            compliance_hit=False,
        ),
    )

    assert raw_data_rows(samples) == [
        [
            "1",
            "negative",
            "-0.03",
            "-0.02",
            "1e-06",
            "20000",
            "123.456789",
            "yes",
        ],
        ["2", "differential", "", "", "", "", "", ""],
    ]
