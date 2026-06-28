"""Contact-seek configuration, status, and depth planning helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from probe_station_gui.route.formatting import format_route_ohm


DEFAULT_MANUAL_CONTACT_SEEK_STEP_MM = -0.001
DEFAULT_MANUAL_CONTACT_SEEK_MAX_TOTAL_MM = 0.020
DEFAULT_MANUAL_CONTACT_SEEK_QUICK_COUNT = 25
DEFAULT_MANUAL_CONTACT_SEEK_CONFIRM_COUNT = 250


@dataclass(frozen=True)
class ContactSeekStartDecision:
    accepted: bool
    status_message: str = ""
    calibration_window_result: str | None = None


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


def contact_seek_start_decision(
    *,
    contact_seek_active: bool,
    route_measurement_active: bool,
    instrument_connected: bool,
) -> ContactSeekStartDecision:
    if contact_seek_active:
        return ContactSeekStartDecision(
            accepted=False,
            status_message="Contact seek is already running.",
        )
    if route_measurement_active:
        return ContactSeekStartDecision(
            accepted=False,
            status_message="Stop route measurement before contact seek.",
        )
    if not instrument_connected:
        return ContactSeekStartDecision(
            accepted=False,
            status_message="Connect the measurement instrument before contact seek.",
            calibration_window_result="Measurement instrument is not connected.",
        )
    return ContactSeekStartDecision(accepted=True)


def contact_seek_current_position_status(quality: Any) -> str:
    return (
        "Contact seek: current position "
        f"{quality.status}, median={format_route_ohm(quality.median_ohm)}."
    )


def contact_seek_lowering_status(attempt: ContactSeekAttempt) -> str:
    return (
        "Contact seek: lowering A "
        f"{attempt.attempt_number}/{attempt.max_attempts}."
    )


def contact_seek_depth_status(moved_mm: float, quality: Any) -> str:
    return (
        "Contact seek: "
        f"{moved_mm:.4f} mm down, {quality.status}, "
        f"median={format_route_ohm(quality.median_ohm)}, "
        f"MAD={format_route_ohm(quality.mad_sigma_ohm)}."
    )


def contact_seek_confirming_status(confirm_count: int) -> str:
    return (
        "Contact seek: confirming stable contact with "
        f"{int(confirm_count)} readings."
    )


def contact_seek_confirmation_failed_status(quality: Any) -> str:
    return (
        "Contact seek: quick check was good, confirmation failed "
        f"({quality.status})."
    )


def contact_seek_found_detail(label: str, moved_mm: float, quality: Any) -> str:
    return (
        f"{label}; moved {moved_mm:.4f} mm; "
        f"median={format_route_ohm(quality.median_ohm)}, "
        f"MAD={format_route_ohm(quality.mad_sigma_ohm)}, "
        f"p95 step={format_route_ohm(quality.p95_abs_step_ohm)}."
    )


def contact_seek_found_message(detail: str) -> str:
    return f"Contact seek found stable contact: {detail}"


def contact_seek_cancelled_message() -> str:
    return "Contact seek cancelled."


def contact_seek_not_found_message(max_total_mm: float) -> str:
    return (
        "Contact seek did not find a stable contact within "
        f"{float(max_total_mm):.3f} mm."
    )


def contact_seek_failed_message(exc: BaseException) -> str:
    return f"Contact seek failed: {exc}"


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
