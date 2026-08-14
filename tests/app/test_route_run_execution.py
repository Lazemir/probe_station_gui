from __future__ import annotations

import importlib
from collections.abc import Callable

import pytest


def _owner_module():
    return importlib.import_module("probe_station_gui.application.route_run_execution")


def _owner_types():
    module = _owner_module()
    return (
        module._RouteRunExecutionSlot,
        module.RouteRunKind,
        module.RouteRunReleaseCause,
        module.RouteRunReleaseRequest,
    )


class _Runner:
    def __init__(
        self,
        *,
        status: dict[str, object] | None = None,
        callback: Callable[[], None] | None = None,
        calls: list[object] | None = None,
    ) -> None:
        self.status = status or {}
        self.callback = callback
        self.calls = calls if calls is not None else []

    def status_payload(self) -> dict[str, object]:
        if self.callback is not None:
            self.callback()
        self.calls.append("status")
        return dict(self.status)

    def request_current_point_correction(self) -> None:
        if self.callback is not None:
            self.callback()
        self.calls.append("interrupt")

    def stop(self) -> None:
        if self.callback is not None:
            self.callback()
        self.calls.append("stop")


class _Thread:
    def __init__(
        self,
        *,
        alive: bool,
        stops_after_join: bool = True,
        callback: Callable[[], None] | None = None,
        calls: list[object] | None = None,
    ) -> None:
        self.alive = alive
        self.stops_after_join = stops_after_join
        self.callback = callback
        self.calls = calls if calls is not None else []
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def is_alive(self) -> bool:
        if self.callback is not None:
            self.callback()
        self.calls.append("is_alive")
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        if self.callback is not None:
            self.callback()
        self.calls.append(("join", timeout))
        if self.stops_after_join:
            self.alive = False


def _activate(*, kind_name: str = "GUI", alive: bool = True):
    slot_type, kind_type, _, _ = _owner_types()
    slot = slot_type()
    runner = _Runner()
    thread = _Thread(alive=alive)
    slot.activate(runner, thread, kind=getattr(kind_type, kind_name))
    return slot, runner, thread


def _release_request(
    cause_name: str,
    runner: object,
    *,
    join_timeout_s: float,
):
    _, _, cause_type, request_type = _owner_types()
    return request_type(
        cause=getattr(cause_type, cause_name),
        expected_runner=runner,
        join_timeout_s=join_timeout_s,
    )


def _assert_slot_lock_available(slot: object) -> None:
    lock = slot._lock
    acquired = lock.acquire(blocking=False)
    assert acquired, "slot invoked a callback while holding its lock"
    lock.release()


def test_canonical_route_run_execution_owner_exists() -> None:
    module = _owner_module()

    assert module._RouteRunExecutionSlot.__module__ == module.__name__


def test_idle_snapshot() -> None:
    slot_type, _, _, _ = _owner_types()

    snapshot = slot_type().snapshot()

    assert snapshot.runner is None
    assert snapshot.thread is None
    assert snapshot.kind is None
    assert snapshot.waiting is False
    assert snapshot.waiting_reason == ""
    assert snapshot.active is False
    assert snapshot.thread_alive is False


@pytest.mark.parametrize("kind_name", ["GUI", "EXTERNAL_RESULT_SESSION"])
def test_activation_records_explicit_kind_without_starting_thread(
    kind_name: str,
) -> None:
    slot_type, kind_type, _, _ = _owner_types()
    slot = slot_type()
    runner = _Runner()
    thread = _Thread(alive=False)

    snapshot = slot.activate(
        runner,
        thread,
        kind=getattr(kind_type, kind_name),
    )

    assert snapshot.runner is runner
    assert snapshot.thread is thread
    assert snapshot.kind is getattr(kind_type, kind_name)
    assert snapshot.active is True
    assert snapshot.waiting is False
    assert thread.start_calls == 0


def test_activation_rejects_a_second_active_run() -> None:
    slot, runner, thread = _activate()
    prior = slot.snapshot()

    with pytest.raises(RuntimeError, match="already active"):
        slot.activate(_Runner(), _Thread(alive=True), kind=prior.kind)

    assert slot.snapshot() == prior
    assert slot.snapshot().runner is runner
    assert slot.snapshot().thread is thread


def test_waiting_publication_derives_and_clears_reason() -> None:
    slot_type, kind_type, _, _ = _owner_types()
    slot = slot_type()
    runner = _Runner(status={"waiting_reason": "  contact attention  "})
    slot.activate(runner, _Thread(alive=True), kind=kind_type.GUI)

    waiting = slot.publish_waiting(True)
    running = slot.publish_waiting(False)

    assert waiting.waiting is True
    assert waiting.waiting_reason == "contact attention"
    assert running.waiting is False
    assert running.waiting_reason == ""


@pytest.mark.parametrize(
    "status",
    [{}, {"waiting_reason": ""}, {"waiting_reason": None}],
)
def test_waiting_publication_uses_paused_fallback(
    status: dict[str, object],
) -> None:
    slot_type, kind_type, _, _ = _owner_types()
    slot = slot_type()
    slot.activate(_Runner(status=status), _Thread(alive=True), kind=kind_type.GUI)

    assert slot.publish_waiting(True).waiting_reason == "paused"


def test_stale_waiting_publication_after_release_cannot_reactivate_slot() -> None:
    slot, runner, _ = _activate(alive=False)
    slot.release(_release_request("FINISHED", runner, join_timeout_s=0.1))

    snapshot = slot.publish_waiting(True)

    assert snapshot.active is False
    assert snapshot.waiting is False
    assert snapshot.waiting_reason == ""


@pytest.mark.parametrize(
    ("waiting", "cancel_stage"),
    [(False, True), (True, False)],
)
def test_interrupt_requests_runner_correction_and_routes_stage_cancel(
    waiting: bool,
    cancel_stage: bool,
) -> None:
    slot, runner, _ = _activate()
    slot.publish_waiting(waiting)

    directive = slot.request_interrupt()

    assert directive.runner is runner
    assert directive.cancel_stage is cancel_stage
    assert runner.calls[-1] == "interrupt"
    assert slot.snapshot().waiting is waiting


def test_idle_interrupt_is_a_noop() -> None:
    slot_type, _, _, _ = _owner_types()

    directive = slot_type().request_interrupt()

    assert directive.runner is None
    assert directive.cancel_stage is False


def test_successful_gui_takeover_stops_then_joins_and_clears() -> None:
    slot_type, kind_type, _, _ = _owner_types()
    calls: list[object] = []
    slot = slot_type()
    runner = _Runner(calls=calls)
    thread = _Thread(alive=True, calls=calls)
    slot.activate(runner, thread, kind=kind_type.GUI)
    slot.publish_waiting(True)
    prior = slot.snapshot()

    outcome = slot.release(_release_request("TAKEOVER", runner, join_timeout_s=2.0))

    assert calls[-4:] == ["stop", "is_alive", ("join", 2.0), "is_alive"]
    assert outcome.released is True
    assert outcome.stale is False
    assert outcome.timed_out is False
    assert outcome.rejected is False
    assert outcome.prior == prior
    assert outcome.current.active is False
    assert slot.snapshot() == outcome.current


def test_timed_out_gui_takeover_retains_the_complete_snapshot() -> None:
    slot_type, kind_type, _, _ = _owner_types()
    slot = slot_type()
    runner = _Runner(status={"waiting_reason": "contact attention"})
    thread = _Thread(alive=True, stops_after_join=False)
    slot.activate(runner, thread, kind=kind_type.GUI)
    slot.publish_waiting(True)
    prior = slot.snapshot()

    outcome = slot.release(_release_request("TAKEOVER", runner, join_timeout_s=2.0))

    assert outcome.released is False
    assert outcome.timed_out is True
    assert outcome.rejected is False
    assert outcome.prior == prior
    assert outcome.current == prior
    assert slot.snapshot() == prior


@pytest.mark.parametrize(
    ("kind_name", "waiting"),
    [("GUI", False), ("EXTERNAL_RESULT_SESSION", True)],
)
def test_takeover_rejects_nonwaiting_or_non_gui_runs(
    kind_name: str,
    waiting: bool,
) -> None:
    slot, runner, thread = _activate(kind_name=kind_name)
    slot.publish_waiting(waiting)
    prior = slot.snapshot()

    outcome = slot.release(_release_request("TAKEOVER", runner, join_timeout_s=2.0))

    assert outcome.rejected is True
    assert outcome.released is False
    assert outcome.current == prior
    assert runner.calls == ([] if not waiting else ["status"])
    assert ("join", 2.0) not in thread.calls


def test_failed_start_cleanup_clears_even_when_stop_times_out() -> None:
    slot, runner, thread = _activate(kind_name="EXTERNAL_RESULT_SESSION")
    thread.stops_after_join = False
    prior = slot.snapshot()

    outcome = slot.release(_release_request("FAILED_START", runner, join_timeout_s=2.0))

    assert runner.calls == ["stop"]
    assert thread.calls == ["is_alive", ("join", 2.0), "is_alive"]
    assert outcome.released is True
    assert outcome.timed_out is True
    assert outcome.prior == prior
    assert outcome.current.active is False


def test_current_finish_returns_prior_snapshot_joins_dead_thread_and_clears() -> None:
    slot, runner, thread = _activate(alive=False)
    prior = slot.snapshot()

    outcome = slot.release(_release_request("FINISHED", runner, join_timeout_s=0.1))

    assert thread.calls == ["is_alive", ("join", 0.1)]
    assert outcome.released is True
    assert outcome.prior == prior
    assert outcome.current.active is False


def test_stale_finish_is_ignored_without_invoking_current_run_callbacks() -> None:
    slot, runner, thread = _activate(alive=False)
    prior = slot.snapshot()

    outcome = slot.release(_release_request("FINISHED", _Runner(), join_timeout_s=0.1))

    assert outcome.stale is True
    assert outcome.released is False
    assert outcome.current == prior
    assert runner.calls == []
    assert thread.calls == []


def test_structural_callbacks_are_invoked_without_holding_the_slot_lock() -> None:
    slot_type, kind_type, _, _ = _owner_types()
    slot = slot_type()

    def callback() -> None:
        _assert_slot_lock_available(slot)

    runner = _Runner(
        status={"waiting_reason": "paused"},
        callback=callback,
    )
    thread = _Thread(alive=True, callback=callback)
    slot.activate(runner, thread, kind=kind_type.GUI)

    assert slot.snapshot().thread_alive is True
    slot.publish_waiting(True)
    slot.request_interrupt()
    outcome = slot.release(_release_request("TAKEOVER", runner, join_timeout_s=2.0))

    assert outcome.released is True
