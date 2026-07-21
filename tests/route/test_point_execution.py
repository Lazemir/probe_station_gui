from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from probe_station_gui.route.contact_quality import (
    RouteContactQualityLimits,
    RouteMeasurementSample,
)
from probe_station_gui.route.measurement_records import RouteMeasurementPoint


@dataclass
class _CompletedAction:
    def wait(self) -> None:
        return None


class _Motion:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.feedrates: list[tuple[str, float | None]] = []
        self.z_mm = 4.25

    def raise_needles(self, feedrate: float | None = None) -> None:
        self.feedrates.append(("raise", feedrate))
        self.calls.append("raise")

    def move_to(self, target_xy: tuple[float, float]) -> None:
        self.calls.append(("move", target_xy))

    def lower_needles(self, feedrate: float | None = None) -> None:
        self.feedrates.append(("lower", feedrate))
        self.calls.append("lower")

    def start_lift(self, feedrate: float | None = None) -> _CompletedAction:
        self.feedrates.append(("lift", feedrate))
        self.calls.append("lift")
        return _CompletedAction()

    def fallback_lift(self, feedrate: float | None = None) -> None:
        self.feedrates.append(("fallback_lift", feedrate))
        self.calls.append("fallback_lift")

    def press_to_depth(
        self,
        depth_mm: float,
        step_mm: float,
        feedrate: float | None = None,
    ) -> bool:
        self.feedrates.append(("press", feedrate))
        self.calls.append(("press", depth_mm, step_mm))
        return True

    def latest_axis_a_lowering(self) -> float:
        return 0.0


class _Acquisition:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def prepare(self, count: int, source_list_count: int) -> _CompletedAction:
        self.calls.append(("prepare", count, source_list_count))
        return _CompletedAction()

    def enable_output(self) -> None:
        self.calls.append("output")

    def read(self, count: int, start_index: int, after_measurement=None):
        self.calls.append(("read", count, start_index))
        return []


class _Control:
    def __init__(self) -> None:
        self.interrupt = False

    def stopped(self) -> bool:
        return False

    def interrupted(self) -> bool:
        return self.interrupt

    def settle(self, seconds: float, *, interruptible: bool) -> bool:
        return not (interruptible and self.interrupt)

    def cancelled(self, exc: BaseException) -> bool:
        return self.interrupt and str(exc) == "Operation cancelled."


class _Photo:
    def __init__(self, motion: _Motion, control: _Control) -> None:
        self._motion = motion
        self._control = control
        self.calls: list[str] = []

    def autofocus(self, point, position: int, total: int, settings=None):
        self.calls.append("focus")
        starting_z = self._motion.z_mm
        self._motion.z_mm = starting_z + 0.5
        self._control.interrupt = True
        self._motion.z_mm = starting_z
        return {"focus_best_z_mm": starting_z + 0.5}

    def capture(
        self,
        point,
        position: int,
        total: int,
        focus_result,
        stage_xy,
        settings=None,
    ):
        self.calls.append("capture")
        return Path("point.png")


class _Events:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self, message: str) -> None:
        self.calls.append("status")

    def photo_saved(self, record, position: int, total: int) -> None:
        self.calls.append("photo")

    def pre_contact(self, point, position: int, total: int) -> None:
        self.calls.append("pre_contact")

    def save(self, record) -> None:
        self.calls.append("csv")

    def measurement_finished(self, event) -> None:
        self.calls.append("result")


def _point() -> RouteMeasurementPoint:
    return RouteMeasurementPoint(
        index=1,
        point_id="point-1",
        label="P001",
        design_center=(0.0, 0.0),
        stage_xy=(1.0, 2.0),
        needle_1_design=(-0.1, 0.0),
        needle_2_design=(0.1, 0.0),
        photo_stage_xy=(1.5, 2.5),
    )


def _measure_request():
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointRequest,
    )

    return RoutePointRequest(
        point=_point(),
        position=1,
        total=1,
        route_offset_xy=(0.0, 0.0),
        mode=PointMode(measure=True, photo=False, autofocus=False),
        measurement=PointMeasurementSettings(
            count=1,
            initial_count=1,
            max_relative_rms=None,
            quality_limits=RouteContactQualityLimits(),
            nplc_label="",
            measurement_type="",
        ),
        seek=PointSeekSettings(enabled=False, step_mm=0.002, max_total_mm=0.0),
        contact_settle_s=0.0,
        photo_settle_s=0.0,
    )


@pytest.mark.parametrize(
    ("fallback_fails", "pending_cleanup"),
    [(False, False), (True, True)],
)
def test_cancelled_lower_attempts_lift_fallback_and_reports_cleanup(
    fallback_fails: bool,
    pending_cleanup: bool,
) -> None:
    from probe_station_gui.route.point_execution import RoutePointExecution

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    events = _Events()

    def cancelled_lower(feedrate: float | None = None) -> None:
        motion.calls.append("lower")
        control.interrupt = True
        raise RuntimeError("Operation cancelled.")

    class _FailedLift:
        def wait(self) -> None:
            motion.calls.append("lift_wait")
            raise RuntimeError("lift failed")

    def start_lift(feedrate: float | None = None) -> _FailedLift:
        motion.calls.append("lift_start")
        return _FailedLift()

    def fallback_lift(feedrate: float | None = None) -> None:
        motion.calls.append("fallback_lift")
        if fallback_fails:
            raise RuntimeError("fallback failed")

    motion.lower_needles = cancelled_lower
    motion.start_lift = start_lift
    motion.fallback_lift = fallback_lift
    result = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    ).execute(_measure_request())

    assert result.interrupted is True
    assert result.pending_cleanup is pending_cleanup
    assert motion.calls[-4:] == [
        "lower",
        "lift_start",
        "lift_wait",
        "fallback_lift",
    ]
    assert not any(
        call[0] == "read" for call in acquisition.calls if isinstance(call, tuple)
    )
    assert not {"csv", "result"}.intersection(events.calls)


def test_interrupt_during_pre_contact_skips_lower_read_and_recording() -> None:
    from probe_station_gui.route.point_execution import RoutePointExecution

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()

    class _InterruptingEvents(_Events):
        def pre_contact(self, point, position: int, total: int) -> None:
            super().pre_contact(point, position, total)
            control.interrupt = True

    events = _InterruptingEvents()
    result = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    ).execute(_measure_request())

    assert result.interrupted is True
    assert result.pending_cleanup is False
    assert "lower" not in motion.calls
    assert not any(
        call[0] == "read" for call in acquisition.calls if isinstance(call, tuple)
    )
    assert not {"csv", "result"}.intersection(events.calls)


def test_interrupt_after_prepare_wait_skips_pre_contact_and_cleans_up() -> None:
    from probe_station_gui.route.point_execution import RoutePointExecution

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    events = _Events()

    class _InterruptingPrepare:
        def wait(self) -> None:
            control.interrupt = True

    acquisition.prepare = lambda _count, _source_count: _InterruptingPrepare()
    result = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    ).execute(_measure_request())

    assert result.interrupted is True
    assert result.pending_cleanup is False
    assert "lift" in motion.calls
    assert "pre_contact" not in events.calls
    assert "lower" not in motion.calls
    assert not any(
        call[0] == "read" for call in acquisition.calls if isinstance(call, tuple)
    )
    assert not {"csv", "result"}.intersection(events.calls)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_prepare_wait_error_runs_lift_and_preserves_failure(
    cleanup_fails: bool,
) -> None:
    from probe_station_gui.route.point_execution import (
        RoutePointCleanupError,
        RoutePointExecution,
    )

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    original = RuntimeError("prepare wait failed")

    class _FailedPrepare:
        def wait(self) -> None:
            raise original

    class _FailedLift:
        def wait(self) -> None:
            motion.calls.append("lift_wait")
            raise RuntimeError("lift failed")

    acquisition.prepare = lambda _count, _source_count: _FailedPrepare()
    if cleanup_fails:
        motion.start_lift = lambda _feedrate=None: (
            motion.calls.append("lift_start") or _FailedLift()
        )
        motion.fallback_lift = lambda _feedrate=None: (
            motion.calls.append("fallback_lift")
            or (_ for _ in ()).throw(RuntimeError("fallback failed"))
        )

    with pytest.raises(RuntimeError) as raised:
        RoutePointExecution(
            motion=motion,
            acquisition=acquisition,
            photo=_Photo(motion, control),
            control=control,
            events=_Events(),
        ).execute(_measure_request())

    if cleanup_fails:
        assert type(raised.value) is RoutePointCleanupError
        assert raised.value.cause is original
        assert raised.value.pending_cleanup is True
        assert motion.calls[-3:] == ["lift_start", "lift_wait", "fallback_lift"]
    else:
        assert raised.value is original
        assert motion.calls[-1] == "lift"


def test_read_exception_after_lower_lifts_before_preserving_original_error() -> None:
    from probe_station_gui.route.point_execution import RoutePointExecution

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    acquisition.read = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("meter failed")
    )

    with pytest.raises(RuntimeError, match="meter failed") as raised:
        RoutePointExecution(
            motion=motion,
            acquisition=acquisition,
            photo=_Photo(motion, control),
            control=control,
            events=_Events(),
        ).execute(_measure_request())

    assert type(raised.value) is RuntimeError
    assert motion.calls[-1] == "lift"


def test_read_exception_reports_pending_cleanup_when_both_lifts_fail() -> None:
    from probe_station_gui.route.point_execution import RoutePointExecution

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    original = RuntimeError("meter failed")
    acquisition.read = lambda *args, **kwargs: (_ for _ in ()).throw(original)
    motion.start_lift = lambda _feedrate=None: (_ for _ in ()).throw(RuntimeError("lift failed"))
    motion.fallback_lift = lambda _feedrate=None: (_ for _ in ()).throw(
        RuntimeError("fallback failed")
    )

    with pytest.raises(RuntimeError) as raised:
        RoutePointExecution(
            motion=motion,
            acquisition=acquisition,
            photo=_Photo(motion, control),
            control=control,
            events=_Events(),
        ).execute(_measure_request())

    assert getattr(raised.value, "pending_cleanup", False) is True
    assert getattr(raised.value, "cause", None) is original


def test_interrupt_during_lift_wait_skips_csv_and_result() -> None:
    from probe_station_gui.route.point_execution import RoutePointExecution

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    acquisition.read = lambda *args, **kwargs: [
        RouteMeasurementSample(1, 1000.0)
    ]

    class _InterruptingLift:
        def wait(self) -> None:
            control.interrupt = True

    motion.start_lift = lambda _feedrate=None: _InterruptingLift()
    events = _Events()
    result = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    ).execute(_measure_request())

    assert result.interrupted is True
    assert result.pending_cleanup is False
    assert not {"csv", "result"}.intersection(events.calls)


def test_seek_found_by_compliance_preserves_short_metadata() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    batches = iter(
        [
            [
                RouteMeasurementSample(1, 1000.0),
                RouteMeasurementSample(2, 1500.0),
            ],
            [
                RouteMeasurementSample(1, 2.0, compliance_hit=True),
                RouteMeasurementSample(2, 2.0, compliance_hit=True),
            ],
        ]
    )
    acquisition.read = lambda *args, **kwargs: next(batches)
    request = RoutePointRequest(
        point=_point(),
        position=1,
        total=1,
        route_offset_xy=(0.0, 0.0),
        mode=PointMode(measure=True, photo=False, autofocus=False),
        measurement=PointMeasurementSettings(
            count=2,
            initial_count=2,
            max_relative_rms=None,
            quality_limits=RouteContactQualityLimits(max_mad_sigma_ohm=1.0),
            nplc_label="",
            measurement_type="",
        ),
        seek=PointSeekSettings(enabled=True, step_mm=-0.002, max_total_mm=0.01),
        contact_settle_s=0.0,
        photo_settle_s=0.0,
    )

    result = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=_Events(),
    ).execute(request)

    assert result.seek is not None
    assert result.seek.found is True
    assert result.seek.status == "short"
    assert result.seek.final_status == "short"


def test_seek_truncated_final_attempt_respects_configured_depth_limit() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    bad = [
        RouteMeasurementSample(1, 1000.0),
        RouteMeasurementSample(2, 1500.0),
    ]
    acquisition.read = lambda *args, **kwargs: bad
    request = RoutePointRequest(
        point=_point(),
        position=1,
        total=1,
        route_offset_xy=(0.0, 0.0),
        mode=PointMode(measure=True, photo=False, autofocus=False),
        measurement=PointMeasurementSettings(
            count=2,
            initial_count=2,
            max_relative_rms=None,
            quality_limits=RouteContactQualityLimits(max_mad_sigma_ohm=1.0),
            nplc_label="",
            measurement_type="",
        ),
        seek=PointSeekSettings(enabled=True, step_mm=-0.003, max_total_mm=0.01),
        contact_settle_s=0.0,
        photo_settle_s=0.0,
    )

    RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=_Events(),
    ).execute(request)

    presses = [
        call
        for call in motion.calls
        if isinstance(call, tuple) and call[0] == "press"
    ]
    assert [call[1] for call in presses] == pytest.approx(
        [0.003, 0.006, 0.009, 0.01]
    )
    assert [call[2] for call in presses] == pytest.approx(
        [-0.003, -0.003, -0.003, -0.001]
    )
    assert abs(sum(call[2] for call in presses)) <= 0.01


def test_seek_emits_start_attempt_result_and_exhaustion_statuses() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    acquisition.read = lambda *args, **kwargs: [
        RouteMeasurementSample(1, 1000.0),
        RouteMeasurementSample(2, 1500.0),
    ]

    class _StatusEvents(_Events):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def status(self, message: str) -> None:
            self.messages.append(message)

    events = _StatusEvents()
    request = RoutePointRequest(
        point=_point(),
        position=2,
        total=4,
        route_offset_xy=(0.0, 0.0),
        mode=PointMode(measure=True, photo=False, autofocus=False),
        measurement=PointMeasurementSettings(
            count=2,
            initial_count=2,
            max_relative_rms=None,
            quality_limits=RouteContactQualityLimits(max_mad_sigma_ohm=1.0),
            nplc_label="",
            measurement_type="",
        ),
        seek=PointSeekSettings(enabled=True, step_mm=-0.002, max_total_mm=0.002),
        contact_settle_s=0.0,
        photo_settle_s=0.0,
    )

    RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    ).execute(request)

    text = "\n".join(events.messages)
    assert "point 2/4 contact check" in text
    assert "pressing deeper 1/1" in text
    assert "0.0020 mm below down" in text
    assert "contact seek did not find stable contact" in text


def test_individual_meter_read_stops_after_interrupt_between_samples() -> None:
    from probe_station_gui.route.point_execution_adapters import (
        MeterPointAcquisitionAdapter,
    )

    interrupted = False

    class _IndividualMeter:
        calls = 0

        def read_route_measurement_now(self):
            nonlocal interrupted
            self.calls += 1
            interrupted = True
            return 1000.0

    meter = _IndividualMeter()
    adapter = MeterPointAcquisitionAdapter(
        meter,
        cancelled=lambda: interrupted,
    )

    assert adapter.read(3, 1) is None
    assert meter.calls == 1


def test_point_request_feedrate_reaches_lower_and_lift_ports() -> None:
    from probe_station_gui.route.point_execution import RoutePointExecution

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    acquisition.read = lambda *args, **kwargs: [
        RouteMeasurementSample(1, 1000.0)
    ]

    RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=_Events(),
    ).execute(replace(_measure_request(), needle_feedrate=123.0))

    assert ("lower", 123.0) in motion.feedrates
    assert ("lift", 123.0) in motion.feedrates


def test_interrupt_after_autofocus_restores_safe_z_and_skips_downstream_work() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointFocusOutcome,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    motion = _Motion()
    acquisition = _Acquisition()
    control = _Control()
    photo = _Photo(motion, control)
    original_autofocus = photo.autofocus
    photo.autofocus = lambda *args: PointFocusOutcome(
        value=original_autofocus(*args),
        safe_z=True,
    )
    events = _Events()
    execution = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=photo,
        control=control,
        events=events,
    )
    request = RoutePointRequest(
        point=_point(),
        position=1,
        total=1,
        route_offset_xy=(0.0, 0.0),
        mode=PointMode(measure=True, photo=True, autofocus=True),
        measurement=PointMeasurementSettings(
            count=3,
            initial_count=1,
            max_relative_rms=None,
            quality_limits=RouteContactQualityLimits(),
            nplc_label="1 PLC",
            measurement_type="resistance",
        ),
        seek=PointSeekSettings(enabled=True, step_mm=0.002, max_total_mm=0.02),
        contact_settle_s=0.0,
        photo_settle_s=0.0,
    )

    result = execution.execute(request)

    assert result.interrupted is True
    assert result.pending_cleanup is False
    assert motion.z_mm == 4.25
    assert photo.calls == ["focus"]
    assert "lower" not in motion.calls
    assert not any(call[0] == "read" for call in acquisition.calls if isinstance(call, tuple))
    assert not {"pre_contact", "csv", "result", "photo"}.intersection(events.calls)


def test_interrupt_during_lower_lifts_and_skips_read_and_recording() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    control = _Control()
    motion = _Motion()
    original_lower = motion.lower_needles

    def interrupting_lower(feedrate: float | None = None) -> None:
        original_lower(feedrate)
        control.interrupt = True

    motion.lower_needles = interrupting_lower
    acquisition = _Acquisition()
    events = _Events()
    execution = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    )
    result = execution.execute(
        RoutePointRequest(
            point=_point(),
            position=1,
            total=1,
            route_offset_xy=(0.0, 0.0),
            mode=PointMode(measure=True, photo=False, autofocus=False),
            measurement=PointMeasurementSettings(
                count=2,
                initial_count=1,
                max_relative_rms=None,
                quality_limits=RouteContactQualityLimits(),
                nplc_label="1 PLC",
                measurement_type="resistance",
            ),
            seek=PointSeekSettings(enabled=False, step_mm=0.002, max_total_mm=0.0),
            contact_settle_s=0.0,
            photo_settle_s=0.0,
        )
    )

    assert result.interrupted is True
    assert result.pending_cleanup is False
    assert motion.calls[-1] == "lift"
    assert not any(call[0] == "read" for call in acquisition.calls if isinstance(call, tuple))
    assert not {"csv", "result"}.intersection(events.calls)


def test_interrupt_during_read_lifts_and_skips_csv_and_result() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()

    def interrupting_read(count: int, start_index: int, after_measurement=None):
        acquisition.calls.append(("read", count, start_index))
        control.interrupt = True
        if after_measurement is not None:
            after_measurement()
        return None

    acquisition.read = interrupting_read
    events = _Events()
    execution = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    )
    request = RoutePointRequest(
        point=_point(),
        position=1,
        total=1,
        route_offset_xy=(0.0, 0.0),
        mode=PointMode(measure=True, photo=False, autofocus=False),
        measurement=PointMeasurementSettings(
            count=2,
            initial_count=1,
            max_relative_rms=None,
            quality_limits=RouteContactQualityLimits(),
            nplc_label="1 PLC",
            measurement_type="resistance",
        ),
        seek=PointSeekSettings(enabled=False, step_mm=0.002, max_total_mm=0.0),
        contact_settle_s=0.0,
        photo_settle_s=0.0,
    )

    result = execution.execute(request)

    assert result.interrupted is True
    assert result.pending_cleanup is False
    assert motion.calls.count("lift") == 1
    assert not {"csv", "result"}.intersection(events.calls)


def test_photo_is_captured_before_needles_lower() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    order: list[str] = []
    control = _Control()
    motion = _Motion()
    original_lower = motion.lower_needles
    motion.lower_needles = lambda feedrate=None: (
        order.append("lower"),
        original_lower(feedrate),
    )[-1]
    acquisition = _Acquisition()
    acquisition.read = lambda count, start_index, after_measurement=None: []
    photo = _Photo(motion, control)
    photo.capture = lambda *args: (order.append("photo"), Path("point.png"))[1]
    events = _Events()
    execution = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=photo,
        control=control,
        events=events,
    )
    result = execution.execute(
        RoutePointRequest(
            point=_point(),
            position=1,
            total=1,
            route_offset_xy=(0.0, 0.0),
            mode=PointMode(measure=True, photo=True, autofocus=False),
            measurement=PointMeasurementSettings(
                count=1,
                initial_count=1,
                max_relative_rms=None,
                quality_limits=RouteContactQualityLimits(),
                nplc_label="",
                measurement_type="",
            ),
            seek=PointSeekSettings(enabled=False, step_mm=0.002, max_total_mm=0.0),
            contact_settle_s=0.0,
            photo_settle_s=0.0,
        )
    )

    assert result.photos_saved == 1
    assert order[:2] == ["photo", "lower"]


def test_lift_starts_before_save_and_measurement_callbacks() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    order: list[str] = []
    control = _Control()
    motion = _Motion()

    class _OrderedAction:
        def wait(self) -> None:
            order.append("lift_wait")

    motion.start_lift = lambda _feedrate=None: (
        order.append("lift_start"),
        _OrderedAction(),
    )[1]
    acquisition = _Acquisition()

    def read(count: int, start_index: int, after_measurement=None):
        if after_measurement is not None:
            after_measurement()
        return [
            RouteMeasurementSample(1, 1000.0),
            RouteMeasurementSample(2, 1001.0),
            RouteMeasurementSample(3, 999.0),
        ]

    acquisition.read = read
    events = _Events()
    events.save = lambda record: order.append("csv")
    events.measurement_finished = lambda event: order.append("result")
    execution = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    )
    result = execution.execute(
        RoutePointRequest(
            point=_point(),
            position=1,
            total=1,
            route_offset_xy=(0.0, 0.0),
            mode=PointMode(measure=True, photo=False, autofocus=False),
            measurement=PointMeasurementSettings(
                count=3,
                initial_count=1,
                max_relative_rms=None,
                quality_limits=RouteContactQualityLimits(),
                nplc_label="1 PLC",
                measurement_type="resistance",
            ),
            seek=PointSeekSettings(enabled=False, step_mm=0.002, max_total_mm=0.0),
            contact_settle_s=0.0,
            photo_settle_s=0.0,
        )
    )

    assert result.record_saved is True
    assert result.measurements_saved == 1
    assert order == ["lift_start", "lift_wait", "csv", "result"]


def test_failed_background_lift_uses_fallback_before_result() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    order: list[str] = []
    control = _Control()
    motion = _Motion()

    class _FailedLift:
        def wait(self) -> None:
            order.append("lift_failed")
            raise RuntimeError("background lift failed")

    motion.start_lift = lambda _feedrate=None: _FailedLift()
    motion.fallback_lift = lambda _feedrate=None: order.append("fallback")
    acquisition = _Acquisition()
    acquisition.read = lambda count, start_index, after_measurement=None: [
        RouteMeasurementSample(1, 1000.0),
        RouteMeasurementSample(2, 1000.0),
        RouteMeasurementSample(3, 1000.0),
    ]
    events = _Events()
    events.save = lambda record: order.append("csv")
    events.measurement_finished = lambda event: order.append("result")
    execution = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    )
    request = RoutePointRequest(
        point=_point(),
        position=1,
        total=1,
        route_offset_xy=(0.0, 0.0),
        mode=PointMode(measure=True, photo=False, autofocus=False),
        measurement=PointMeasurementSettings(
            count=3,
            initial_count=1,
            max_relative_rms=None,
            quality_limits=RouteContactQualityLimits(),
            nplc_label="",
            measurement_type="",
        ),
        seek=PointSeekSettings(enabled=False, step_mm=0.002, max_total_mm=0.0),
        contact_settle_s=0.0,
        photo_settle_s=0.0,
    )

    result = execution.execute(request)

    assert result.pending_cleanup is False
    assert order == ["lift_failed", "fallback", "csv", "result"]


def test_failed_background_and_fallback_lift_defers_recording() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    control = _Control()
    motion = _Motion()

    class _FailedLift:
        def wait(self) -> None:
            raise RuntimeError("background lift failed")

    motion.start_lift = lambda _feedrate=None: _FailedLift()
    motion.fallback_lift = lambda _feedrate=None: (_ for _ in ()).throw(RuntimeError("fallback failed"))
    acquisition = _Acquisition()
    acquisition.read = lambda count, start_index, after_measurement=None: [
        RouteMeasurementSample(1, 1000.0),
    ]
    events = _Events()
    execution = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    )
    request = RoutePointRequest(
        point=_point(),
        position=1,
        total=1,
        route_offset_xy=(0.0, 0.0),
        mode=PointMode(measure=True, photo=False, autofocus=False),
        measurement=PointMeasurementSettings(
            count=1,
            initial_count=1,
            max_relative_rms=None,
            quality_limits=RouteContactQualityLimits(),
            nplc_label="",
            measurement_type="",
        ),
        seek=PointSeekSettings(enabled=False, step_mm=0.002, max_total_mm=0.0),
        contact_settle_s=0.0,
        photo_settle_s=0.0,
    )

    result = execution.execute(request)

    assert result.pending_cleanup is True
    assert result.result_emitted is False
    assert not {"csv", "result"}.intersection(events.calls)


def test_seek_exhaustion_is_saved_with_named_seek_metadata() -> None:
    from probe_station_gui.route.point_execution import (
        PointMeasurementSettings,
        PointMode,
        PointSeekSettings,
        RoutePointExecution,
        RoutePointRequest,
    )

    control = _Control()
    motion = _Motion()
    acquisition = _Acquisition()
    batches = iter(
        [
            [
                RouteMeasurementSample(1, 1000.0),
                RouteMeasurementSample(2, 1400.0),
                RouteMeasurementSample(3, 800.0),
            ],
            [
                RouteMeasurementSample(1, 1000.0),
                RouteMeasurementSample(2, 1500.0),
                RouteMeasurementSample(3, 700.0),
            ],
            [
                RouteMeasurementSample(1, 1000.0),
                RouteMeasurementSample(2, 1600.0),
                RouteMeasurementSample(3, 600.0),
            ],
        ]
    )
    acquisition.read = lambda count, start_index, after_measurement=None: next(batches)
    events = _Events()
    execution = RoutePointExecution(
        motion=motion,
        acquisition=acquisition,
        photo=_Photo(motion, control),
        control=control,
        events=events,
    )
    result = execution.execute(
        RoutePointRequest(
            point=_point(),
            position=1,
            total=1,
            route_offset_xy=(0.0, 0.0),
            mode=PointMode(measure=True, photo=False, autofocus=False),
            measurement=PointMeasurementSettings(
                count=3,
                initial_count=3,
                max_relative_rms=None,
                quality_limits=RouteContactQualityLimits(
                    max_mad_sigma_ohm=1.0,
                    max_p95_abs_step_ohm=1.0,
                    max_relative_mad_sigma=0.001,
                    max_relative_p95_abs_step=0.001,
                ),
                nplc_label="1 PLC",
                measurement_type="resistance",
                confirm_each_point=True,
            ),
            seek=PointSeekSettings(enabled=True, step_mm=0.002, max_total_mm=0.004),
            contact_settle_s=0.0,
            photo_settle_s=0.0,
        )
    )

    assert result.record_saved is True
    assert result.save_exhausted_bad_contact is True
    assert result.quality_rejected is False
    assert result.seek is not None
    assert result.seek.status == "not_found"
    assert result.seek.attempts == 2
    assert result.seek.depth_below_down_mm == 0.004
    assert result.seek.step_mm == 0.002
    assert result.seek.max_depth_mm == 0.004
    assert [call for call in motion.calls if isinstance(call, tuple) and call[0] == "press"] == [
        ("press", 0.002, 0.002),
        ("press", 0.004, 0.002),
    ]
