"""Sample load/unload workflow planning and stage execution helpers."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from probe_station_gui.settings.objective_config import normalize_objective_name
from probe_station_gui.stage.controller import StageControllerError


logger = logging.getLogger(__name__)

SAMPLE_LOAD_X_MM = 0.0
SAMPLE_LOAD_Y_MM = 0.0
SAMPLE_UNLOAD_X_MM = -32.0
SAMPLE_UNLOAD_Y_MM = 32.0


@dataclass(frozen=True)
class SampleStartDecision:
    accepted: bool
    status_message: str = ""


@dataclass(frozen=True)
class SampleAutofocusPrompt:
    should_prompt: bool
    prompt: str = ""


def latest_stage_z(latest_position: object) -> float | None:
    if latest_position is None:
        return None
    try:
        position = tuple(latest_position)
    except TypeError:
        return None
    if len(position) < 3:
        return None
    try:
        z_mm = float(position[2])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(z_mm):
        return None
    return z_mm


def active_sample_objective_name(raw_name: object) -> str:
    name = normalize_objective_name(raw_name)
    if name:
        return name
    fallback = str(raw_name or "").strip().upper()
    return fallback or "UNKNOWN"


def remember_sample_focus(
    focus_by_objective: dict[str, float],
    *,
    raw_objective_name: object,
    latest_position: object,
) -> float | None:
    z_mm = latest_stage_z(latest_position)
    if z_mm is not None:
        focus_by_objective[active_sample_objective_name(raw_objective_name)] = z_mm
    return z_mm


def sample_load_focus_z(
    focus_by_objective: dict[str, float],
    *,
    raw_objective_name: object,
    latest_position: object,
) -> float | None:
    objective_name = active_sample_objective_name(raw_objective_name)
    if objective_name in focus_by_objective:
        return focus_by_objective[objective_name]
    return latest_stage_z(latest_position)


def sample_start_decision(
    action: str,
    *,
    stage_ready: bool,
    sample_active: bool,
    cancelable_operation: bool,
) -> SampleStartDecision:
    if not stage_ready:
        return SampleStartDecision(
            accepted=False,
            status_message="Stage is not connected; sample action not started.",
        )
    if sample_active or cancelable_operation:
        return SampleStartDecision(
            accepted=False,
            status_message=f"Stage is busy. Ignoring sample {action} request.",
        )
    return SampleStartDecision(accepted=True)


def design_registration_is_active(design_session: object) -> bool:
    registration = getattr(design_session, "registration", None)
    return bool(registration is not None and getattr(registration, "valid", False))


def sample_unload_move_status(
    x_mm: float = SAMPLE_UNLOAD_X_MM,
    y_mm: float = SAMPLE_UNLOAD_Y_MM,
) -> str:
    return (
        "Sample unload: moving to "
        f"X={float(x_mm):.3f}, Y={float(y_mm):.3f}."
    )


def sample_unload_success_message(
    x_mm: float = SAMPLE_UNLOAD_X_MM,
    y_mm: float = SAMPLE_UNLOAD_Y_MM,
) -> str:
    return (
        "Sample unloaded at "
        f"X={float(x_mm):.3f}, "
        f"Y={float(y_mm):.3f}; needles are raised."
    )


def sample_load_move_status(
    x_mm: float = SAMPLE_LOAD_X_MM,
    y_mm: float = SAMPLE_LOAD_Y_MM,
) -> str:
    return (
        "Sample load: moving to "
        f"X={float(x_mm):.3f}, Y={float(y_mm):.3f}."
    )


def sample_load_focus_status(focus_z_mm: float) -> str:
    return f"Sample load: moving Z to last focus {focus_z_mm:.4f} mm."


def sample_load_success_message(
    focus_z_mm: float | None,
    *,
    x_mm: float = SAMPLE_LOAD_X_MM,
    y_mm: float = SAMPLE_LOAD_Y_MM,
) -> str:
    if focus_z_mm is not None:
        return (
            "Sample loaded at "
            f"X={float(x_mm):.3f}, "
            f"Y={float(y_mm):.3f}, "
            f"Z={focus_z_mm:.4f}."
        )
    return (
        "Sample loaded at "
        f"X={float(x_mm):.3f}, "
        f"Y={float(y_mm):.3f}; last focus is unavailable."
    )


def sample_autofocus_prompt(
    *,
    success: bool,
    offer_autofocus: bool,
    focus_z_mm: object,
) -> SampleAutofocusPrompt:
    if not success or not offer_autofocus:
        return SampleAutofocusPrompt(should_prompt=False)
    prompt = "Run autofocus now?"
    try:
        focus_z = float(focus_z_mm)
    except (TypeError, ValueError):
        focus_z = math.nan
    if math.isfinite(focus_z):
        prompt = f"Sample is near Z={focus_z:.4f} mm. Run autofocus now?"
    return SampleAutofocusPrompt(should_prompt=True, prompt=prompt)


def run_sample_unload(
    stage_controller: Any,
    *,
    xy_feedrate: float,
    needle_feedrate: float,
    emit_status,
    emit_finished,
    unload_x_mm: float = SAMPLE_UNLOAD_X_MM,
    unload_y_mm: float = SAMPLE_UNLOAD_Y_MM,
) -> None:
    success = False
    message = ""
    try:
        stage_controller.begin_external_task("sample unload")
        emit_status("Sample unload: raising needles.")
        stage_controller.run_external_needles_action("raise", needle_feedrate)
        emit_status(sample_unload_move_status(unload_x_mm, unload_y_mm))
        stage_controller.run_external_move_to_xy(
            unload_x_mm,
            unload_y_mm,
            feedrate=xy_feedrate,
        )
        message = sample_unload_success_message(unload_x_mm, unload_y_mm)
        success = True
    except StageControllerError as exc:
        message = f"Sample unload failed: {exc}"
    except Exception as exc:
        logger.exception("Sample unload failed.")
        message = f"Sample unload failed: {exc}"
    finally:
        stage_controller.finish_external_task()
        emit_finished(success, message, False, None)


def run_sample_load(
    stage_controller: Any,
    *,
    focus_z_mm: float | None,
    xy_feedrate: float,
    focus_feedrate: float,
    needle_feedrate: float,
    emit_status,
    emit_finished,
    load_x_mm: float = SAMPLE_LOAD_X_MM,
    load_y_mm: float = SAMPLE_LOAD_Y_MM,
) -> None:
    success = False
    message = ""
    try:
        stage_controller.begin_external_task("sample load")
        emit_status("Sample load: raising needles.")
        stage_controller.run_external_needles_action("raise", needle_feedrate)
        emit_status(sample_load_move_status(load_x_mm, load_y_mm))
        stage_controller.run_external_move_to_xy(
            load_x_mm,
            load_y_mm,
            feedrate=xy_feedrate,
        )
        if focus_z_mm is not None:
            emit_status(sample_load_focus_status(focus_z_mm))
            stage_controller.run_external_absolute_axis_targets_move(
                {"Z": focus_z_mm},
                feedrate=focus_feedrate,
            )
        message = sample_load_success_message(
            focus_z_mm,
            x_mm=load_x_mm,
            y_mm=load_y_mm,
        )
        success = True
    except StageControllerError as exc:
        message = f"Sample load failed: {exc}"
    except Exception as exc:
        logger.exception("Sample load failed.")
        message = f"Sample load failed: {exc}"
    finally:
        stage_controller.finish_external_task()
        emit_finished(success, message, success, focus_z_mm)
