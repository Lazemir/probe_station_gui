from __future__ import annotations

from probe_station_gui.stage import sample_handling
from probe_station_gui.stage.controller import StageControllerError


class _FakeStageController:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[object] = []
        self.fail = fail

    def begin_external_task(self, label: str) -> None:
        self.calls.append(("begin", label))

    def finish_external_task(self) -> None:
        self.calls.append(("finish",))

    def run_external_needles_action(self, action: str, feedrate: float) -> None:
        self.calls.append(("needles", action, feedrate))
        if self.fail is not None:
            raise self.fail

    def run_external_move_to_xy(
        self,
        x_mm: float,
        y_mm: float,
        *,
        feedrate: float,
    ) -> None:
        self.calls.append(("move_xy", x_mm, y_mm, feedrate))

    def run_external_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        feedrate: float,
    ) -> None:
        self.calls.append(("move_axes", targets, feedrate))


def test_sample_focus_cache_is_objective_scoped_and_falls_back_to_latest_z() -> None:
    cache: dict[str, float] = {}

    assert sample_handling.remember_sample_focus(
        cache,
        raw_objective_name=" x5 ",
        latest_position=(0.0, 0.0, 1.25),
    ) == 1.25
    assert cache == {"X5": 1.25}
    assert sample_handling.sample_load_focus_z(
        cache,
        raw_objective_name="x5",
        latest_position=(0.0, 0.0, 9.0),
    ) == 1.25
    assert sample_handling.sample_load_focus_z(
        cache,
        raw_objective_name="x20",
        latest_position=(0.0, 0.0, 9.0),
    ) == 9.0


def test_sample_start_decision_preserves_guard_messages() -> None:
    disconnected = sample_handling.sample_start_decision(
        "load",
        stage_ready=False,
        sample_active=False,
        cancelable_operation=False,
    )
    busy = sample_handling.sample_start_decision(
        "unload",
        stage_ready=True,
        sample_active=True,
        cancelable_operation=False,
    )

    assert disconnected.accepted is False
    assert disconnected.status_message == (
        "Stage is not connected; sample action not started."
    )
    assert busy.accepted is False
    assert busy.status_message == "Stage is busy. Ignoring sample unload request."
    assert sample_handling.sample_start_decision(
        "load",
        stage_ready=True,
        sample_active=False,
        cancelable_operation=False,
    ).accepted is True


def test_sample_unload_sequence_preserves_stage_order_and_messages() -> None:
    stage = _FakeStageController()
    statuses: list[str] = []
    finished: list[tuple[object, ...]] = []

    sample_handling.run_sample_unload(
        stage,
        xy_feedrate=900.0,
        needle_feedrate=7.0,
        emit_status=statuses.append,
        emit_finished=lambda *args: finished.append(args),
    )

    assert statuses == [
        "Sample unload: raising needles.",
        "Sample unload: moving to X=-32.000, Y=32.000.",
    ]
    assert stage.calls == [
        ("begin", "sample unload"),
        ("needles", "raise", 7.0),
        ("move_xy", -32.0, 32.0, 900.0),
        ("finish",),
    ]
    assert finished == [
        (
            True,
            "Sample unloaded at X=-32.000, Y=32.000; needles are raised.",
            False,
            None,
        )
    ]


def test_sample_load_sequence_moves_to_cached_focus_when_available() -> None:
    stage = _FakeStageController()
    statuses: list[str] = []
    finished: list[tuple[object, ...]] = []

    sample_handling.run_sample_load(
        stage,
        focus_z_mm=1.2345,
        xy_feedrate=800.0,
        focus_feedrate=90.0,
        needle_feedrate=7.0,
        emit_status=statuses.append,
        emit_finished=lambda *args: finished.append(args),
    )

    assert statuses == [
        "Sample load: raising needles.",
        "Sample load: moving to X=0.000, Y=0.000.",
        "Sample load: moving Z to last focus 1.2345 mm.",
    ]
    assert stage.calls == [
        ("begin", "sample load"),
        ("needles", "raise", 7.0),
        ("move_xy", 0.0, 0.0, 800.0),
        ("move_axes", {"Z": 1.2345}, 90.0),
        ("finish",),
    ]
    assert finished == [
        (True, "Sample loaded at X=0.000, Y=0.000, Z=1.2345.", True, 1.2345)
    ]


def test_sample_sequences_accept_owner_coordinate_overrides() -> None:
    stage = _FakeStageController()
    statuses: list[str] = []
    finished: list[tuple[object, ...]] = []

    sample_handling.run_sample_unload(
        stage,
        xy_feedrate=800.0,
        needle_feedrate=7.0,
        emit_status=statuses.append,
        emit_finished=lambda *args: finished.append(args),
        unload_x_mm=-5.0,
        unload_y_mm=6.0,
    )

    assert statuses[-1] == "Sample unload: moving to X=-5.000, Y=6.000."
    assert ("move_xy", -5.0, 6.0, 800.0) in stage.calls
    assert finished == [
        (True, "Sample unloaded at X=-5.000, Y=6.000; needles are raised.", False, None)
    ]

    load_stage = _FakeStageController()
    load_finished: list[tuple[object, ...]] = []
    sample_handling.run_sample_load(
        load_stage,
        focus_z_mm=None,
        xy_feedrate=900.0,
        focus_feedrate=90.0,
        needle_feedrate=8.0,
        emit_status=lambda _message: None,
        emit_finished=lambda *args: load_finished.append(args),
        load_x_mm=3.0,
        load_y_mm=4.0,
    )

    assert ("move_xy", 3.0, 4.0, 900.0) in load_stage.calls
    assert load_finished == [
        (
            True,
            "Sample loaded at X=3.000, Y=4.000; last focus is unavailable.",
            True,
            None,
        )
    ]


def test_sample_load_sequence_reports_unavailable_focus_and_failures() -> None:
    stage = _FakeStageController()
    finished: list[tuple[object, ...]] = []

    sample_handling.run_sample_load(
        stage,
        focus_z_mm=None,
        xy_feedrate=800.0,
        focus_feedrate=90.0,
        needle_feedrate=7.0,
        emit_status=lambda _message: None,
        emit_finished=lambda *args: finished.append(args),
    )

    assert finished == [
        (
            True,
            "Sample loaded at X=0.000, Y=0.000; last focus is unavailable.",
            True,
            None,
        )
    ]

    failed_stage = _FakeStageController(fail=StageControllerError("boom"))
    failed: list[tuple[object, ...]] = []
    sample_handling.run_sample_unload(
        failed_stage,
        xy_feedrate=800.0,
        needle_feedrate=7.0,
        emit_status=lambda _message: None,
        emit_finished=lambda *args: failed.append(args),
    )
    assert failed == [(False, "Sample unload failed: boom", False, None)]
    assert failed_stage.calls[-1] == ("finish",)


def test_sample_autofocus_prompt_handles_success_and_invalid_focus() -> None:
    assert sample_handling.sample_autofocus_prompt(
        success=False,
        offer_autofocus=True,
        focus_z_mm=1.0,
    ).should_prompt is False
    assert sample_handling.sample_autofocus_prompt(
        success=True,
        offer_autofocus=True,
        focus_z_mm=1.23456,
    ).prompt == "Sample is near Z=1.2346 mm. Run autofocus now?"
    assert sample_handling.sample_autofocus_prompt(
        success=True,
        offer_autofocus=True,
        focus_z_mm="bad",
    ).prompt == "Run autofocus now?"
