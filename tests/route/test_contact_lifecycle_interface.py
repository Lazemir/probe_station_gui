from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import threading

import pytest

from probe_station_gui.route.contact_lifecycle import (
    RouteContactFlow,
    RouteContactMessage,
    RouteContactRequest,
)
from probe_station_gui.route.contact_lifecycle_adapter import (
    ContactControlBindings,
    ContactMeterBindings,
    ContactQualityBindings,
    ContactStageBindings,
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
    def __init__(
        self,
        events: list[str],
        *,
        after_contact_move: Callable[[], None] | None = None,
    ) -> None:
        self._events = events
        self._after_contact_move = after_contact_move

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
        if self._after_contact_move is not None:
            self._after_contact_move()

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
    def __init__(
        self,
        events: list[str],
        *,
        after: Callable[[], None] | None = None,
    ) -> None:
        self._events = events
        self._after = after

    def capture(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        focus_result: object | None,
    ) -> str:
        self._events.append(f"photo:capture:{focus_result}")
        if self._after is not None:
            self._after()
        return "contact.png"


class _BindingStageController:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None,
    ) -> None:
        self._events.append(f"needles:{action}:{feedrate}")

    def run_external_move_to_xy(self, x: float, y: float) -> None:
        self._events.append(f"stage:xy:{x}:{y}")


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


def test_contact_success_callback_runs_after_quality_and_interrupt_checkpoint() -> None:
    events: list[str] = []
    flow = RouteContactFlow(
        stage=_StageAdapter(events),
        meter=_MeterAdapter(events),
        quality=_QualityAdapter(events),
        interrupt=_InterruptAdapter(),
        events=_EventAdapter(events),
        post_success_contact=lambda _placement: events.append("reference:capture"),
        post_success_contact_eligible=lambda: True,
    )

    result = flow.place_contact(_contact_request())

    assert result.interrupted is False
    assert result.require_placement().success is True
    assert events.index("quality:record") < events.index("reference:capture")
    assert events.index("contact:check") < events.index("reference:capture")


def test_interrupt_requested_during_success_callback_stops_later_contact_work() -> None:
    events: list[str] = []
    interrupt = _InterruptAdapter()
    callback_entered = threading.Event()
    release_callback = threading.Event()
    results = []

    def blocking_callback(_placement) -> None:
        events.append("reference:capture")
        callback_entered.set()
        assert release_callback.wait(2.0)

    flow = RouteContactFlow(
        stage=_StageAdapter(events),
        meter=_MeterAdapter(events),
        quality=_QualityAdapter(events),
        interrupt=interrupt,
        events=_EventAdapter(events),
        post_success_contact=blocking_callback,
        post_success_contact_eligible=lambda: True,
    )
    worker = threading.Thread(
        target=lambda: results.append(flow.place_contact(_contact_request())),
    )
    worker.start()
    assert callback_entered.wait(2.0)

    interrupt.request()
    release_callback.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].interrupted is True
    assert interrupt.requested() is True
    assert "photo:contact:true" not in events
    assert "status:Contact ready: ok" not in events


def test_interrupt_at_quality_checkpoint_never_runs_contact_success_callback() -> None:
    events: list[str] = []
    interrupt = _InterruptAdapter()

    class _InterruptingQuality(_QualityAdapter):
        def placement_success(self, record: RouteMeasurementRecord) -> bool:
            success = super().placement_success(record)
            interrupt.request()
            return success

    flow = RouteContactFlow(
        stage=_StageAdapter(events),
        meter=_MeterAdapter(events),
        quality=_InterruptingQuality(events),
        interrupt=interrupt,
        events=_EventAdapter(events),
        post_success_contact=lambda _placement: events.append("reference:capture"),
        post_success_contact_eligible=lambda: True,
    )

    result = flow.place_contact(_contact_request())

    assert result.interrupted is True
    assert interrupt.requested() is True
    assert "reference:capture" not in events


def test_late_stop_eligibility_blocks_success_callback_at_last_checkpoint() -> None:
    events: list[str] = []
    eligible = True

    class _StoppingQuality(_QualityAdapter):
        def placement_success(self, record: RouteMeasurementRecord) -> bool:
            nonlocal eligible
            success = super().placement_success(record)
            eligible = False
            return success

    flow = RouteContactFlow(
        stage=_StageAdapter(events),
        meter=_MeterAdapter(events),
        quality=_StoppingQuality(events),
        interrupt=_InterruptAdapter(),
        events=_EventAdapter(events),
        post_success_contact=lambda _placement: events.append("reference:capture"),
        post_success_contact_eligible=lambda: eligible,
    )

    result = flow.place_contact(_contact_request())

    assert result.require_placement().success is True
    assert "reference:capture" not in events


def test_success_callback_requires_explicit_eligibility_predicate() -> None:
    events: list[str] = []
    with pytest.raises(ValueError, match="eligibility"):
        RouteContactFlow(
            stage=_StageAdapter(events),
            meter=_MeterAdapter(events),
            quality=_QualityAdapter(events),
            interrupt=_InterruptAdapter(),
            events=_EventAdapter(events),
            post_success_contact=lambda _placement: None,
        )


@pytest.mark.parametrize("boundary", ["autofocus", "photo", "contact_move"])
def test_interrupt_boundaries_stop_before_contact_or_external_wait(
    boundary: str,
) -> None:
    events: list[str] = []
    interrupt = _InterruptAdapter()
    stage = _StageAdapter(
        events,
        after_contact_move=(
            interrupt.request if boundary == "contact_move" else None
        ),
    )
    flow = RouteContactFlow(
        stage=stage,
        meter=_MeterAdapter(events),
        quality=_QualityAdapter(events),
        interrupt=interrupt,
        events=_EventAdapter(events),
    )
    autofocus = _AutofocusAdapter(
        events,
        after=interrupt.request if boundary == "autofocus" else None,
    )
    photo = _PhotoAdapter(
        events,
        after=interrupt.request if boundary == "photo" else None,
    )

    result = flow.prepare_external_contact(
        _contact_request(autofocus=autofocus, photo=photo)
    )

    assert result.interrupted is True
    assert interrupt.requested() is True
    assert "needles:lower" not in events
    assert "contact:check" not in events
    assert "meter:external" not in events
    assert not any(event.startswith("meter:prepare") for event in events)


def test_production_bindings_are_direct_contact_flow_ports() -> None:
    events: list[str] = []
    stage_callbacks = _StageAdapter(events)
    meter_callbacks = _MeterAdapter(events)
    quality_callbacks = _QualityAdapter(events)
    interrupt = _InterruptAdapter()
    event_callbacks = _EventAdapter(events)
    flow = RouteContactFlow(
        stage=ContactStageBindings(
            controller=_BindingStageController(events),
            needle_feedrate=lambda: 75.0,
            begin=stage_callbacks.begin,
            finish=stage_callbacks.finish,
            wait_for_background_tasks=stage_callbacks.wait_for_background_tasks,
            lower_needles=stage_callbacks.lower_needles,
            contact_xy=lambda point: point.stage_xy,
            photo_xy=lambda point: point.photo_stage_xy or point.stage_xy,
            settle_contact=stage_callbacks.settle_contact,
            settle_photo=stage_callbacks.settle_photo,
        ),
        meter=ContactMeterBindings(
            initial_count=meter_callbacks.initial_count,
            prepare=meter_callbacks.prepare,
            measure=meter_callbacks.measure,
            close_output=meter_callbacks.close_output,
        ),
        quality=ContactQualityBindings(
            reset_seek=quality_callbacks.reset_seek,
            current_seek=quality_callbacks.current_seek,
            auto_seek_enabled=quality_callbacks.auto_seek_enabled,
            set_auto_seek_enabled=quality_callbacks.set_auto_seek_enabled,
            record=quality_callbacks.record,
            placement_success=quality_callbacks.placement_success,
            placement_message=quality_callbacks.placement_message,
        ),
        interrupt=ContactControlBindings(
            clear=interrupt.clear,
            requested=interrupt.requested,
            status=event_callbacks.status,
            pre_contact_photo=event_callbacks.pre_contact_photo,
            contact_photo=event_callbacks.contact_photo,
        ),
        events=ContactControlBindings(
            clear=interrupt.clear,
            requested=interrupt.requested,
            status=event_callbacks.status,
            pre_contact_photo=event_callbacks.pre_contact_photo,
            contact_photo=event_callbacks.contact_photo,
        ),
    )

    result = flow.place_contact(_contact_request()).require_placement()

    assert result.success is True
    assert "needles:lift:75.0" in events
    assert "stage:xy:10.0:20.0" in events


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


def test_preexisting_interrupt_without_clear_preserves_placement_error_and_flag() -> None:
    events: list[str] = []
    interrupt = _InterruptAdapter()
    interrupt.request()
    flow = _contact_flow(events, interrupt=interrupt)

    result = flow.place_contact(
        replace(_contact_request(), clear_interrupt=False)
    )

    assert result.interrupted is True
    assert interrupt.requested() is True
    assert "stage:begin" not in events
    with pytest.raises(RuntimeError, match="^Contact placement interrupted\\.$"):
        result.require_placement()
