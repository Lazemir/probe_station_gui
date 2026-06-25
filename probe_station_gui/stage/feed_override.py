"""FluidNC realtime feed-override payload helpers."""

from __future__ import annotations


def clamp_feed_override_percent(
    percent: int | float,
    *,
    min_percent: int,
    max_percent: int,
) -> int:
    return min(
        max(int(round(percent)), int(min_percent)),
        int(max_percent),
    )


def feed_override_percent_for_feedrates(
    programmed_feedrate: float,
    target_feedrate: float,
    *,
    min_feedrate: float,
    min_percent: int,
    max_percent: int,
) -> int:
    programmed = max(float(min_feedrate), float(programmed_feedrate))
    target = max(float(min_feedrate), float(target_feedrate))
    percent = int(round((target / programmed) * 100.0))
    return clamp_feed_override_percent(
        percent,
        min_percent=min_percent,
        max_percent=max_percent,
    )


def feed_override_payload_for_percent_change(
    current_percent: int,
    target_percent: int,
    *,
    reset_first: bool = False,
    min_percent: int,
    max_percent: int,
    reset_payload: bytes,
    plus_10_payload: bytes,
    minus_10_payload: bytes,
    plus_1_payload: bytes,
    minus_1_payload: bytes,
) -> tuple[bytes, int]:
    current = clamp_feed_override_percent(
        current_percent,
        min_percent=min_percent,
        max_percent=max_percent,
    )
    target = clamp_feed_override_percent(
        target_percent,
        min_percent=min_percent,
        max_percent=max_percent,
    )
    payload = bytearray()
    if reset_first:
        payload.extend(reset_payload)
        current = 100
    delta = target - current
    if delta > 0:
        tens, ones = divmod(delta, 10)
        payload.extend(plus_10_payload * tens)
        payload.extend(plus_1_payload * ones)
    elif delta < 0:
        tens, ones = divmod(abs(delta), 10)
        payload.extend(minus_10_payload * tens)
        payload.extend(minus_1_payload * ones)
    return (bytes(payload), target)
