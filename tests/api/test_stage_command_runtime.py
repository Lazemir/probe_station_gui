from __future__ import annotations

import logging
import subprocess
import sys
import threading

import pytest

from probe_station_gui.api.request_bridge import DeferredApiResponse
from probe_station_gui.api.stage_command_runtime import ApiStageCommandRuntime


def test_submit_dispatches_a_copied_request_on_one_named_daemon_worker() -> None:
    caller_thread_id = threading.get_ident()
    dispatch_started = threading.Event()
    release_dispatch = threading.Event()
    dispatched: list[tuple[dict[str, object], int, str, bool]] = []

    def dispatch(request: dict[str, object]) -> dict[str, object]:
        worker = threading.current_thread()
        dispatch_started.set()
        assert release_dispatch.wait(timeout=1.0)
        dispatched.append(
            (dict(request), threading.get_ident(), worker.name, worker.daemon)
        )
        return {"accepted": True, "status_code": 200}

    runtime = ApiStageCommandRuntime(dispatch)
    command_request: dict[str, object] = {
        "action": "stage_local_focus",
        "payload": {"range_mm": 0.03},
    }

    response = runtime.submit(command_request, "stage_local_focus")

    assert dispatch_started.wait(timeout=1.0)
    command_request["action"] = "mutated_after_submit"
    release_dispatch.set()
    assert response.wait(timeout_s=1.0) == {
        "accepted": True,
        "status_code": 200,
    }
    assert dispatched == [
        (
            {
                "action": "stage_local_focus",
                "payload": {"range_mm": 0.03},
            },
            dispatched[0][1],
            "ApiStageCommand-stage_local_focus",
            True,
        )
    ]
    assert dispatched[0][1] != caller_thread_id
    assert runtime.wait_until_idle(timeout_s=1.0)


def test_submit_rejects_a_second_command_with_the_active_action() -> None:
    dispatch_started = threading.Event()
    release_dispatch = threading.Event()
    dispatched: list[str] = []

    def dispatch(request: dict[str, object]) -> dict[str, object]:
        dispatched.append(str(request["action"]))
        dispatch_started.set()
        assert release_dispatch.wait(timeout=1.0)
        return {"accepted": True, "status_code": 202}

    runtime = ApiStageCommandRuntime(dispatch)
    first = runtime.submit(
        {"action": "start_route_session", "payload": {"start": 1}},
        "start_route_session",
    )
    assert dispatch_started.wait(timeout=1.0)

    second = runtime.submit(
        {"action": "stage_local_focus", "payload": {}},
        "stage_local_focus",
    )

    assert second == {
        "accepted": False,
        "status_code": 409,
        "message": "API stage command is already running: start_route_session.",
    }
    assert dispatched == ["start_route_session"]
    release_dispatch.set()
    assert first.wait(timeout_s=1.0) == {"accepted": True, "status_code": 202}


def test_simultaneous_submits_reserve_exactly_one_command() -> None:
    submit_gate = threading.Barrier(3)
    dispatch_started = threading.Event()
    release_dispatch = threading.Event()
    results: list[tuple[str, dict[str, object] | DeferredApiResponse]] = []
    results_lock = threading.Lock()
    dispatched: list[str] = []

    def dispatch(request: dict[str, object]) -> dict[str, object]:
        dispatched.append(str(request["action"]))
        dispatch_started.set()
        assert release_dispatch.wait(timeout=1.0)
        return {"accepted": True, "status_code": 202}

    runtime = ApiStageCommandRuntime(dispatch)

    def submit(action: str) -> None:
        submit_gate.wait()
        response = runtime.submit({"action": action, "payload": {}}, action)
        with results_lock:
            results.append((action, response))

    submitters = [
        threading.Thread(target=submit, args=(action,))
        for action in ("stage_local_focus", "start_route_session")
    ]
    for submitter in submitters:
        submitter.start()
    submit_gate.wait()
    for submitter in submitters:
        submitter.join(timeout=1.0)

    assert dispatch_started.wait(timeout=1.0)
    assert all(not submitter.is_alive() for submitter in submitters)
    accepted = [
        (action, response)
        for action, response in results
        if isinstance(response, DeferredApiResponse)
    ]
    rejected = [
        (action, response) for action, response in results if isinstance(response, dict)
    ]
    assert len(accepted) == 1
    assert len(rejected) == 1
    accepted_action, completion = accepted[0]
    assert dispatched == [accepted_action]
    assert rejected[0][1] == {
        "accepted": False,
        "status_code": 409,
        "message": f"API stage command is already running: {accepted_action}.",
    }
    release_dispatch.set()
    assert completion.wait(timeout_s=1.0) == {
        "accepted": True,
        "status_code": 202,
    }
    assert runtime.wait_until_idle(timeout_s=1.0)


def test_submit_copies_top_level_request_before_held_worker_runs() -> None:
    held_threads: list[object] = []
    dispatched: list[dict[str, object]] = []

    class HeldThread:
        def __init__(self, *, target, **_kwargs: object) -> None:
            self._target = target
            held_threads.append(self)

        def start(self) -> None:
            pass

        def run(self) -> None:
            self._target()

    runtime = ApiStageCommandRuntime(
        lambda request: (
            dispatched.append(dict(request)) or {"accepted": True, "status_code": 200}
        ),
        thread_factory=HeldThread,
    )
    request: dict[str, object] = {
        "action": "stage_local_focus",
        "payload": {"range_mm": 0.03},
    }

    response = runtime.submit(request, "stage_local_focus")
    request["action"] = "mutated_after_submit"
    assert dispatched == []
    held_threads[0].run()

    assert response.wait(timeout_s=0.0) == {
        "accepted": True,
        "status_code": 200,
    }
    assert dispatched == [
        {
            "action": "stage_local_focus",
            "payload": {"range_mm": 0.03},
        }
    ]
    assert runtime.wait_until_idle(timeout_s=0.0)


def test_reservation_is_active_until_completion_publication_returns() -> None:
    release_dispatch = threading.Event()
    publication_started = threading.Event()
    release_publication = threading.Event()

    def dispatch(_request: dict[str, object]) -> dict[str, object]:
        assert release_dispatch.wait(timeout=1.0)
        return {"accepted": True, "status_code": 202}

    runtime = ApiStageCommandRuntime(dispatch)
    response = runtime.submit(
        {"action": "start_route_session", "payload": {}},
        "start_route_session",
    )

    def hold_publication(_result: dict[str, object]) -> None:
        publication_started.set()
        assert release_publication.wait(timeout=1.0)

    response.add_done_callback(hold_publication)
    release_dispatch.set()
    assert publication_started.wait(timeout=1.0)
    assert runtime.active()
    assert not runtime.wait_until_idle(timeout_s=0.0)
    assert runtime.submit(
        {"action": "stage_local_focus", "payload": {}},
        "stage_local_focus",
    ) == {
        "accepted": False,
        "status_code": 409,
        "message": "API stage command is already running: start_route_session.",
    }
    release_publication.set()
    assert runtime.wait_until_idle(timeout_s=1.0)


def test_thread_start_failure_returns_500_and_leaves_runtime_idle() -> None:
    dispatches: list[dict[str, object]] = []

    class FailingThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread unavailable")

    runtime = ApiStageCommandRuntime(
        lambda request: dispatches.append(dict(request)) or {},
        thread_factory=FailingThread,
    )

    response = runtime.submit(
        {"action": "stage_local_focus", "payload": {}},
        "stage_local_focus",
    )

    assert response == {
        "accepted": False,
        "status_code": 500,
        "message": "API stage command could not start: thread unavailable",
    }
    assert dispatches == []
    assert not runtime.active()
    assert runtime.wait_until_idle(timeout_s=0.0)


def test_thread_factory_construction_failure_returns_500_and_releases() -> None:
    def fail_thread_construction(**_kwargs: object):
        raise RuntimeError("thread construction unavailable")

    runtime = ApiStageCommandRuntime(
        lambda _request: {"accepted": True, "status_code": 200},
        thread_factory=fail_thread_construction,
    )

    response = runtime.submit(
        {"action": "stage_local_focus", "payload": {}},
        "stage_local_focus",
    )

    assert response == {
        "accepted": False,
        "status_code": 500,
        "message": (
            "API stage command could not start: thread construction unavailable"
        ),
    }
    assert not runtime.active()
    assert runtime.wait_until_idle(timeout_s=0.0)


def test_blocked_waiter_wakes_when_thread_start_failure_releases() -> None:
    start_entered = threading.Event()
    release_start = threading.Event()
    submit_result: list[dict[str, object] | DeferredApiResponse] = []
    waiter_started = threading.Event()
    waiter_result: list[bool] = []

    class BlockingFailingThread:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            start_entered.set()
            assert release_start.wait(timeout=1.0)
            raise RuntimeError("thread unavailable")

    runtime = ApiStageCommandRuntime(
        lambda _request: {"accepted": True, "status_code": 200},
        thread_factory=BlockingFailingThread,
    )
    submitter = threading.Thread(
        target=lambda: submit_result.append(
            runtime.submit(
                {"action": "stage_local_focus", "payload": {}},
                "stage_local_focus",
            )
        )
    )
    submitter.start()
    assert start_entered.wait(timeout=1.0)

    def wait_for_idle() -> None:
        waiter_started.set()
        waiter_result.append(runtime.wait_until_idle(timeout_s=1.0))

    waiter = threading.Thread(target=wait_for_idle)
    waiter.start()
    assert waiter_started.wait(timeout=1.0)
    assert waiter.is_alive()
    release_start.set()
    submitter.join(timeout=1.0)
    waiter.join(timeout=1.0)

    assert not submitter.is_alive()
    assert not waiter.is_alive()
    assert submit_result == [
        {
            "accepted": False,
            "status_code": 500,
            "message": "API stage command could not start: thread unavailable",
        }
    ]
    assert waiter_result == [True]
    assert not runtime.active()


def test_cancelled_dispatch_result_is_published_before_runtime_becomes_idle() -> None:
    cancelled = {
        "accepted": False,
        "status_code": 409,
        "message": "Operation cancelled.",
    }
    runtime = ApiStageCommandRuntime(lambda _request: cancelled)

    response = runtime.submit(
        {"action": "stage_local_focus", "payload": {}},
        "stage_local_focus",
    )

    assert response.wait(timeout_s=1.0) == cancelled
    assert runtime.wait_until_idle(timeout_s=1.0)


def test_dispatch_exception_logs_and_publishes_500_before_release(caplog) -> None:
    def dispatch(_request: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("dispatch failed")

    runtime = ApiStageCommandRuntime(dispatch)

    with caplog.at_level(
        logging.ERROR,
        logger="probe_station_gui.api.stage_command_runtime",
    ):
        response = runtime.submit(
            {"action": "stage_local_focus", "payload": {}},
            "stage_local_focus",
        )
        result = response.wait(timeout_s=1.0)

    assert result == {
        "accepted": False,
        "status_code": 500,
        "message": "dispatch failed",
    }
    assert "API stage command failed." in caplog.messages
    assert runtime.wait_until_idle(timeout_s=1.0)


def test_completion_callback_exception_still_releases_runtime() -> None:
    held_threads: list[object] = []

    class HeldThread:
        def __init__(self, *, target, **_kwargs: object) -> None:
            self._target = target
            held_threads.append(self)

        def start(self) -> None:
            pass

        def run(self) -> None:
            self._target()

    runtime = ApiStageCommandRuntime(
        lambda _request: {"accepted": True, "status_code": 200},
        thread_factory=HeldThread,
    )
    response = runtime.submit(
        {"action": "stage_local_focus", "payload": {}},
        "stage_local_focus",
    )

    def fail_callback(_result: dict[str, object]) -> None:
        raise RuntimeError("callback failed")

    response.add_done_callback(fail_callback)
    with pytest.raises(RuntimeError, match="callback failed"):
        held_threads[0].run()

    assert response.wait(timeout_s=0.0) == {
        "accepted": True,
        "status_code": 200,
    }
    assert not runtime.active()
    assert runtime.wait_until_idle(timeout_s=0.0)


def test_wait_timeout_is_clamped_and_observes_only_reservation_removal() -> None:
    release_dispatch = threading.Event()

    def dispatch(_request: dict[str, object]) -> dict[str, object]:
        assert release_dispatch.wait(timeout=1.0)
        return {"accepted": True, "status_code": 200}

    runtime = ApiStageCommandRuntime(dispatch)
    response = runtime.submit(
        {"action": "stage_local_focus", "payload": {}},
        "stage_local_focus",
    )

    assert not runtime.wait_until_idle(timeout_s=-1.0)
    assert not runtime.wait_until_idle(timeout_s=0.01)
    release_dispatch.set()
    assert response.wait(timeout_s=1.0) == {"accepted": True, "status_code": 200}
    assert runtime.wait_until_idle(timeout_s=1.0)


def test_import_and_construction_are_lazy_in_a_fresh_process() -> None:
    script = "\n".join(
        [
            "import sys, threading",
            "before = tuple(threading.enumerate())",
            (
                "from probe_station_gui.api.stage_command_runtime "
                "import ApiStageCommandRuntime"
            ),
            "runtime = ApiStageCommandRuntime(lambda request: request)",
            "assert not runtime.active()",
            "assert tuple(threading.enumerate()) == before",
            (
                "forbidden = [bytes(codes).decode() for codes in "
                "[[80,121,83,105,100,101,54],"
                "[114,111,116,112,121],"
                "[112,121,118,105,115,97],"
                "[113,99,111,100,101,115],"
                "[112,114,111,98,101,95,115,116,97,116,105,111,110,95,103,117,105,"
                "46,115,116,97,103,101,95,99,111,110,116,114,111,108,108,101,114],"
                "[112,114,111,98,101,95,115,116,97,116,105,111,110,95,103,117,105,"
                "46,99,97,109,101,114,97]]]"
            ),
            (
                "assert not any(any(name == prefix or "
                "name.startswith(prefix + chr(46)) for prefix in forbidden) "
                "for name in sys.modules)"
            ),
        ]
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
