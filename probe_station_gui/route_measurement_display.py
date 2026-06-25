"""Display-data helpers for route measurement dialogs."""

from __future__ import annotations

import math

from probe_station_gui.route_formatting import csv_float


def sample_values(samples: tuple[object, ...], attribute: str) -> list[float]:
    values: list[float] = []
    for sample in samples:
        try:
            value = float(getattr(sample, attribute))
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return values


def histogram_counts(
    values: list[float],
    bin_count: int,
    minimum: float,
    maximum: float,
) -> list[int]:
    counts = [0 for _index in range(bin_count)]
    span = maximum - minimum
    if span <= 0.0:
        return counts
    for value in values:
        index = int((value - minimum) / span * bin_count)
        index = max(0, min(bin_count - 1, index))
        counts[index] += 1
    return counts


def resistance_axis_unit(values: list[float]) -> tuple[float, str]:
    finite_values = [abs(value) for value in values if math.isfinite(value)]
    reference = max(finite_values, default=1.0)
    for scale, label in (
        (1e9, "GOhm"),
        (1e6, "MOhm"),
        (1e3, "kOhm"),
        (1.0, "Ohm"),
        (1e-3, "mOhm"),
        (1e-6, "uOhm"),
    ):
        if reference >= scale:
            return scale, label
    return 1.0, "Ohm"


def axis_tick_decimals(scaled_span: float) -> int:
    span = abs(float(scaled_span))
    if not math.isfinite(span) or span <= 0.0:
        return 3
    tick_step = span / 2.0
    decimals = int(math.ceil(-math.log10(tick_step))) + 1
    return max(0, min(6, decimals))


def count_axis_ticks(max_count: int) -> list[int]:
    if max_count <= 1:
        return [0, 1]
    middle = max(1, max_count // 2)
    return sorted({0, middle, max_count})


def raw_data_rows(samples: tuple[object, ...]) -> list[list[str]]:
    rows: list[list[str]] = []
    for sample in samples:
        sample_index = str(getattr(sample, "sample_index", ""))
        differential = csv_float(
            getattr(sample, "differential_resistance_ohm", math.nan)
        )
        compliance = "yes" if bool(getattr(sample, "compliance_hit", False)) else ""
        added = False
        for polarity in ("negative", "positive"):
            values = (
                getattr(sample, f"{polarity}_source_voltage_v", None),
                getattr(sample, f"{polarity}_measured_voltage_v", None),
                getattr(sample, f"{polarity}_current_a", None),
                getattr(sample, f"{polarity}_resistance_ohm", None),
            )
            if all(value is None for value in values):
                continue
            rows.append(
                [
                    sample_index,
                    polarity,
                    csv_float(values[0]),
                    csv_float(values[1]),
                    csv_float(values[2]),
                    csv_float(values[3]),
                    differential,
                    compliance,
                ]
            )
            added = True
        if not added:
            rows.append(
                [
                    sample_index,
                    "differential",
                    "",
                    "",
                    "",
                    "",
                    differential,
                    compliance,
                ]
            )
    return rows
