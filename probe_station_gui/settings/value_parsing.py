"""Small value normalisers shared by settings parsers."""

from __future__ import annotations

import math


def coerce_bool(value, *, default: bool) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no"}
    if value is None:
        return default
    return bool(value)


def coerce_float(value, *, default: float) -> float:
    try:
        if isinstance(value, (int, float, str)):
            return float(value)
    except (TypeError, ValueError):
        pass
    return default


def coerce_int(value, *, default: int) -> int:
    try:
        if isinstance(value, (int, float, str)):
            return int(float(value))
    except (TypeError, ValueError):
        pass
    return default


def finite_float(value, *, default: float) -> float:
    result = coerce_float(value, default=default)
    if not math.isfinite(result):
        return default
    return result


def positive_float(value, *, default: float) -> float:
    result = finite_float(value, default=default)
    if result <= 0.0:
        return default
    return result


def normalise_choice(value, *, choices: tuple, default: str) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        for choice in choices:
            if candidate.upper() == str(choice).upper():
                return str(choice)
    return default
