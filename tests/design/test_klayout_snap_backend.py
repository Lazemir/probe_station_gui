from __future__ import annotations

from pathlib import Path
import time

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    SNAP_UNAVAILABLE_CANCELLED,
    SNAP_UNAVAILABLE_CANDIDATE_BUDGET,
    SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE,
    SNAP_UNAVAILABLE_OUTSIDE_BOUNDS,
    SNAP_UNAVAILABLE_SHAPE_BUDGET,
    SNAP_UNAVAILABLE_TIME_BUDGET,
    SnapRequest,
    SnapWorkBudget,
)
from probe_station_gui.design.klayout_snap_worker import (
    _KLayoutSnapBackend,
    _collect_snap_geometry,
)


def _config() -> KLayoutConfig:
    return KLayoutConfig(
        path=Path("layout.gds"),
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 100.0, 100.0),
        display_bounds=(0.0, 0.0, 100.0, 100.0),
        rotation_quarter_turns=0,
        generation=1,
    )


def _shape(points, *, closed: bool = True):
    return ((tuple(points), closed),)


def _budget(*, shapes=100, candidates=100, elapsed_ms=1_000.0):
    return SnapWorkBudget(shapes, candidates, elapsed_ms)


def _assert_abort(result, reason: str) -> None:
    assert result.unavailable_reason == reason
    assert result.vertices == ()
    assert result.segments == ()


def _assert_following_normal_request_runs() -> None:
    result = _collect_snap_geometry(
        [_shape([(0.0, 0.0), (1.0, 0.0)], closed=False)],
        _budget(),
        clock=time.perf_counter,
        is_cancelled=lambda: False,
    )
    assert result.unavailable_reason is None
    assert result.vertices == ((0.0, 0.0), (1.0, 0.0))
    assert result.segments == (((0.0, 0.0), (1.0, 0.0)),)


def test_shape_budget_discards_partial_snap() -> None:
    result = _collect_snap_geometry(
        [_shape([(0.0, 0.0)]), _shape([(1.0, 0.0)])],
        _budget(shapes=1),
        clock=time.perf_counter,
        is_cancelled=lambda: False,
    )
    _assert_abort(result, SNAP_UNAVAILABLE_SHAPE_BUDGET)
    _assert_following_normal_request_runs()


def test_candidate_budget_stops_inside_one_huge_contour() -> None:
    result = _collect_snap_geometry(
        [_shape((float(index), 0.0) for index in range(100))],
        _budget(candidates=3),
        clock=time.perf_counter,
        is_cancelled=lambda: False,
    )
    _assert_abort(result, SNAP_UNAVAILABLE_CANDIDATE_BUDGET)
    _assert_following_normal_request_runs()


class _StepClock:
    def __init__(self, step: float) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def test_elapsed_budget_discards_partial_snap() -> None:
    result = _collect_snap_geometry(
        [_shape([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])],
        _budget(elapsed_ms=0.5),
        clock=_StepClock(0.0002),
        is_cancelled=lambda: False,
    )
    _assert_abort(result, SNAP_UNAVAILABLE_TIME_BUDGET)
    _assert_following_normal_request_runs()


def test_cancellation_discards_partial_snap() -> None:
    checks = 0

    def is_cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    result = _collect_snap_geometry(
        [_shape([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])],
        _budget(),
        clock=time.perf_counter,
        is_cancelled=is_cancelled,
    )
    _assert_abort(result, SNAP_UNAVAILABLE_CANCELLED)
    _assert_following_normal_request_runs()


def test_backend_budget_abort_closes_shape_stream() -> None:
    closed: list[bool] = []
    backend = _KLayoutSnapBackend(clock=time.perf_counter)

    def close_aware_stream(_config, _source_box):
        try:
            yield _shape([(0.0, 0.0)])
            yield _shape([(1.0, 0.0)])
        finally:
            closed.append(True)

    backend._iter_shape_contours = close_aware_stream

    response = backend.snap(
        SnapRequest(
            1,
            _config(),
            (1.0, 1.0),
            1.0,
            budget=_budget(shapes=1),
        ),
        is_cancelled=lambda: False,
    )

    assert response.unavailable_reason == SNAP_UNAVAILABLE_SHAPE_BUDGET
    assert closed == [True]


def _backend_with_counted_stream():
    calls: list[object] = []
    backend = _KLayoutSnapBackend(clock=time.perf_counter)

    def stream(_config, source_box):
        calls.append(source_box)
        return iter([_shape([(1.0, 1.0), (2.0, 1.0)], closed=False)])

    backend._iter_shape_contours = stream
    return backend, calls


def test_outside_query_does_not_construct_iterator() -> None:
    backend, calls = _backend_with_counted_stream()
    response = backend.snap(
        SnapRequest(1, _config(), (1_000.0, 1_000.0), 2.0),
        is_cancelled=lambda: False,
    )
    assert response.unavailable_reason == SNAP_UNAVAILABLE_OUTSIDE_BOUNDS
    assert response.result.mode == "free"
    assert response.result.segment_start is None
    assert response.result.segment_end is None
    assert calls == []


def test_extreme_radius_is_rejected_before_recursive_traversal_and_normal_request_runs() -> None:
    backend, calls = _backend_with_counted_stream()
    started = time.perf_counter()
    extreme = backend.snap(
        SnapRequest(1, _config(), (50.0, 50.0), 10_000_000.0),
        is_cancelled=lambda: False,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1
    assert extreme.unavailable_reason == SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE
    assert extreme.result.mode == "free"
    assert calls == []

    normal = backend.snap(
        SnapRequest(2, _config(), (1.0, 1.0), 0.5),
        is_cancelled=lambda: False,
    )
    assert normal.unavailable_reason is None
    assert normal.result.mode == "vertex"
    assert normal.result.point == (1.0, 1.0)
    assert len(calls) == 1
