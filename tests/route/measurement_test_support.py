import csv
import math
import threading
from pathlib import Path

from probe_station_gui.route.measurement import RouteMeasurementPoint


class _FakeStage:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.axis_a_lowering_mm = math.nan

    def begin_external_task(self, label: str) -> None:
        self.calls.append(("begin", label))

    def finish_external_task(self) -> None:
        self.calls.append(("finish",))

    def run_external_move_to_xy(self, x_mm: float, y_mm: float) -> str:
        self.calls.append(("move", x_mm, y_mm))
        return "moved"

    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> str:
        self.calls.append(("needles", action, feedrate))
        self.axis_a_lowering_mm = {
            "lower": 1.0,
            "lift": 0.0,
            "raise": 0.0,
        }.get(action, self.axis_a_lowering_mm)
        return f"{action} done"

    def run_external_needles_adjust(
        self,
        step_mm: float,
        feedrate: float | None = None,
    ) -> str:
        self.calls.append(("adjust", step_mm, feedrate))
        if math.isfinite(self.axis_a_lowering_mm):
            self.axis_a_lowering_mm += abs(float(step_mm))
        return "adjusted"

    def run_external_needles_lower_to_depth_below_down(
        self,
        depth_mm: float,
        feedrate: float | None = None,
    ) -> str:
        self.calls.append(("lower_to_depth", depth_mm, feedrate))
        self.axis_a_lowering_mm = 1.0 + float(depth_mm)
        return "lowered to depth"

    def run_external_local_autofocus(self, *, range_mm: float, step_mm=None) -> str:
        self.calls.append(("autofocus", range_mm, step_mm))
        return "local autofocus done"

    def latest_axis_a_lowering(self) -> float:
        return self.axis_a_lowering_mm


class _NotifyingStage(_FakeStage):
    def __init__(self) -> None:
        super().__init__()
        self.changed = threading.Condition()

    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> str:
        result = super().run_external_needles_action(action, feedrate)
        with self.changed:
            self.changed.notify_all()
        return result

    def wait_for_needles_action(self, action: str) -> bool:
        with self.changed:
            return self.changed.wait_for(
                lambda: any(
                    call[0] == "needles" and call[1] == action
                    for call in self.calls
                    if isinstance(call, tuple)
                ),
                timeout=2.0,
            )


class _FakeLCR:
    def __init__(self, values: list[float], on_read=None) -> None:
        self.values = list(values)
        self.on_read = on_read
        self.abort_count = 0

    def read_primary_value_now(self) -> float:
        value = self.values.pop(0)
        if self.on_read is not None:
            self.on_read()
        return value

    def abort_current_measurement(self) -> None:
        self.abort_count += 1


class _FakeRouteLCR:
    def __init__(self, measurements: list[dict[str, object]]) -> None:
        self.measurements = list(measurements)

    def read_route_measurement_now(self) -> dict[str, object]:
        return dict(self.measurements.pop(0))


class _FakeBatchRouteLCR:
    def __init__(self, measurements: list[dict[str, object]]) -> None:
        self.measurements = list(measurements)
        self.batch_counts: list[int] = []

    def read_route_measurement_batch_now(self, count: int) -> list[dict[str, object]]:
        self.batch_counts.append(int(count))
        batch = self.measurements[:count]
        del self.measurements[:count]
        return [dict(item) for item in batch]


class _EventStage(_FakeStage):
    def __init__(self, events: list[object]) -> None:
        super().__init__()
        self.events = events

    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> str:
        self.events.append(("needles", action))
        return super().run_external_needles_action(action, feedrate)


class _OutputTrackingBatchRouteLCR(_FakeBatchRouteLCR):
    def __init__(
        self,
        measurements: list[dict[str, object]],
        events: list[object],
    ) -> None:
        super().__init__(measurements)
        self.events = events
        self.output_active = False
        self.prepare_calls: list[tuple[int, int | None]] = []

    def output(self, enabled: bool = True):
        lcr = self
        requested = bool(enabled)

        class _OutputContext:
            def __enter__(self):
                lcr.output_active = requested
                lcr.events.append(("output", requested))
                return lcr

            def __exit__(self, _exc_type, _exc, _tb) -> None:
                lcr.output_active = False
                lcr.events.append(("output", False))

        return _OutputContext()

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        self.prepare_calls.append((int(count), source_list_count))
        self.events.append(("prepare", int(count), source_list_count))

    def read_route_measurement_batch_now(
        self,
        count: int,
        *,
        after_measurement=None,
    ) -> list[dict[str, object]]:
        self.events.append(("read", int(count), self.output_active))
        if after_measurement is not None:
            after_measurement()
        return super().read_route_measurement_batch_now(count)


class _PreparedBatchRouteLCR(_FakeBatchRouteLCR):
    def __init__(self, measurements: list[dict[str, object]]) -> None:
        super().__init__(measurements)
        self.prepare_calls: list[tuple[int, int | None]] = []
        self.prepare_started = threading.Event()
        self.prepare_finished = threading.Event()
        self.allow_prepare_finish = threading.Event()
        self.lift_started = threading.Event()
        self.read_after_measurement: list[bool] = []

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        self.prepare_calls.append((int(count), source_list_count))
        self.prepare_started.set()
        self.prepare_finished.set()

    def read_route_measurement_batch_now(
        self,
        count: int,
        *,
        after_measurement=None,
    ) -> list[dict[str, object]]:
        self.read_after_measurement.append(after_measurement is not None)
        if after_measurement is not None:
            after_measurement()
            self.lift_started.wait(timeout=2.0)
        return super().read_route_measurement_batch_now(count)


class _FailingPreparedBatchRouteLCR(_FakeBatchRouteLCR):
    def __init__(self, exc: Exception) -> None:
        super().__init__([])
        self.exc = exc
        self.prepare_calls: list[tuple[int, int | None]] = []

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        self.prepare_calls.append((int(count), source_list_count))
        raise self.exc


class _PrepareAwareStage(_FakeStage):
    def __init__(self, lcr: _PreparedBatchRouteLCR) -> None:
        super().__init__()
        self.lcr = lcr
        self.prepare_started_before_lower = False

    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> str:
        if action == "lower":
            self.prepare_started_before_lower = self.lcr.prepare_finished.wait(
                timeout=2.0
            )
        result = super().run_external_needles_action(action, feedrate)
        if action == "lift":
            self.lcr.lift_started.set()
        return result


class _PausingBatchRouteLCR(_FakeBatchRouteLCR):
    def __init__(self, measurements: list[dict[str, object]], pause_on_batch) -> None:
        super().__init__(measurements)
        self.pause_on_batch = pause_on_batch

    def read_route_measurement_batch_now(self, count: int) -> list[dict[str, object]]:
        batch = super().read_route_measurement_batch_now(count)
        self.pause_on_batch(len(self.batch_counts))
        return batch


class _OpeningLCR(_FakeLCR):
    def __init__(self, values: list[float]) -> None:
        super().__init__(values)
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True


def _point(index: int) -> RouteMeasurementPoint:
    return RouteMeasurementPoint(
        index=index,
        point_id=f"p{index:03d}",
        label=f"P{index:03d}",
        design_center=(100.0 + index, 200.0),
        stage_xy=(float(index), 10.0 + float(index)),
        needle_1_design=(101.0 + index, 201.0),
        needle_2_design=(99.0 + index, 199.0),
    )


def _read_csv_rows_if_exists(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
