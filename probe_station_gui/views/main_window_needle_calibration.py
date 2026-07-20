"""Main-window needle calibration runtime synchronization."""

from __future__ import annotations

import logging

from probe_station_gui.route import contact_seek as manual_contact_seek
from probe_station_gui.stage import calibration_positions, sample_handling
from probe_station_gui.stage.controller import StageControllerError
from probe_station_gui.views import main_window_stage_position_panel as stage_position_panel


logger = logging.getLogger(__name__)


def contact_seek_step_mm(owner: object) -> float:
    return float(
        getattr(
            owner,
            "CONTACT_SEEK_STEP_MM",
            manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_STEP_MM,
        )
    )


def contact_seek_max_total_mm(owner: object) -> float:
    return float(
        getattr(
            owner,
            "CONTACT_SEEK_MAX_TOTAL_MM",
            manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_MAX_TOTAL_MM,
        )
    )


def contact_seek_quick_count(owner: object) -> int:
    return int(
        getattr(
            owner,
            "CONTACT_SEEK_QUICK_COUNT",
            manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_QUICK_COUNT,
        )
    )


def contact_seek_confirm_count(owner: object) -> int:
    return int(
        getattr(
            owner,
            "CONTACT_SEEK_CONFIRM_COUNT",
            manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_CONFIRM_COUNT,
        )
    )


def configured_lowering(settings: object, value_name: str, configured_name: str):
    return (
        getattr(settings, value_name)
        if bool(getattr(settings, configured_name))
        else None
    )


def apply_needle_calibration_runtime(
    owner: object,
    needle_settings: object,
) -> None:
    raise_lowering = configured_lowering(
        needle_settings,
        "raise_position_mm",
        "raise_position_configured",
    )
    down_lowering = configured_lowering(
        needle_settings,
        "down_position_mm",
        "down_position_configured",
    )
    owner.stage_controller.apply_needle_calibration(
        raise_position_mm=raise_lowering,
        down_position_mm=down_lowering,
        contact_zone_mm=needle_settings.contact_zone_mm,
    )
    joystick_panel = getattr(owner, "joystick_panel", None)
    if joystick_panel is not None:
        joystick_panel.set_needle_contact_coordinate(
            "raise",
            owner._display_a_for_needle_lowering(raise_lowering),
        )
        joystick_panel.set_needle_contact_coordinate(
            "lower",
            owner._display_a_for_needle_lowering(down_lowering),
        )
    contact_window = getattr(owner, "contact_calibration_window", None)
    if contact_window is not None:
        contact_window.set_saved_needle_height(down_lowering)
        contact_window.set_saved_surface_position(
            "chip",
            needle_settings.chip_position,
        )
        contact_window.set_saved_surface_position(
            "stone",
            needle_settings.stone_position,
        )


def save_needle_calibration_settings(owner: object, settings: object) -> None:
    owner.settings_manager.replace_and_save(
        settings,
        preserve_exposure_policy=True,
    )
    apply_needle_calibration_runtime(owner, settings.needle_calibration)


def save_current_needle_height(owner: object) -> None:
    a_position = owner.stage_controller.latest_a_position()
    latest_state = owner.stage_controller.latest_stage_state()
    if latest_state not in (None, "Idle"):
        owner._show_status("Wait for the stage to stop before saving needle contact.")
        return
    if a_position is None:
        a_position = owner.stage_controller.current_a_position()
    if a_position is None:
        reason = (
            owner.stage_controller.last_a_position_read_failure()
            or "unknown reason"
        )
        logger.warning("Unable to save needle down height: %s", reason)
        owner._show_status(calibration_positions.a_position_failure_status(reason))
        return
    latest_state = owner.stage_controller.latest_stage_state()
    if latest_state not in (None, "Idle"):
        owner._show_status("Wait for the stage to stop before saving needle contact.")
        return
    lowering_mm = owner.stage_controller.axis_a_lowering_for_configured_coordinate(
        a_position
    )
    try:
        owner.stage_controller.set_current_axis_work_coordinate("A", 0.0)
    except StageControllerError as exc:
        logger.warning("Unable to zero A work coordinate for needle contact: %s", exc)
        owner._show_status(f"Unable to set A0 at needle contact: {exc}")
        return
    save_needle_down_position_from_lowering(owner, lowering_mm)


def save_needle_position_from_display_a_coordinate(
    owner: object,
    action: str,
    a_coordinate: float,
) -> None:
    display_a = calibration_positions.finite_display_a(a_coordinate)
    if display_a is None:
        owner._show_status("Invalid A coordinate.")
        return
    raw_a = owner.stage_controller.calibrated_axis_raw_value("A", display_a)
    save_needle_position_from_raw_a_coordinate(owner, action, raw_a)


def save_needle_down_position_from_lowering(
    owner: object,
    lowering_mm: float,
) -> None:
    plan = calibration_positions.needle_down_from_lowering_plan(lowering_mm)
    settings = owner.settings_manager.settings.clone()
    calibration_positions.apply_needle_target(settings.needle_calibration, plan)
    save_needle_calibration_settings(owner, settings)
    owner._show_status(plan.status_message)


def save_needle_position_from_raw_a_coordinate(
    owner: object,
    action: str,
    a_coordinate: float,
) -> None:
    plan = calibration_positions.needle_target_save_plan(
        action,
        a_coordinate,
        lowering_for_raw_a=(
            owner.stage_controller.axis_a_lowering_for_configured_coordinate
        ),
        display_a_for_lowering=owner._display_a_for_needle_lowering,
    )
    if isinstance(plan, str):
        owner._show_status(plan)
        return
    settings = owner.settings_manager.settings.clone()
    calibration_positions.apply_needle_target(settings.needle_calibration, plan)
    save_needle_calibration_settings(owner, settings)
    owner._show_status(plan.status_message)


def save_surface_position(owner: object, target: str) -> None:
    if calibration_positions.normalise_surface_target(target) is None:
        owner._show_status(calibration_positions.surface_target_error(target))
        return
    try:
        current_position = owner.stage_controller.current_stage_position()
    except Exception as exc:
        owner._show_status(str(exc))
        return
    plan = calibration_positions.surface_position_save_plan(target, current_position)
    if isinstance(plan, str):
        owner._show_status(plan)
        return
    settings = owner.settings_manager.settings.clone()
    calibration_positions.apply_surface_position(settings.needle_calibration, plan)
    save_needle_calibration_settings(owner, settings)
    owner._show_status(plan.status_message)


def move_to_surface_position(owner: object, target: str) -> None:
    settings = owner.settings_manager.needle_calibration_configuration()
    plan = calibration_positions.surface_move_plan(target, settings)
    if isinstance(plan, str):
        owner._show_status(plan)
        return
    owner.stage_controller.request_move_to_xyz(
        plan.x_mm,
        plan.y_mm,
        plan.z_mm,
        plan.transit_z_mm,
        plan.label,
    )


def request_contact_seek(owner: object, *, thread_factory) -> None:
    thread = owner._contact_seek_thread
    route_thread = owner._route_measurement_thread
    decision = manual_contact_seek.contact_seek_start_decision(
        contact_seek_active=thread is not None and thread.is_alive(),
        route_measurement_active=(
            route_thread is not None and route_thread.is_alive()
        ),
        instrument_connected=owner.lcr_controller.is_connected(),
    )
    if not decision.accepted:
        owner._show_status(decision.status_message)
        contact_window = getattr(owner, "contact_calibration_window", None)
        if contact_window is not None and decision.calibration_window_result is not None:
            contact_window.set_contact_seek_result(decision.calibration_window_result)
        return
    owner._contact_seek_stop_requested.clear()
    contact_window = getattr(owner, "contact_calibration_window", None)
    if contact_window is not None:
        contact_window.set_contact_seek_running(True)
        contact_window.set_contact_seek_result("Starting.")
    thread = thread_factory(target=lambda: run_contact_seek(owner), daemon=True)
    owner._contact_seek_thread = thread
    thread.start()


def on_contact_seek_status(owner: object, message: str) -> None:
    contact_window = getattr(owner, "contact_calibration_window", None)
    if contact_window is not None:
        contact_window.set_contact_seek_result(message)
    owner._show_status(message, 5000)


def on_contact_seek_calibration_found(
    owner: object,
    lowering_mm: float,
    detail: str,
) -> None:
    save_needle_down_position_from_lowering(owner, float(lowering_mm))
    contact_window = getattr(owner, "contact_calibration_window", None)
    if contact_window is not None:
        contact_window.set_contact_seek_result(detail)


def on_contact_seek_finished(owner: object, success: bool, message: str) -> None:
    thread = owner._contact_seek_thread
    if thread is not None and not thread.is_alive():
        thread.join(timeout=0.1)
    owner._contact_seek_thread = None
    owner._resume_resistance_standby_polling()
    contact_window = getattr(owner, "contact_calibration_window", None)
    if contact_window is not None:
        contact_window.set_contact_seek_running(False)
        contact_window.set_contact_seek_result(message)
    owner._show_status(message, 8000 if not success else 5000)
    if not success:
        owner._send_telegram_alert(
            "contact_seek_failed",
            f"Contact seek needs attention:\n{message}",
            attach_photo=True,
        )


def run_contact_seek(owner: object) -> None:
    stage_reserved = False
    try:
        owner.stage_controller.begin_external_task("contact seek")
        stage_reserved = True
        feedrate = owner._current_needle_feedrate()
        step_mm = contact_seek_step_mm(owner)
        max_total_mm = contact_seek_max_total_mm(owner)
        quick_count = contact_seek_quick_count(owner)
        quick_quality = owner._contact_seek_measure_quality(quick_count)
        owner.contact_seek_status.emit(
            manual_contact_seek.contact_seek_current_position_status(quick_quality)
        )
        if quick_quality.good is True:
            if confirm_and_save_contact_seek(owner, "current position", 0.0):
                return

        for attempt in manual_contact_seek.contact_seek_attempts(
            step_mm,
            max_total_mm,
        ):
            if owner._contact_seek_stop_requested.is_set():
                owner.contact_seek_finished.emit(
                    False,
                    manual_contact_seek.contact_seek_cancelled_message(),
                )
                return
            owner.contact_seek_status.emit(
                manual_contact_seek.contact_seek_lowering_status(attempt)
            )
            owner.stage_controller.run_external_needles_adjust(
                attempt.adjust_delta_mm(step_mm),
                feedrate,
            )
            if owner._contact_seek_stop_requested.is_set():
                owner.contact_seek_finished.emit(
                    False,
                    manual_contact_seek.contact_seek_cancelled_message(),
                )
                return
            quick_quality = owner._contact_seek_measure_quality(quick_count)
            owner.contact_seek_status.emit(
                manual_contact_seek.contact_seek_depth_status(
                    attempt.depth_mm,
                    quick_quality,
                )
            )
            if quick_quality.good is True:
                if confirm_and_save_contact_seek(
                    owner,
                    f"{attempt.depth_mm:.4f} mm down",
                    attempt.depth_mm,
                ):
                    return
        owner.contact_seek_finished.emit(
            False,
            manual_contact_seek.contact_seek_not_found_message(max_total_mm),
        )
    except Exception as exc:
        logger.exception("Contact seek failed.")
        owner.contact_seek_finished.emit(
            False,
            manual_contact_seek.contact_seek_failed_message(exc),
        )
    finally:
        if stage_reserved:
            owner.stage_controller.finish_external_task()


def confirm_and_save_contact_seek(
    owner: object,
    label: str,
    moved_mm: float,
) -> bool:
    confirm_count = contact_seek_confirm_count(owner)
    owner.contact_seek_status.emit(
        manual_contact_seek.contact_seek_confirming_status(confirm_count)
    )
    confirm_quality = owner._contact_seek_measure_quality(confirm_count)
    if confirm_quality.good is not True:
        owner.contact_seek_status.emit(
            manual_contact_seek.contact_seek_confirmation_failed_status(
                confirm_quality
            )
        )
        return False
    lowering_mm = owner.stage_controller.latest_axis_a_lowering()
    if lowering_mm is None:
        raise StageControllerError("Unable to read A lowering after contact seek.")
    owner.stage_controller.finish_external_task()
    try:
        owner.stage_controller.set_current_axis_work_coordinate("A", 0.0)
    except StageControllerError:
        raise
    detail = manual_contact_seek.contact_seek_found_detail(
        label,
        moved_mm,
        confirm_quality,
    )
    owner.contact_seek_calibration_found.emit(float(lowering_mm), detail)
    owner.contact_seek_finished.emit(
        True,
        manual_contact_seek.contact_seek_found_message(detail),
    )
    return True


def request_sample_unload(owner: object, *, message_box, thread_factory) -> None:
    if not owner._sample_workflow_can_start("unload"):
        return
    if owner._design_registration_is_active():
        response = message_box.question(
            owner,
            "Unload Sample",
            "Unload sample and clear design registration?",
            message_box.Yes | message_box.No,
            message_box.No,
        )
        if response != message_box.Yes:
            return
        owner._invalidate_design_registration(
            "Design registration cleared before sample unload."
        )
    owner._remember_sample_focus_from_latest()
    xy_feedrate = owner.stage_controller.max_feedrate_for_axes(("X", "Y"))
    needle_feedrate = owner._current_needle_feedrate()
    thread = thread_factory(
        target=owner._run_sample_unload,
        args=(xy_feedrate, needle_feedrate),
        daemon=True,
        name="SampleUnload",
    )
    owner._sample_handling_thread = thread
    thread.start()


def request_sample_load(owner: object, *, thread_factory) -> None:
    if not owner._sample_workflow_can_start("load"):
        return
    focus_z_mm = owner._sample_load_focus_z()
    xy_feedrate = owner.stage_controller.max_feedrate_for_axes(("X", "Y"))
    focus_feedrate = owner.stage_controller.max_feedrate_for_axes(("Z",))
    needle_feedrate = owner._current_needle_feedrate()
    thread = thread_factory(
        target=owner._run_sample_load,
        args=(focus_z_mm, xy_feedrate, focus_feedrate, needle_feedrate),
        daemon=True,
        name="SampleLoad",
    )
    owner._sample_handling_thread = thread
    thread.start()


def on_sample_handling_finished(
    owner: object,
    success: bool,
    message: str,
    offer_autofocus: bool,
    focus_z_mm: object,
    *,
    message_box,
) -> None:
    owner._sample_handling_thread = None
    if message:
        owner._show_status(message, 7000)
    stage_position_panel.clear_stage_motion_axes(owner)
    owner._update_stage_coordinate_apply_state()
    owner._schedule_cancel_state_refresh()
    prompt_plan = sample_handling.sample_autofocus_prompt(
        success=success,
        offer_autofocus=offer_autofocus,
        focus_z_mm=focus_z_mm,
    )
    if not prompt_plan.should_prompt:
        return
    response = message_box.question(
        owner,
        "Autofocus",
        prompt_plan.prompt,
        message_box.Yes | message_box.No,
        message_box.Yes,
    )
    if response != message_box.Yes:
        return
    if owner.stage_controller.is_busy():
        owner._show_status("Stage is busy; autofocus not started.", 4000)
        return
    owner.stage_controller.request_autofocus()
