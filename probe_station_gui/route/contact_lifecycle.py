"""Contact placement transactions behind explicit route-control ports."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from probe_station_gui.route.contact_quality import RouteMeasurementSample
from probe_station_gui.route.measurement_payloads import focus_result_to_dict
from probe_station_gui.route.measurement_records import (
    RouteContactPlacementResult,
    RouteContactSeekResult,
    RouteExternalContactPreparation,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)


logger = logging.getLogger("probe_station_gui.route.measurement")


class ContactTask(Protocol):
    def wait(self) -> None: ...


class RouteContactStagePort(Protocol):
    def begin(self) -> None: ...

    def finish(self) -> None: ...

    def wait_for_background_tasks(self) -> None: ...

    def raise_needles(self) -> None: ...

    def lift_needles(self) -> None: ...

    def lower_needles(self) -> None: ...

    def move_to_contact(self, point: RouteMeasurementPoint) -> None: ...

    def move_to_photo(self, point: RouteMeasurementPoint) -> tuple[float, float]: ...

    def photo_matches_contact(
        self,
        point: RouteMeasurementPoint,
        photo_xy: tuple[float, float],
    ) -> bool: ...

    def settle_contact(self) -> bool: ...

    def settle_photo(self) -> bool: ...


class RouteContactMeterPort(Protocol):
    def initial_count(self) -> int: ...

    def prepare(self, count: int) -> ContactTask | None: ...

    def measure(
        self,
        *,
        position: int,
        total: int,
        prepare_task: ContactTask | None,
    ) -> list[RouteMeasurementSample] | None: ...

    def close_output(self) -> None: ...


@dataclass(frozen=True)
class RouteContactMessage:
    action_label: str
    failure_label: str
    point: RouteMeasurementPoint
    record: RouteMeasurementRecord
    position: int
    total: int
    success: bool
    seek: RouteContactSeekResult | None = None


class RouteContactQualityPort(Protocol):
    def reset_seek(self) -> None: ...

    def current_seek(self) -> RouteContactSeekResult | None: ...

    def auto_seek_enabled(self) -> bool: ...

    def set_auto_seek_enabled(self, enabled: bool) -> None: ...

    def record(
        self,
        point: RouteMeasurementPoint,
        samples: list[RouteMeasurementSample],
    ) -> RouteMeasurementRecord: ...

    def placement_success(self, record: RouteMeasurementRecord) -> bool: ...

    def placement_message(self, message: RouteContactMessage) -> str: ...


class RouteContactInterruptPort(Protocol):
    def clear(self) -> None: ...

    def requested(self) -> bool: ...


class RouteContactEventPort(Protocol):
    def status(self, message: str) -> None: ...

    def pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None: ...

    def contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        success: bool,
    ) -> None: ...


class RouteContactAutofocusPort(Protocol):
    def run(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> object | None: ...


class RouteContactPhotoPort(Protocol):
    def capture(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        focus_result: object | None,
    ) -> str: ...


@dataclass(frozen=True)
class CallbackContactAutofocusAdapter:
    run_callback: Callable[[RouteMeasurementPoint, int, int], object | None]

    def run(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> object | None:
        return self.run_callback(point, position, total)


@dataclass(frozen=True)
class CallbackContactPhotoAdapter:
    capture_callback: Callable[
        [RouteMeasurementPoint, int, int, object | None], object
    ]

    def capture(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        focus_result: object | None,
    ) -> str:
        return str(self.capture_callback(point, position, total, focus_result))


@dataclass(frozen=True)
class RouteContactRequest:
    """One contact transaction and its placement/photo policy."""

    point: RouteMeasurementPoint
    position: int = 1
    total: int = 1
    move_to_point: bool = True
    lift_before_move: bool = True
    lift_on_failure: bool = True
    clear_interrupt: bool = True
    autofocus: RouteContactAutofocusPort | None = None
    photo: RouteContactPhotoPort | None = None


@dataclass(frozen=True)
class RouteContactFlowResult:
    """Internal result; runner adapters retain their historical exception surface."""

    placement: RouteContactPlacementResult | None = None
    preparation: RouteExternalContactPreparation | None = None
    interrupted: bool = False
    interruption_message: str = "Route contact interrupted."

    def require_placement(self) -> RouteContactPlacementResult:
        if self.interrupted or self.placement is None:
            raise RuntimeError(self.interruption_message)
        return self.placement

    def require_preparation(self) -> RouteExternalContactPreparation:
        if self.interrupted or self.preparation is None:
            raise RuntimeError(self.interruption_message)
        return self.preparation


@dataclass(frozen=True)
class _ExternalContactPhotoPrelude:
    photo_path: str | None = None
    focus_result: object | None = None


class _ContactInterrupted(RuntimeError):
    pass


class RouteContactFlow:
    """Place, assess, and release one route contact through named ports."""

    def __init__(
        self,
        *,
        stage: RouteContactStagePort,
        meter: RouteContactMeterPort,
        quality: RouteContactQualityPort,
        interrupt: RouteContactInterruptPort,
        events: RouteContactEventPort,
    ) -> None:
        self._stage = stage
        self._meter = meter
        self._quality = quality
        self._interrupt = interrupt
        self._events = events

    def place_contact(self, request: RouteContactRequest) -> RouteContactFlowResult:
        try:
            prelude, placement_request = self._photo_prelude_if_requested(request)
            placement = self._place_contact(placement_request)
        except _ContactInterrupted as exc:
            return RouteContactFlowResult(
                interrupted=True,
                interruption_message=str(exc) or "Route contact interrupted.",
            )
        preparation = None
        if request.autofocus is not None or request.photo is not None:
            preparation = RouteExternalContactPreparation(
                placement=placement,
                photo_path=prelude.photo_path,
                focus=focus_result_to_dict(prelude.focus_result),
            )
        return RouteContactFlowResult(
            placement=placement,
            preparation=preparation,
        )

    def prepare_external_contact(
        self,
        request: RouteContactRequest,
    ) -> RouteContactFlowResult:
        result = self.place_contact(request)
        if result.interrupted or result.preparation is not None:
            return result
        placement = result.require_placement()
        return replace(
            result,
            preparation=RouteExternalContactPreparation(placement=placement),
        )

    def lift_after_external_measurement(
        self,
        *,
        position: int = 1,
        total: int = 1,
    ) -> None:
        self._stage.begin()
        try:
            self._events.status(
                f"Route contact: point {position}/{total} lifting needles."
            )
            self._stage.lift_needles()
        finally:
            try:
                self._stage.wait_for_background_tasks()
            finally:
                try:
                    self._meter.close_output()
                finally:
                    self._stage.finish()

    def check_contact(self, request: RouteContactRequest) -> RouteContactFlowResult:
        return self.measure_current_contact(
            request,
            auto_contact_seek=False,
            action_label="Contact check",
        )

    def seek_contact(self, request: RouteContactRequest) -> RouteContactFlowResult:
        return self.measure_current_contact(
            request,
            auto_contact_seek=True,
            action_label="Contact seek",
        )

    def measure_current_contact(
        self,
        request: RouteContactRequest,
        *,
        auto_contact_seek: bool,
        action_label: str,
    ) -> RouteContactFlowResult:
        self._interrupt.clear()
        self._quality.reset_seek()
        previous_auto_seek = self._quality.auto_seek_enabled()
        self._quality.set_auto_seek_enabled(auto_contact_seek)
        self._stage.begin()
        try:
            prepare_task = self._meter.prepare(self._meter.initial_count())
            if not self._stage.settle_contact():
                raise RuntimeError(f"{action_label} stopped.")
            self._events.status(
                f"{action_label}: point {request.position}/{request.total} "
                "checking contact."
            )
            samples = self._meter.measure(
                position=request.position,
                total=request.total,
                prepare_task=prepare_task,
            )
            if samples is None:
                if self._interrupt.requested():
                    raise _ContactInterrupted(f"{action_label} stopped.")
                raise RuntimeError(f"{action_label} stopped.")
            placement = self._placement_from_samples(
                request,
                samples,
                action_label=action_label,
                failure_label=f"{action_label} failed",
                include_seek=auto_contact_seek,
            )
            self._events.status(placement.message)
            return RouteContactFlowResult(placement=placement)
        except _ContactInterrupted as exc:
            return RouteContactFlowResult(
                interrupted=True,
                interruption_message=str(exc) or "Route contact interrupted.",
            )
        finally:
            self._quality.set_auto_seek_enabled(previous_auto_seek)
            try:
                self._stage.wait_for_background_tasks()
            finally:
                self._stage.finish()

    def _photo_prelude_if_requested(
        self,
        request: RouteContactRequest,
    ) -> tuple[_ExternalContactPhotoPrelude, RouteContactRequest]:
        if (
            not request.move_to_point
            or (request.autofocus is None and request.photo is None)
        ):
            return _ExternalContactPhotoPrelude(), request
        prelude = self._prepare_external_contact_photo_prelude(request)
        return prelude, replace(
            request,
            move_to_point=False,
            lift_before_move=False,
        )

    def _prepare_external_contact_photo_prelude(
        self,
        request: RouteContactRequest,
    ) -> _ExternalContactPhotoPrelude:
        self._stage.begin()
        try:
            photo_xy = self._raise_and_move_to_photo(request)
            focus_result = self._focus_photo(request)
            photo_path = self._capture_photo(request, focus_result)
            if not self._stage.photo_matches_contact(request.point, photo_xy):
                self._events.status(
                    f"Route contact: point {request.position}/{request.total} "
                    "moving to contact."
                )
                self._stage.move_to_contact(request.point)
                self._raise_if_interrupted()
            return _ExternalContactPhotoPrelude(photo_path, focus_result)
        finally:
            try:
                self._stage.wait_for_background_tasks()
            finally:
                self._stage.finish()

    def _raise_and_move_to_photo(
        self,
        request: RouteContactRequest,
    ) -> tuple[float, float]:
        self._events.status(
            f"Route contact: point {request.position}/{request.total} "
            "raising needles."
        )
        self._stage.raise_needles()
        self._raise_if_interrupted()
        self._events.status(
            f"Route contact: point {request.position}/{request.total} moving to photo."
        )
        photo_xy = self._stage.move_to_photo(request.point)
        self._raise_if_interrupted()
        return photo_xy

    def _focus_photo(self, request: RouteContactRequest) -> object | None:
        if request.autofocus is None:
            return None
        self._events.status(
            f"Route contact: point {request.position}/{request.total} local autofocus."
        )
        focus_result = request.autofocus.run(
            request.point,
            request.position,
            request.total,
        )
        self._raise_if_interrupted()
        focus_message = str(focus_result or "")
        if focus_message.strip():
            self._events.status(
                f"Route contact: point {request.position}/{request.total} "
                f"{focus_message}"
            )
        return focus_result

    def _capture_photo(
        self,
        request: RouteContactRequest,
        focus_result: object | None,
    ) -> str | None:
        if request.photo is None:
            return None
        if not self._stage.settle_photo():
            raise RuntimeError("Route contact photo stopped.")
        photo_path = request.photo.capture(
            request.point,
            request.position,
            request.total,
            focus_result=focus_result,
        )
        self._events.status(
            f"Route contact: point {request.position}/{request.total} photo captured."
        )
        self._raise_if_interrupted()
        return str(photo_path)

    def _place_contact(
        self,
        request: RouteContactRequest,
    ) -> RouteContactPlacementResult:
        self._prepare_interrupt(request.clear_interrupt)
        self._quality.reset_seek()
        self._stage.begin()
        needles_lowered = False
        placement_succeeded = False
        try:
            self._prepare_contact_move(request)
            self._prepare_measurement_batch()
            self._events.status(
                f"Route contact: point {request.position}/{request.total} "
                "lowering needles."
            )
            self._events.pre_contact_photo(
                request.point,
                request.position,
                request.total,
            )
            self._raise_if_interrupted()
            self._stage.lower_needles()
            needles_lowered = True
            samples = self._measure_placement_samples(request)
            placement = self._placement_from_samples(
                request,
                samples,
                action_label="Contact ready",
                failure_label="Contact check failed",
            )
            if not placement.success and request.lift_on_failure:
                self._lift_failed_contact()
                needles_lowered = False
            placement_succeeded = placement.success
            self._events.contact_photo(
                request.point,
                placement.record,
                request.position,
                request.total,
                placement.success,
            )
            self._events.status(placement.message)
            return placement
        finally:
            if (
                needles_lowered
                and request.lift_on_failure
                and not placement_succeeded
            ):
                self._try_lift_failed_contact()
            try:
                self._stage.wait_for_background_tasks()
            finally:
                self._stage.finish()

    def _prepare_interrupt(self, clear_interrupt: bool) -> None:
        if clear_interrupt:
            self._interrupt.clear()
        elif self._interrupt.requested():
            raise _ContactInterrupted("Contact placement interrupted.")

    def _prepare_contact_move(self, request: RouteContactRequest) -> None:
        if request.lift_before_move:
            self._events.status(
                f"Route contact: point {request.position}/{request.total} "
                "lifting needles."
            )
            self._stage.lift_needles()
            self._raise_if_interrupted()
        if request.move_to_point:
            self._events.status(
                f"Route contact: point {request.position}/{request.total} moving."
            )
            self._stage.move_to_contact(request.point)
            self._raise_if_interrupted()
        self._raise_if_interrupted()

    def _prepare_measurement_batch(self) -> None:
        prepare_task = self._meter.prepare(self._meter.initial_count())
        self._raise_if_interrupted()
        if prepare_task is not None:
            prepare_task.wait()

    def _measure_placement_samples(
        self,
        request: RouteContactRequest,
    ) -> list[RouteMeasurementSample]:
        if not self._stage.settle_contact():
            if self._interrupt.requested():
                raise _ContactInterrupted("Contact placement stopped.")
            raise RuntimeError("Contact placement stopped.")
        self._events.status(
            f"Route contact: point {request.position}/{request.total} "
            "checking contact."
        )
        samples = self._meter.measure(
            position=request.position,
            total=request.total,
            prepare_task=None,
        )
        if samples is None:
            if self._interrupt.requested():
                raise _ContactInterrupted("Contact placement stopped.")
            raise RuntimeError("Contact placement stopped.")
        return samples

    def _placement_from_samples(
        self,
        request: RouteContactRequest,
        samples: list[RouteMeasurementSample],
        *,
        action_label: str,
        failure_label: str,
        include_seek: bool = False,
    ) -> RouteContactPlacementResult:
        record = self._quality.record(request.point, samples)
        success = self._quality.placement_success(record)
        seek = self._quality.current_seek()
        message = self._quality.placement_message(
            RouteContactMessage(
                action_label=action_label,
                failure_label=failure_label,
                point=request.point,
                record=record,
                position=request.position,
                total=request.total,
                success=success,
                seek=seek if include_seek else None,
            )
        )
        return RouteContactPlacementResult(
            success=success,
            message=message,
            point=request.point,
            record=record,
            contact_seek=seek,
        )

    def _lift_failed_contact(self) -> None:
        self._stage.lift_needles()
        self._meter.close_output()

    def _try_lift_failed_contact(self) -> None:
        try:
            self._lift_failed_contact()
        except Exception:
            logger.exception("Failed to lift needles after contact check.")

    def _raise_if_interrupted(self) -> None:
        if self._interrupt.requested():
            raise _ContactInterrupted("Route contact interrupted.")


__all__ = [
    "CallbackContactAutofocusAdapter",
    "CallbackContactPhotoAdapter",
    "ContactTask",
    "RouteContactAutofocusPort",
    "RouteContactEventPort",
    "RouteContactFlow",
    "RouteContactFlowResult",
    "RouteContactInterruptPort",
    "RouteContactMessage",
    "RouteContactMeterPort",
    "RouteContactPhotoPort",
    "RouteContactQualityPort",
    "RouteContactRequest",
    "RouteContactStagePort",
]
