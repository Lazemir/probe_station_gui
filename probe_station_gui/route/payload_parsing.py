"""Small typed parsers for route API payload values."""

from __future__ import annotations

import math


def payload_bool(
    payload: dict[str, object],
    *keys: str,
    default: bool,
) -> bool:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "no", "n", "off"}:
                return False
    return default


def payload_float(
    payload: dict[str, object],
    *keys: str,
    default: float,
    minimum: float | None = None,
) -> float:
    value: object = default
    for key in keys:
        if key in payload and payload.get(key) is not None:
            value = payload.get(key)
            break
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric value for {keys[0]}.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"Invalid numeric value for {keys[0]}.")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{keys[0]} must be at least {minimum}.")
    return parsed


def payload_int(
    payload: dict[str, object],
    *keys: str,
    default: int,
    minimum: int | None = None,
) -> int:
    value: object = default
    for key in keys:
        if key in payload and payload.get(key) is not None:
            value = payload.get(key)
            break
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer value for {keys[0]}.") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{keys[0]} must be at least {minimum}.")
    return parsed


def payload_optional_float(
    payload: dict[str, object],
    *keys: str,
    minimum: float | None = None,
) -> float | None:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload_float(
                payload,
                key,
                default=0.0,
                minimum=minimum,
            )
    return None


__all__ = [
    "payload_bool",
    "payload_float",
    "payload_int",
    "payload_optional_float",
]
