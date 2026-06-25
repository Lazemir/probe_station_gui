"""Formatting helpers for route measurement records and status text."""

from __future__ import annotations

import math


def csv_float(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return f"{number:.12g}"


def csv_bool(value: object) -> str:
    return "true" if bool(value) else "false"


def format_route_ohm(value: float) -> str:
    if not math.isfinite(value):
        return "nan Ohm"
    abs_value = abs(value)
    for scale, unit in (
        (1e9, "GOhm"),
        (1e6, "MOhm"),
        (1e3, "kOhm"),
        (1.0, "Ohm"),
        (1e-3, "mOhm"),
        (1e-6, "uOhm"),
    ):
        if abs_value >= scale:
            return f"{value / scale:.3g} {unit}"
    return f"{value:.3g} Ohm"


def format_route_percent(value: float) -> str:
    if not math.isfinite(value):
        return "nan%"
    return f"{value * 100.0:.3g}%"
