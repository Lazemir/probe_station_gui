"""Contact-seek configuration and depth planning helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ContactSeekAttempt:
    previous_depth_mm: float
    depth_mm: float
    attempt_number: int
    max_attempts: int

    def adjust_delta_mm(self, step_mm: float) -> float:
        return math.copysign(
            self.depth_mm - self.previous_depth_mm,
            float(step_mm),
        )


def normalize_contact_seek_step(value: object, *, default_step_mm: float) -> float:
    try:
        step = float(value)
    except (TypeError, ValueError):
        step = float(default_step_mm)
    if not math.isfinite(step) or step == 0.0:
        step = float(default_step_mm)
    return -abs(step)


def normalize_contact_seek_limit(value: object, *, default_limit_mm: float) -> float:
    try:
        limit = float(value)
    except (TypeError, ValueError):
        limit = float(default_limit_mm)
    if not math.isfinite(limit) or limit < 0.0:
        return 0.0
    return limit


def contact_seek_depths(step_mm: float, max_total_mm: float) -> tuple[float, ...]:
    step = abs(float(step_mm))
    max_total = max(0.0, float(max_total_mm))
    if step <= 0.0 or not math.isfinite(step) or not math.isfinite(max_total):
        return ()
    max_depth_steps = int(math.ceil(max_total / step))
    return tuple(
        min((step_index + 1) * step, max_total)
        for step_index in range(max_depth_steps)
    )


def contact_seek_attempt_number(depth_mm: float, step_mm: float) -> int:
    step = abs(float(step_mm))
    if step <= 0.0 or not math.isfinite(step):
        return 1
    return max(1, int(math.ceil(float(depth_mm) / step)))


def contact_seek_attempts(
    step_mm: float,
    max_total_mm: float,
) -> tuple[ContactSeekAttempt, ...]:
    depths = contact_seek_depths(step_mm, max_total_mm)
    max_attempts = len(depths)
    previous_depth_mm = 0.0
    attempts: list[ContactSeekAttempt] = []
    for depth_mm in depths:
        attempts.append(
            ContactSeekAttempt(
                previous_depth_mm=previous_depth_mm,
                depth_mm=depth_mm,
                attempt_number=contact_seek_attempt_number(depth_mm, step_mm),
                max_attempts=max_attempts,
            )
        )
        previous_depth_mm = float(depth_mm)
    return tuple(attempts)
