"""Needle and surface calibration position planning helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


VALID_NEEDLE_TARGET_ACTIONS = frozenset({"raise", "lower"})
VALID_SURFACE_TARGETS = frozenset({"chip", "stone"})


@dataclass(frozen=True)
class NeedleTargetSavePlan:
    action_key: str
    lowering_mm: float
    display_a: float | None
    status_message: str


@dataclass(frozen=True)
class SurfacePositionSavePlan:
    target_key: str
    x_mm: float
    y_mm: float
    z_mm: float
    status_message: str


@dataclass(frozen=True)
class SurfaceMovePlan:
    target_key: str
    x_mm: float
    y_mm: float
    z_mm: float
    transit_z_mm: float
    label: str


def a_position_failure_status(reason: object) -> str:
    reason_text = str(reason or "unknown reason")
    if "stage task is active" in reason_text:
        status_reason = "stage is busy"
    elif "serial connection" in reason_text:
        status_reason = "serial connection is unavailable"
    elif "status query" in reason_text:
        status_reason = "controller status was unavailable"
    elif "does not include A axis" in reason_text:
        status_reason = "controller status did not include A"
    else:
        status_reason = "see log for details"
    return f"Unable to read A position: {status_reason}."


def finite_display_a(value: object) -> float | None:
    try:
        display_a = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(display_a):
        return None
    return display_a


def finite_raw_a(value: object) -> float | None:
    try:
        raw_a = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(raw_a):
        return None
    return raw_a


def normalise_needle_action(action: object) -> str | None:
    action_key = str(action).strip().lower()
    if action_key not in VALID_NEEDLE_TARGET_ACTIONS:
        return None
    return action_key


def needle_down_from_lowering_plan(lowering_mm: object) -> NeedleTargetSavePlan:
    lowering = max(0.0, float(lowering_mm))
    return NeedleTargetSavePlan(
        action_key="lower",
        lowering_mm=lowering,
        display_a=None,
        status_message="Saved needle down target and set current A position to A0.",
    )


def needle_target_save_plan(
    action: object,
    raw_a: object,
    *,
    lowering_for_raw_a,
    display_a_for_lowering,
) -> NeedleTargetSavePlan | str:
    action_key = normalise_needle_action(action)
    if action_key is None:
        return f"Unknown needle target '{action}'."
    raw_value = finite_raw_a(raw_a)
    if raw_value is None:
        return "Invalid A coordinate."
    lowering_mm = float(lowering_for_raw_a(raw_value))
    display_a = display_a_for_lowering(lowering_mm)
    return NeedleTargetSavePlan(
        action_key=action_key,
        lowering_mm=lowering_mm,
        display_a=display_a,
        status_message=(
            f"Saved needle {action_key} target "
            f"A={display_a:.4f} ({lowering_mm:.4f} mm lowering)."
        ),
    )


def apply_needle_target(settings: Any, plan: NeedleTargetSavePlan) -> None:
    if plan.action_key == "raise":
        settings.raise_position_mm = plan.lowering_mm
        settings.raise_position_configured = True
        return
    settings.down_position_mm = plan.lowering_mm
    settings.down_position_configured = True


def normalise_surface_target(target: object) -> str | None:
    target_key = str(target).strip().lower()
    if target_key not in VALID_SURFACE_TARGETS:
        return None
    return target_key


def surface_target_error(target: object) -> str:
    return f"Unknown calibration position '{target}'."


def surface_position_save_plan(
    target: object,
    current_position: object,
) -> SurfacePositionSavePlan | str:
    target_key = normalise_surface_target(target)
    if target_key is None:
        return surface_target_error(target)
    try:
        position = tuple(current_position)
    except TypeError:
        return "Controller did not report X/Y/Z coordinates."
    if len(position) < 3:
        return "Controller did not report X/Y/Z coordinates."
    x_mm = float(position[0])
    y_mm = float(position[1])
    z_mm = float(position[2])
    return SurfacePositionSavePlan(
        target_key=target_key,
        x_mm=x_mm,
        y_mm=y_mm,
        z_mm=z_mm,
        status_message=(
            f"Saved {target_key} focus at "
            f"X={x_mm:.4f}, Y={y_mm:.4f}, Z={z_mm:.4f} mm."
        ),
    )


def apply_surface_position(settings: Any, plan: SurfacePositionSavePlan) -> Any:
    saved_position = (
        settings.chip_position
        if plan.target_key == "chip"
        else settings.stone_position
    )
    saved_position.x_mm = plan.x_mm
    saved_position.y_mm = plan.y_mm
    saved_position.z_mm = plan.z_mm
    saved_position.configured = True
    return saved_position


def surface_move_plan(target: object, settings: Any) -> SurfaceMovePlan | str:
    target_key = normalise_surface_target(target)
    if target_key is None:
        return surface_target_error(target)
    destination = (
        settings.chip_position if target_key == "chip" else settings.stone_position
    )
    other = settings.stone_position if target_key == "chip" else settings.chip_position
    if not destination.configured:
        return f"Save the {target_key} focus position first."
    transit_z = destination.z_mm
    if other.configured:
        transit_z = min(destination.z_mm, other.z_mm)
    return SurfaceMovePlan(
        target_key=target_key,
        x_mm=float(destination.x_mm),
        y_mm=float(destination.y_mm),
        z_mm=float(destination.z_mm),
        transit_z_mm=float(transit_z),
        label=f"{target_key} position",
    )
