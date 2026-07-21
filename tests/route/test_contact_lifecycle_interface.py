from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from probe_station_gui.route.contact_lifecycle import (
    RouteContactFlow,
    RouteContactMessage,
    RouteContactRequest,
)
from probe_station_gui.route.contact_quality import RouteMeasurementSample
from probe_station_gui.route.measurement_records import (
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)


class _Task:
    def wait(self) -> None:
        pass


class _StageAdapter:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def begin(self) -> None:
        self._events.append("stage:begin")

    def finish(self) -> None:
        self._events.append("stage:finish")

    def wait_for_background_tasks(self) -> None:
        self._events.append("stage:wait-background")

    def raise_needles(self) -> None:
        self._events.append("needles:raise")

    def lift_needles(self) -> None:
        self._events.append("needles:lift")

    def lower_needles(self) -> None:
        self._events.append("needles:lower")

    def move_to_contact(self, point: RouteMeasurementPoint) -> None:
        self._events.append(f"stage:contact:{point.point_id}")

    def move_to_photo(self, point: RouteMeasurementPoint) -> tuple[float, float]:
        self._events.append(f"stage:photo:{point.point_id}")
        return point.photo_stage_xy or point.stage_xy

    def photo_matches_contact(
        self,
        point: RouteMeasurementPoint,
        photo_xy: tuple[float, float],
    ) -> bool:
        return photo_xy == point.stage_xy

    def settle_contact(self) -> bool:
        self._events.append("stage:settle-contact")
        return True

    def settle_photo(self) -> bool:
        self._events.append("stage:settle-photo")
        return True


class _MeterAdapter:
    def __init__(
        self,
        events: list[str],
        *,
        after_measure: Callable[[], None] | None = None,
    ) -> None:
        self._events = events
        self._after_measure = after_measure

    def initial_count(self) -> int:
        return 2

    def prepare(self, count: int) -> _Task:
        self._events.append(f"meter:prepare:{count}")
        return _Task()

    def measure(
        self,
        *,
        position: int,
        total: int,
        prepare_task: _Task | None,
    ) -> list[RouteMeasurementSample] | None:
        self._events.extend(("contact:check", "meter:external"))
        if self._after_measure is not None:
            self._after_measure()
            return None
        return [
            RouteMeasurementSample(1, 100.0),
            RouteMeasurementSample(2, 101.0),
        ]

    def close_output(self) -> None:
        self._events.append("meter:close-output")


class _QualityAdapter:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._auto_seek = False
        self._seek = None

    def reset_seek(self) -> None:
        self._events.append("quality:reset-seek")
        self._seek = None

    def current_seek(self):
        return self._seek

    def auto_seek_enabled(self) -> bool:
        return self._auto_seek

    def set_auto_seek_enabled(self, enabled: bool) -> None:
        self._events.append(f"quality:auto-seek:{str(enabled).lower()}")
        self._auto_seek = bool(enabled)

    def record(
        self,
        point: RouteMeasurementPoint,
        samples: list[RouteMeasurementSample],
    ) -> RouteMeasurementRecord:
        self._events.append("quality:record")
        return _record(status="ok", samples=samples)

    def placement_success(self, record: RouteMeasurementRecord) -> bool:
        return record.status in {"ok", "short"}

    def placement_message(self, message: RouteContactMessage) -> str:
        return f"{message.action_label}: {'ok' if message.success else 'failed'}"


class _InterruptAdapter:
    def __init__(self) -> None:
        self._requested = False

    def clear(self) -> None:
        self._requested = False

    def requested(self) -> bool:
        return self._requested

    def request(self) -> None:
        self._requested = True


class _EventAdapter:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def status(self, message: str) -> None:
        self._events.append(f"status:{message}")

    def pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None:
        self._events.append("photo:pre-contact")

    def contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        success: bool,
    ) -> None:
        self._events.append(f"photo:contact:{str(success).lower()}")


class _AutofocusAdapter:
    def __init__(
        self,
        events: list[str],
        *,
        after: Callable[[], None] | None = None,
    ) -> None:
        self._events = events
        self._after = after

    def run(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> object | None:
        self._events.append("autofocus:start")
        if self._after is not None:
            self._after()
            self._events.append("autofocus:restore-safe-z")
        return {"mode": "local"}


class _PhotoAdapter:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def capture(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        focus_result: object | None,
    ) -> str:
        self._events.append(f"photo:capture:{focus_result}")
        return "contact.png"


class _PauseControl:
    """Coordinator-owned Pause Request/Pause Ack state used by the contract test."""

    def __init__(self) -> None:
        self.pause_requested = False
        self.pause_ack = False

    def request_pause(self) -> None:
        self.pause_requested = True

    def safe_waiting_point(self) -> None:
        if self.pause_requested:
            self.pause_requested = False
            self.pause_ack = True


def _point() -> RouteMeasurementPoint:
    return RouteMeasurementPoint(
        index=1,
        point_id="point-1",
        label="P1",
        design_center=(1.0, 2.0),
        stage_xy=(10.0, 20.0),
        needle_1_design=(0.0, 0.0),
        needle_2_design=(1.0, 1.0),
        photo_stage_xy=(11.0, 21.0),
    )


def _record(
    *,
    status: str,
    samples: list[RouteMeasurementSample] | None = None,
) -> RouteMeasurementRecord:
    return RouteMeasurementRecord(
        timestamp="2026-07-21T00:00:00",
        structure_number=1,
        nplc="1",
        measurement_type="R",
        n_measurements=len(samples or ()),
        resistance_ohm=100.0,
        resistance_rms_ohm=1.0,
        relative_rms=0.01,
        status=status,
        raw_samples=tuple(samples or ()),
    )


def _contact_flow(
    events: list[str],
    *,
    interrupt: _InterruptAdapter | None = None,
    meter: _MeterAdapter | None = None,
) -> RouteContactFlow:
    return RouteContactFlow(
        stage=_StageAdapter(events),
        meter=meter or _MeterAdapter(events),
        quality=_QualityAdapter(events),
        interrupt=interrupt or _InterruptAdapter(),
        events=_EventAdapter(events),
    )


def _contact_request(
    *,
    autofocus: _AutofocusAdapter | None = None,
    photo: _PhotoAdapter | None = None,
) -> RouteContactRequest:
    return RouteContactRequest(
        point=_point(),
        autofocus=autofocus,
        photo=photo,
    )


def test_interrupt_after_autofocus_skips_lower_check_and_external_measurement() -> None:
    events: list[str] = []
    interrupt = _InterruptAdapter()
    flow = _contact_flow(events, interrupt=interrupt)
    autofocus = _AutofocusAdapter(events, after=interrupt.request)

    result = flow.place_contact(_contact_request(autofocus=autofocus))

    assert result.interrupted is True
    assert "autofocus:restore-safe-z" in events
    assert "needles:lower" not in events
    assert "contact:check" not in events
    assert "meter:external" not in events
    assert interrupt.requested() is True


def test_pause_request_is_not_acknowledged_until_coordinator_safe_waiting_point() -> None:
    events: list[str] = []
    control = _PauseControl()
    flow = _contact_flow(events)
    control.request_pause()

    result = flow.place_contact(_contact_request())

    assert result.interrupted is False
    assert control.pause_requested is True
    assert control.pause_ack is False

    flow.lift_after_external_measurement(position=1, total=1)
    assert control.pause_ack is False

    control.safe_waiting_point()
    assert control.pause_requested is False
    assert control.pause_ack is True


def test_external_contact_preserves_focus_photo_and_contact_metadata() -> None:
    events: list[str] = []
    flow = _contact_flow(events)

    result = flow.prepare_external_contact(
        replace(
            _contact_request(
                autofocus=_AutofocusAdapter(events),
                photo=_PhotoAdapter(events),
            ),
            lift_on_failure=False,
        )
    )

    assert result.interrupted is False
    assert result.preparation is not None
    assert result.preparation.photo_path == "contact.png"
    assert result.preparation.focus is not None
    assert result.preparation.placement.contact_seek is None
    assert events.index("needles:raise") < events.index("autofocus:start")
    capture_event = "photo:capture:{'mode': 'local'}"
    assert events.index("autofocus:start") < events.index(capture_event)
    assert "status:Route contact: point 1/1 moving to contact." in events
    assert events.index(capture_event) < events.index("needles:lower")


def test_check_and_seek_restore_the_previous_auto_seek_setting() -> None:
    events: list[str] = []
    flow = _contact_flow(events)
    request = _contact_request()

    checked = flow.check_contact(request)
    sought = flow.seek_contact(request)

    assert checked.placement is not None
    assert sought.placement is not None
    assert events.count("quality:auto-seek:false") >= 2
    assert "quality:auto-seek:true" in events


def test_interrupted_current_check_preserves_compatibility_error_message() -> None:
    events: list[str] = []
    interrupt = _InterruptAdapter()
    flow = _contact_flow(
        events,
        interrupt=interrupt,
        meter=_MeterAdapter(events, after_measure=interrupt.request),
    )

    result = flow.check_contact(_contact_request())

    assert result.interrupted is True
    assert interrupt.requested() is True
    with pytest.raises(RuntimeError, match="^Contact check stopped\\.$"):
        result.require_placement()
