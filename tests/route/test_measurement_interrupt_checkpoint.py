from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from probe_station_gui.route.measurement import (
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    RouteMeasurementRunner,
)
from probe_station_gui.route.point_execution_adapters import RouteMeasurementEvents
from tests.route.measurement_test_support import (
    _FakeLCR,
    _FakeStage,
    _point,
    _read_csv_rows_if_exists,
)


def _start_runner(
    runner: RouteMeasurementRunner,
) -> tuple[threading.Thread, list[tuple[bool, str]]]:
    finished: list[tuple[bool, str]] = []
    thread = threading.Thread(
        target=lambda: finished.append(runner.run()),
        daemon=True,
    )
    thread.start()
    return thread, finished


def _finish_waiting_runner(
    runner: RouteMeasurementRunner,
    thread: threading.Thread,
) -> None:
    if thread.is_alive():
        assert runner.submit_confirmation("skip")
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def _photo_then_measure_runner(
    *,
    csv_path: Path,
    points: int,
    stage: _FakeStage,
    lcr: _FakeLCR,
    statuses: list[str],
    photos: list[int],
    contacts: list[int],
    confirm_each_point: bool = False,
) -> RouteMeasurementRunner:
    return RouteMeasurementRunner(
        points=[_point(index) for index in range(1, points + 1)],
        csv_path=csv_path,
        stage_controller=stage,
        lcr_controller=lcr,
        needle_feedrate=75.0,
        operation_mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        confirm_each_point=confirm_each_point,
        events=RouteMeasurementEvents(
            photo=lambda point, _position, _total, _focus: (
                photos.append(int(point.index)) or f"photo-{point.index}.png"
            ),
            pre_contact_photo=lambda point, _position, _total: contacts.append(
                int(point.index)
            ),
            status=statuses.append,
        ),
        photo_settle_s=0.0,
        contact_settle_s=0.0,
    )


@dataclass(frozen=True)
class _InterruptScenario:
    runner: RouteMeasurementRunner
    csv_path: Path
    stage: _FakeStage
    lcr: _FakeLCR
    statuses: list[str]
    photos: list[int]
    contacts: list[int]


def _interrupt_scenario(
    tmp_path: Path,
    *,
    points: int,
    confirm_each_point: bool = False,
) -> _InterruptScenario:
    csv_path = tmp_path / "route.csv"
    stage = _FakeStage()
    lcr = _FakeLCR([3.0 + 2.0 * index for index in range(1, points + 1)])
    statuses: list[str] = []
    photos: list[int] = []
    contacts: list[int] = []
    runner = _photo_then_measure_runner(
        csv_path=csv_path,
        points=points,
        stage=stage,
        lcr=lcr,
        statuses=statuses,
        photos=photos,
        contacts=contacts,
        confirm_each_point=confirm_each_point,
    )
    return _InterruptScenario(
        runner=runner,
        csv_path=csv_path,
        stage=stage,
        lcr=lcr,
        statuses=statuses,
        photos=photos,
        contacts=contacts,
    )


def _assert_no_point_side_effects(
    *,
    csv_path: Path,
    stage: _FakeStage,
    lcr: _FakeLCR,
    photos: list[int],
    contacts: list[int],
) -> None:
    assert photos == []
    assert contacts == []
    assert not any(
        call[0] == "needles" and call[1] == "lower"
        for call in stage.calls
        if isinstance(call, tuple)
    )
    assert lcr.values == [5.0]
    assert _read_csv_rows_if_exists(csv_path) == []


def test_interrupt_before_first_point_reaches_correction_checkpoint(
    tmp_path: Path,
) -> None:
    scenario = _interrupt_scenario(tmp_path, points=1)
    scenario.runner.request_current_point_correction()

    thread, finished = _start_runner(scenario.runner)
    try:
        assert scenario.runner.wait_until_waiting(timeout_s=2.0)
        assert finished == []
        assert any(
            "interrupted; correct position" in item for item in scenario.statuses
        )
        _assert_no_point_side_effects(
            csv_path=scenario.csv_path,
            stage=scenario.stage,
            lcr=scenario.lcr,
            photos=scenario.photos,
            contacts=scenario.contacts,
        )
    finally:
        _finish_waiting_runner(scenario.runner, thread)

    assert finished[0][0] is True


def test_interrupt_wins_when_pause_is_already_pending(tmp_path: Path) -> None:
    scenario = _interrupt_scenario(
        tmp_path,
        points=1,
        confirm_each_point=True,
    )
    scenario.runner.request_pause_after_current_point()
    scenario.runner.request_current_point_correction()

    thread, finished = _start_runner(scenario.runner)
    try:
        assert scenario.runner.wait_until_waiting(timeout_s=2.0)
        assert finished == []
        assert any(
            "interrupted; correct position" in item for item in scenario.statuses
        )
        assert not any("point 1/1 paused" in item for item in scenario.statuses)
        _assert_no_point_side_effects(
            csv_path=scenario.csv_path,
            stage=scenario.stage,
            lcr=scenario.lcr,
            photos=scenario.photos,
            contacts=scenario.contacts,
        )
    finally:
        _finish_waiting_runner(scenario.runner, thread)

    assert finished[0][0] is True


def test_acknowledged_interrupt_is_clear_before_the_next_point(
    tmp_path: Path,
) -> None:
    scenario = _interrupt_scenario(tmp_path, points=2)
    scenario.runner.request_current_point_correction()

    thread, finished = _start_runner(scenario.runner)
    try:
        assert scenario.runner.wait_until_waiting(timeout_s=2.0)
        assert scenario.runner.current_point_correction_requested() is False
        assert scenario.runner.submit_confirmation("skip")
        thread.join(timeout=2.0)
    finally:
        if thread.is_alive():
            scenario.runner.stop()
            thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert finished[0][0] is True
    assert scenario.runner.current_point_correction_requested() is False
    assert scenario.photos == [2]
    assert scenario.contacts == [2]
    assert scenario.lcr.values == [7.0]
    assert len(_read_csv_rows_if_exists(scenario.csv_path)) == 1
