"""Contact placement lifecycle helpers for route measurements."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from probe_station_gui.route import measurement_recording
from probe_station_gui.route.measurement_payloads import focus_result_to_dict
from probe_station_gui.route.measurement_records import (
    RouteContactPlacementResult,
    RouteExternalContactPreparation,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)


logger = logging.getLogger("probe_station_gui.route.measurement")


@dataclass(frozen=True)
class _ExternalContactPhotoPrelude:
    photo_path: str | None = None
    focus_result: object | None = None


def place_contact(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int = 1,
    total: int = 1,
    move_to_point: bool = True,
    lift_before_move: bool = True,
    lift_on_failure: bool = True,
    clear_interrupt: bool = True,
) -> RouteContactPlacementResult:
    """Move to a route point and leave verified contact under the needles."""

    _prepare_contact_interrupt_state(owner, clear_interrupt=clear_interrupt)
    owner._current_contact_seek_result = None
    owner._begin_stage_task()
    needles_lowered = False
    placement_succeeded = False
    try:
        owner._prepare_contact_placement_move(
            point,
            position=position,
            total=total,
            move_to_point=move_to_point,
            lift_before_move=lift_before_move,
        )
        owner._prepare_contact_measurement_batch()
        owner._status(f"Route contact: point {position}/{total} lowering needles.")
        owner._emit_pre_contact_photo(point, position, total)
        owner._raise_if_point_interrupted()
        owner._lower_needles_for_measurement()
        needles_lowered = True
        record = owner._measure_contact_placement_record(
            point,
            position=position,
            total=total,
        )
        success = measurement_recording.contact_placement_record_is_success(record)
        needles_lowered = _lift_after_failed_contact_if_needed(
            owner,
            success=success,
            lift_on_failure=lift_on_failure,
            needles_lowered=needles_lowered,
        )
        message = measurement_recording.contact_placement_message(
            owner,
            action_label="Contact ready",
            failure_label="Contact check failed",
            point=point,
            record=record,
            position=position,
            total=total,
            success=success,
        )
        if success:
            placement_succeeded = True
        owner._emit_contact_photo(point, record, position, total, success)
        owner._status(message)
        return RouteContactPlacementResult(
            success=success,
            message=message,
            point=point,
            record=record,
            contact_seek=owner._current_contact_seek_result,
        )
    finally:
        _finish_contact_placement(
            owner,
            needles_lowered=needles_lowered,
            lift_on_failure=lift_on_failure,
            placement_succeeded=placement_succeeded,
        )


def _prepare_contact_interrupt_state(owner: Any, *, clear_interrupt: bool) -> None:
    if clear_interrupt:
        owner._point_interrupt_requested.clear()
    elif owner._point_interrupt_requested.is_set():
        raise RuntimeError("Contact placement interrupted.")


def _lift_after_failed_contact_if_needed(
    owner: Any,
    *,
    success: bool,
    lift_on_failure: bool,
    needles_lowered: bool,
) -> bool:
    if success or not lift_on_failure or not needles_lowered:
        return needles_lowered
    owner._lift_needles_after_failed_contact()
    return False


def _finish_contact_placement(
    owner: Any,
    *,
    needles_lowered: bool,
    lift_on_failure: bool,
    placement_succeeded: bool,
) -> None:
    if needles_lowered and lift_on_failure and not placement_succeeded:
        _try_lift_after_failed_contact(owner)
    try:
        owner._wait_for_background_tasks()
    finally:
        owner._finish_stage_task()


def _try_lift_after_failed_contact(owner: Any) -> None:
    try:
        owner._lift_needles_after_failed_contact()
    except Exception:
        logger.exception("Failed to lift needles after contact check.")


def prepare_contact_placement_move(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int,
    total: int,
    move_to_point: bool,
    lift_before_move: bool,
) -> None:
    if lift_before_move:
        owner._status(f"Route contact: point {position}/{total} lifting needles.")
        owner._stage_controller.run_external_needles_action(
            "lift",
            owner._needle_feedrate,
        )
        owner._raise_if_point_interrupted()
    if move_to_point:
        target_xy = owner._adjusted_stage_xy(point)
        owner._status(f"Route contact: point {position}/{total} moving.")
        owner._stage_controller.run_external_move_to_xy(
            target_xy[0],
            target_xy[1],
        )
        owner._raise_if_point_interrupted()
    owner._raise_if_point_interrupted()


def prepare_contact_measurement_batch(owner: Any) -> None:
    prepare_task = owner._start_measurement_prepare_task(
        owner._initial_measurement_count()
    )
    owner._raise_if_point_interrupted()
    if prepare_task is not None:
        prepare_task.wait()


def measure_contact_placement_record(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int,
    total: int,
) -> RouteMeasurementRecord:
    if not owner._sleep_contact_settle():
        raise RuntimeError("Contact placement stopped.")
    owner._status(f"Route contact: point {position}/{total} checking contact.")
    samples = owner._measure_samples(
        position=position,
        total=total,
        prepare_task=None,
    )
    if samples is None:
        raise RuntimeError("Contact placement stopped.")
    return measurement_recording.record_for_point(
        owner,
        point=point,
        samples=samples,
    )


def lift_needles_after_failed_contact(owner: Any) -> None:
    owner._stage_controller.run_external_needles_action(
        "lift",
        owner._needle_feedrate,
    )
    owner._close_meter_output_context()


def prepare_external_contact(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int = 1,
    total: int = 1,
    photo_enabled: bool = False,
    photo_focus_enabled: bool = False,
    move_to_point: bool = True,
    lift_before_move: bool = True,
    lift_on_failure: bool = False,
) -> RouteExternalContactPreparation:
    """Prepare one route contact and leave needles down when usable."""

    prelude = _ExternalContactPhotoPrelude()
    if move_to_point and (photo_enabled or photo_focus_enabled):
        prelude = _prepare_external_contact_photo_prelude(
            owner,
            point,
            position=position,
            total=total,
            photo_enabled=photo_enabled,
            photo_focus_enabled=photo_focus_enabled,
        )
        move_to_point = False
        lift_before_move = False
    placement = owner.place_contact(
        point,
        position=position,
        total=total,
        move_to_point=move_to_point,
        lift_before_move=lift_before_move,
        lift_on_failure=lift_on_failure,
        clear_interrupt=False,
    )
    return RouteExternalContactPreparation(
        placement=placement,
        photo_path=prelude.photo_path,
        focus=focus_result_to_dict(prelude.focus_result),
    )


def _prepare_external_contact_photo_prelude(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int,
    total: int,
    photo_enabled: bool,
    photo_focus_enabled: bool,
) -> _ExternalContactPhotoPrelude:
    owner._begin_stage_task()
    try:
        photo_xy = _raise_and_move_to_external_contact_photo(
            owner,
            point,
            position=position,
            total=total,
        )
        focus_result = _focus_external_contact_photo(
            owner,
            point,
            position=position,
            total=total,
            enabled=photo_focus_enabled,
        )
        photo_path = _capture_external_contact_photo(
            owner,
            point,
            position=position,
            total=total,
            enabled=photo_enabled,
            focus_result=focus_result,
        )
        _move_from_external_photo_to_contact(
            owner,
            point,
            position=position,
            total=total,
            photo_xy=photo_xy,
        )
        return _ExternalContactPhotoPrelude(
            photo_path=photo_path,
            focus_result=focus_result,
        )
    finally:
        try:
            owner._wait_for_background_tasks()
        finally:
            owner._finish_stage_task()


def _raise_and_move_to_external_contact_photo(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int,
    total: int,
) -> tuple[float, float]:
    owner._status(f"Route contact: point {position}/{total} raising needles.")
    owner._stage_controller.run_external_needles_action(
        "raise",
        owner._needle_feedrate,
    )
    owner._raise_if_point_interrupted()
    photo_xy = owner._adjusted_photo_stage_xy(point)
    owner._status(f"Route contact: point {position}/{total} moving to photo.")
    owner._stage_controller.run_external_move_to_xy(
        photo_xy[0],
        photo_xy[1],
    )
    owner._raise_if_point_interrupted()
    return photo_xy


def _focus_external_contact_photo(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int,
    total: int,
    enabled: bool,
) -> object | None:
    if not enabled:
        return None
    owner._status(f"Route contact: point {position}/{total} local autofocus.")
    focus_result = owner._run_photo_focus(point, position, total)
    owner._raise_if_point_interrupted()
    focus_message = str(focus_result or "")
    if focus_message.strip():
        owner._status(f"Route contact: point {position}/{total} {focus_message}")
    return focus_result


def _capture_external_contact_photo(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int,
    total: int,
    enabled: bool,
    focus_result: object | None,
) -> str | None:
    if not enabled:
        return None
    if not owner._sleep_photo_settle():
        raise RuntimeError("Route contact photo stopped.")
    photo_path = str(
        owner._capture_photo(
            point,
            position,
            total,
            focus_result=focus_result,
        )
    )
    owner._status(f"Route contact: point {position}/{total} photo captured.")
    owner._raise_if_point_interrupted()
    return photo_path


def _move_from_external_photo_to_contact(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int,
    total: int,
    photo_xy: tuple[float, float],
) -> None:
    contact_xy = owner._adjusted_stage_xy(point)
    if owner._same_stage_xy(photo_xy, contact_xy):
        return
    owner._status(f"Route contact: point {position}/{total} moving to contact.")
    owner._stage_controller.run_external_move_to_xy(
        contact_xy[0],
        contact_xy[1],
    )
    owner._raise_if_point_interrupted()


def measure_current_contact(
    owner: Any,
    point: RouteMeasurementPoint,
    *,
    position: int,
    total: int,
    auto_contact_seek: bool,
    action_label: str,
) -> RouteContactPlacementResult:
    owner._point_interrupt_requested.clear()
    owner._current_contact_seek_result = None
    previous_auto_seek = owner._auto_contact_seek_on_bad_contact
    owner._auto_contact_seek_on_bad_contact = bool(auto_contact_seek)
    owner._begin_stage_task()
    prepare_task: Any | None = None
    try:
        prepare_task = owner._start_measurement_prepare_task(
            owner._initial_measurement_count()
        )
        if not owner._sleep_contact_settle():
            raise RuntimeError(f"{action_label} stopped.")
        owner._status(f"{action_label}: point {position}/{total} checking contact.")
        samples = owner._measure_samples(
            position=position,
            total=total,
            prepare_task=prepare_task,
        )
        prepare_task = None
        if samples is None:
            raise RuntimeError(f"{action_label} stopped.")
        record = measurement_recording.record_for_point(
            owner,
            point=point,
            samples=samples,
        )
        success = measurement_recording.contact_placement_record_is_success(record)
        seek = owner._current_contact_seek_result
        message = measurement_recording.contact_placement_message(
            owner,
            action_label=action_label,
            failure_label=f"{action_label} failed",
            point=point,
            record=record,
            position=position,
            total=total,
            success=success,
            seek=seek if auto_contact_seek else None,
        )
        owner._status(message)
        return RouteContactPlacementResult(
            success=success,
            message=message,
            point=point,
            record=record,
            contact_seek=seek,
        )
    finally:
        owner._auto_contact_seek_on_bad_contact = previous_auto_seek
        try:
            owner._wait_for_background_tasks()
        finally:
            owner._finish_stage_task()


__all__ = [
    "lift_needles_after_failed_contact",
    "measure_contact_placement_record",
    "measure_current_contact",
    "place_contact",
    "prepare_contact_measurement_batch",
    "prepare_contact_placement_move",
    "prepare_external_contact",
]
