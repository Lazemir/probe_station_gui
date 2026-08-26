from __future__ import annotations

import gc
import weakref

from PySide6.QtWidgets import QApplication

from probe_station_gui.application import stage_motion_settle_polls as settle_module
from probe_station_gui.application.stage_motion_session import _StageMotionSession
from probe_station_gui.application.stage_motion_types import StageMotionConfig
from probe_station_gui.stage import types as stage_types
from probe_station_gui.stage.coordinate_targets import CoordinateTargetConfig
from probe_station_gui.stage.manual_jog_prediction import ManualJogPredictionConfig


class _Controller:
    def __init__(self) -> None:
        self.status_refreshes = 0
        self.polls = None
        self.pending_counts: list[int] = []

    def request_status_refresh(self) -> None:
        self.status_refreshes += 1
        if self.polls is not None:
            self.pending_counts.append(len(self.polls._timers))

    def latest_stage_state(self) -> str:
        return "Idle"

    def last_status_timestamp(self) -> float | None:
        return None

    def is_busy(self) -> bool:
        return False


class _TimerSignal:
    def __init__(self, timer) -> None:
        self.timer = timer
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self) -> None:
        _OwnedSingleShotTimer.current_sender = self.timer
        try:
            for callback in tuple(self.callbacks):
                callback()
        finally:
            _OwnedSingleShotTimer.current_sender = None


class _OwnedSingleShotTimer:
    instances = []
    fired_intervals: list[int] = []
    current_sender = None

    def __init__(self, parent) -> None:
        self.parent = parent
        self.timeout = _TimerSignal(self)
        self.interval_ms = 0
        self.single_shot = False
        self.started = False
        self.stopped = False
        self.deleted = False
        self.__class__.instances.append(self)

    def setSingleShot(self, enabled: bool) -> None:  # noqa: N802 - Qt API
        self.single_shot = bool(enabled)

    def setInterval(self, interval_ms: int) -> None:  # noqa: N802 - Qt API
        self.interval_ms = int(interval_ms)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def deleteLater(self) -> None:  # noqa: N802 - Qt API
        self.deleted = True

    def fire(self) -> None:
        self.__class__.fired_intervals.append(self.interval_ms)
        self.timeout.emit()


def _config() -> StageMotionConfig:
    return StageMotionConfig(
        axis_names=(),
        prediction_interval_ms=50,
        planned_move_duration_padding_s=0.12,
        planned_start_tolerance_mm=1e-4,
        min_feedrate_mm_min=1.0,
        b_position_change_tolerance_deg=1e-3,
        manual_jog=ManualJogPredictionConfig(
            axis_names=(),
            ignore_idle_after_command_s=0.25,
            reconcile_smooth_threshold_mm=0.35,
            reconcile_smooth_alpha=0.35,
            status_settle_hold_s=0.8,
            default_stop_tail_s=0.11,
            stop_tail_min_s=0.02,
            stop_tail_max_s=0.25,
            stop_tail_learn_alpha=0.25,
        ),
        coordinate_target=CoordinateTargetConfig(
            axis_names=(),
            min_feedrate_mm_min=1.0,
            duration_padding_s=0.0,
            min_idle_accept_s=0.0,
            target_tolerance_mm=0.01,
        ),
        settle_status_poll_delays_ms=(40, 120),
    )


def _session() -> tuple[QApplication, _StageMotionSession, _Controller]:
    application = QApplication.instance() or QApplication([])
    controller = _Controller()
    session = _StageMotionSession(controller, _config())
    return application, session, controller


def _use_owned_timer_fake(monkeypatch, polls) -> None:
    _OwnedSingleShotTimer.instances.clear()
    _OwnedSingleShotTimer.fired_intervals.clear()
    _OwnedSingleShotTimer.current_sender = None
    monkeypatch.setattr(settle_module, "QTimer", _OwnedSingleShotTimer)
    monkeypatch.setattr(
        polls,
        "sender",
        lambda: _OwnedSingleShotTimer.current_sender,
    )


def test_settle_poll_timers_have_exact_session_parent_and_reset_cleanup(
    monkeypatch,
) -> None:
    _application, session, _controller = _session()
    polls = session._settle_status_polls
    _use_owned_timer_fake(monkeypatch, polls)

    polls.schedule()

    timers = list(_OwnedSingleShotTimer.instances)
    assert [timer.interval_ms for timer in timers] == [40, 120]
    assert all(timer.parent is session for timer in timers)
    assert all(timer.single_shot and timer.started for timer in timers)
    assert polls._timers == set(timers)

    session.reset(stage_types.StageMotionResetReason.CONNECTION_CHANGED)

    assert polls._timers == set()
    assert all(timer.stopped and timer.deleted for timer in timers)


def test_settle_poll_series_overlap_without_coalescing(monkeypatch) -> None:
    _application, session, controller = _session()
    polls = session._settle_status_polls
    controller.polls = polls
    _use_owned_timer_fake(monkeypatch, polls)

    polls.schedule()
    polls.schedule()

    timers = list(_OwnedSingleShotTimer.instances)
    assert [timer.interval_ms for timer in timers] == [40, 120, 40, 120]
    assert all(timer.parent is session for timer in timers)
    assert len(polls._timers) == 4

    for _index, timer in sorted(
        enumerate(timers),
        key=lambda item: (item[1].interval_ms, item[0]),
    ):
        timer.fire()

    assert _OwnedSingleShotTimer.fired_intervals == [40, 40, 120, 120]
    assert controller.status_refreshes == 4
    assert controller.pending_counts == [3, 2, 1, 0]
    assert polls._timers == set()
    assert all(timer.deleted for timer in timers)


def test_unowned_timeout_sender_is_ignored(monkeypatch) -> None:
    _application, session, controller = _session()
    polls = session._settle_status_polls
    _use_owned_timer_fake(monkeypatch, polls)
    foreign_timer = _OwnedSingleShotTimer(session)
    foreign_timer.timeout.connect(polls._on_timeout)

    foreign_timer.fire()

    assert controller.status_refreshes == 0
    assert foreign_timer.deleted is False


def test_pending_settle_poll_timers_do_not_retain_destroyed_session() -> None:
    _application, session, _controller = _session()
    session._settle_status_polls.schedule()
    assert len(session._settle_status_polls._timers) == 2
    session_ref = weakref.ref(session)
    polls_ref = weakref.ref(session._settle_status_polls)
    timer_refs = tuple(
        weakref.ref(timer) for timer in session._settle_status_polls._timers
    )

    del session
    gc.collect()

    assert session_ref() is None
    assert polls_ref() is None
    assert all(timer_ref() is None for timer_ref in timer_refs)
