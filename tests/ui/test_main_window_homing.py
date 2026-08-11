from __future__ import annotations

from types import SimpleNamespace

from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow


class _StageController:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.busy = False
        self.owner: SimpleNamespace | None = None
        self.home_axis_result = True
        self.latest_position = (1.0, 2.0, 3.0)
        self.emit_started_synchronously = False

    def is_busy(self) -> bool:
        return self.busy

    def request_home_axis(self, axis: str) -> bool:
        self.events.append(("home_axis", axis))
        if self.emit_started_synchronously and self.owner is not None:
            homing_ui.on_homing_action_started(self.owner, axis)
        return self.home_axis_result

    def request_home_all(self) -> bool:
        self.events.append(("home_all",))
        return True

    def latest_stage_position(self) -> tuple[float, float, float]:
        self.events.append(("latest_position",))
        return self.latest_position


def _owner(events: list[object]) -> SimpleNamespace:
    stage_controller = _StageController(events)
    owner = SimpleNamespace(
        STAGE_AXIS_NAMES=("X", "Y", "Z", "A", "B"),
        _stage_limit_axes=set(),
        _manual_jog_prediction=SimpleNamespace(
            prediction_available=lambda _now: False,
            stage_position=None,
        ),
        _planned_move_stage_xy=None,
        _planned_move_started_at=None,
        _planned_move_waiting_for_fresh_status=False,
        _pending_homing_axes=[],
        _homing_active_key=None,
        _stage_motion_axes=set(),
        _stage_motion_blink_dimmed=False,
        _stage_motion_blink_timer=SimpleNamespace(
            isActive=lambda: False,
            start=lambda: events.append(("blink_start",)),
            stop=lambda: events.append(("blink_stop",)),
        ),
        _coordinate_targets=SimpleNamespace(has_active_move=lambda: False),
        joystick_panel=SimpleNamespace(
            set_pending_homing_actions=lambda axes: events.append(
                ("pending_ui", tuple(sorted(axes)))
            )
        ),
        stage_controller=stage_controller,
        _controller_latest_state_blocks_motion=lambda: False,
        _position_with_stage_xy=lambda xy: (xy[0], xy[1], 99.0),
        _update_stage_position_display=lambda position: events.append(
            ("display", position)
        ),
        _update_stage_coordinate_apply_state=lambda: events.append(("apply_state",)),
        _invalidate_design_registration=lambda message: events.append(
            ("invalidate", message)
        ),
    )
    stage_controller.owner = owner
    return owner


def test_queue_starts_first_axis_and_preserves_remainder_when_start_signal_is_sync(
    monkeypatch,
) -> None:
    events: list[object] = []
    timer_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        homing_ui,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda delay, callback: timer_calls.append((delay, callback))
        ),
    )
    owner = _owner(events)
    owner.stage_controller.emit_started_synchronously = True

    homing_ui.queue_or_start_homing_axes(owner, ["x", "z"])

    assert owner._homing_active_key == "X"
    assert owner._pending_homing_axes == ["Z"]
    assert ("home_axis", "X") in events
    assert ("pending_ui", ("Z",)) in events
    assert timer_calls == []


def test_axis_home_request_normalizes_axis_before_queue(monkeypatch) -> None:
    events: list[object] = []
    owner = _owner(events)
    monkeypatch.setattr(
        homing_ui,
        "queue_or_start_homing_axes",
        lambda _owner, axes: events.append(("queue", axes)),
    )

    homing_ui.request_home_axis_from_ui(owner, " x ")
    homing_ui.request_home_axis_from_ui(owner, "bad")

    assert events == [("queue", ["X"])]


def test_home_request_clears_deferred_exact_step() -> None:
    events: list[object] = []
    owner = _owner(events)
    owner._clear_exact_step_targets = lambda: events.append(("clear_exact_step",))

    homing_ui.request_home_axis_from_ui(owner, "X")

    assert events[0] == ("clear_exact_step",)
    assert ("home_axis", "X") in events


def test_queue_defers_when_controller_is_busy(monkeypatch) -> None:
    events: list[object] = []
    timer_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        homing_ui,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda delay, callback: timer_calls.append((delay, callback))
        ),
    )
    owner = _owner(events)
    owner.stage_controller.busy = True

    homing_ui.queue_or_start_homing_axes(owner, ["z"])

    assert owner._pending_homing_axes == ["Z"]
    assert ("home_axis", "Z") not in events
    assert len(timer_calls) == 1
    assert timer_calls[0][0] == homing_ui.HOMING_RETRY_DELAY_MS
    assert callable(timer_calls[0][1])


def test_failed_homing_finish_clears_queue_without_scheduling_next(monkeypatch) -> None:
    events: list[object] = []
    timer_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        homing_ui,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda delay, callback: timer_calls.append((delay, callback))
        ),
    )
    owner = _owner(events)
    owner._homing_active_key = "X"
    owner._pending_homing_axes = ["Z"]
    owner._stage_motion_axes = {"X"}

    homing_ui.on_homing_action_finished(owner, False, "failed", "X")

    assert owner._homing_active_key is None
    assert owner._pending_homing_axes == []
    assert owner._stage_motion_axes == set()
    assert timer_calls == []


def test_successful_homing_finish_refreshes_authority_without_staling_registration(
    monkeypatch,
) -> None:
    events: list[object] = []
    timer_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        homing_ui,
        "QTimer",
        SimpleNamespace(
            singleShot=lambda delay, callback: timer_calls.append((delay, callback))
        ),
    )
    monkeypatch.setattr(
        coordinate_flow,
        "observe_coordinate_authority",
        lambda _owner: events.append(("authority",)),
    )
    owner = _owner(events)
    owner._homing_active_key = "X"
    owner._pending_homing_axes = ["Z"]
    owner._stage_motion_axes = {"X"}

    homing_ui.on_homing_action_finished(owner, True, "ok", "X")

    assert owner._homing_active_key is None
    assert not any(event[0] == "invalidate" for event in events)
    assert ("authority",) in events
    assert owner._stage_motion_axes == set()
    assert len(timer_calls) == 1
    assert timer_calls[0][0] == 0
    assert callable(timer_calls[0][1])


def test_homing_status_loss_immediately_refreshes_coordinate_authority(
    monkeypatch,
) -> None:
    events: list[object] = []
    owner = _owner(events)
    monkeypatch.setattr(
        homing_ui.stage_position_panel,
        "update_stage_position_display",
        lambda _owner, position: events.append(("display", position)),
    )
    monkeypatch.setattr(
        coordinate_flow,
        "observe_coordinate_authority",
        lambda _owner: events.append(("authority",)),
    )

    homing_ui.on_homing_status_changed(owner, {"X"})

    assert events[-1] == ("authority",)
    assert events.count(("authority",)) == 1


def test_limit_axis_update_normalizes_axes_and_preserves_manual_jog_prediction(
    monkeypatch,
) -> None:
    events: list[object] = []
    owner = _owner(events)
    owner._manual_jog_prediction = SimpleNamespace(
        prediction_available=lambda _now: True,
        stage_position=(9.0, 8.0, 7.0),
    )
    monkeypatch.setattr(
        homing_ui.stage_position_panel,
        "update_stage_position_display",
        lambda _owner, position: events.append(("display", position)),
    )

    homing_ui.on_limit_axes_changed(owner, ["x", "bad", "B"])

    assert owner._stage_limit_axes == {"X", "B"}
    assert ("display", (9.0, 8.0, 7.0)) in events
    assert ("latest_position",) not in events


def test_limit_axis_update_restores_selected_coordinate_display(monkeypatch) -> None:
    events: list[object] = []
    owner = _owner(events)
    owner._stage_axis_display_values = {"X": 1.0}

    def render_raw(_owner: object, position: object) -> None:
        events.append(("display", position))
        owner._stage_axis_display_values["X"] = 10.0

    def render_selected(_owner: object) -> None:
        events.append(("selected",))
        owner._stage_axis_display_values["X"] = 1.0

    monkeypatch.setattr(
        homing_ui.stage_position_panel,
        "update_stage_position_display",
        render_raw,
    )
    monkeypatch.setattr(
        homing_ui.stage_position_panel,
        "refresh_coordinate_frame_display",
        render_selected,
    )

    homing_ui.on_limit_axes_changed(owner, ["X"])

    assert events[-1] == ("selected",)
    assert owner._stage_axis_display_values["X"] == 1.0
