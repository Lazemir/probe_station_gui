import threading
import types

import pytest

from probe_station_gui.route.api_artifacts import (
    ApiRouteArtifactsStore,
    api_route_photo_artifact_metadata,
    api_route_photo_content_type,
    api_route_session_action_response,
    api_route_session_result_response,
    api_route_session_seek_response,
    api_route_session_status_response,
    final_api_route_session_status,
)


class _RunnerWithStatus:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = dict(payload)

    def status_payload(self) -> dict[str, object]:
        return dict(self.payload)


class _RunnerForResult:
    def __init__(self, accepted: bool) -> None:
        self.accepted = accepted
        self.submitted: list[dict[str, object]] = []

    def submit_external_result(self, payload: dict[str, object]) -> bool:
        self.submitted.append(dict(payload))
        return self.accepted

    def design_frame_payload(self) -> dict[str, object]:
        return {"frame_id": "design-a", "frame_version": 4}


class _RunnerForSeek:
    def __init__(self, accepted: bool) -> None:
        self.accepted = accepted
        self.calls = 0

    def request_contact_seek(self) -> bool:
        self.calls += 1
        return self.accepted


class _RunnerForAction:
    def __init__(self, confirmations: dict[str, bool] | None = None) -> None:
        self.confirmations = confirmations or {}
        self.pause_calls = 0
        self.stop_calls = 0
        self.confirmation_calls: list[str] = []

    def request_pause_after_current_point(self) -> None:
        self.pause_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def submit_confirmation(self, action: str) -> bool:
        self.confirmation_calls.append(action)
        return self.confirmations.get(action, False)


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def exception(self, message: str) -> None:
        self.messages.append(str(message))


def test_api_route_session_status_uses_active_runner_payload() -> None:
    response = api_route_session_status_response(
        _RunnerWithStatus({"accepted": True, "state": "running"}),
        {"accepted": True, "state": "completed"},
        [{"artifact_id": "a1"}],
    )

    assert response == {
        "accepted": True,
        "state": "running",
        "artifacts": [{"artifact_id": "a1"}],
    }


def test_api_route_session_status_uses_last_status_fallback() -> None:
    response = api_route_session_status_response(
        object(),
        {"accepted": True, "state": "completed"},
        [],
    )

    assert response == {
        "accepted": True,
        "state": "completed",
        "artifacts": [],
    }


def test_api_route_session_status_rejects_when_no_session_exists() -> None:
    response = api_route_session_status_response(None, None, [])

    assert response == {
        "accepted": False,
        "status_code": 404,
        "message": "No API route session is active.",
    }


def test_api_route_artifacts_store_adds_and_serves_public_and_private_payloads() -> None:
    store = ApiRouteArtifactsStore(lock=threading.Lock())

    artifact_id = store.add(
        data=b"\x01\x02",
        filename="photo.png",
        content_type="image/png",
        kind="route_photo",
        metadata={"position": 1, "focus": {"focus_best_z_mm": 1.25}},
        created_at_utc="2026-06-26T20:00:00Z",
    )

    public_payloads = store.public_payloads()
    assert public_payloads == [
        {
            "artifact_id": artifact_id,
            "filename": "photo.png",
            "content_type": "image/png",
            "kind": "route_photo",
            "metadata": {"position": 1, "focus": {"focus_best_z_mm": 1.25}},
            "created_at_utc": "2026-06-26T20:00:00Z",
            "size_bytes": 2,
        }
    ]
    assert "data" not in public_payloads[0]

    response = store.artifact_response({"artifact_id": f"  {artifact_id}  "})
    assert response == {
        "accepted": True,
        "artifact_id": artifact_id,
        "filename": "photo.png",
        "content_type": "image/png",
        "kind": "route_photo",
        "metadata": {"position": 1, "focus": {"focus_best_z_mm": 1.25}},
        "created_at_utc": "2026-06-26T20:00:00Z",
        "size_bytes": 2,
        "data": b"\x01\x02",
    }


def test_api_route_artifacts_store_rejects_missing_artifact() -> None:
    store = ApiRouteArtifactsStore(lock=threading.Lock())

    assert store.artifact_response({"artifact_id": "missing"}) == {
        "accepted": False,
        "status_code": 404,
        "message": "Route session artifact was not found.",
    }


def test_api_route_artifacts_store_clear_removes_artifacts() -> None:
    store = ApiRouteArtifactsStore(lock=threading.Lock())
    store.add(
        data=b"x",
        filename="x.png",
        content_type="image/png",
        kind="route_photo",
        metadata={},
        created_at_utc="2026-06-26T20:00:00Z",
    )

    store.clear()

    assert store.public_payloads() == []


def test_api_route_session_result_response_accepts_external_result() -> None:
    runner = _RunnerForResult(True)

    response = api_route_session_result_response(
        {
            "status": "fail",
            "summary": {"r": 1},
            "files": ["a.csv"],
            "message": "done",
            "request_id": "req-1",
        },
        runner=runner,
        timestamp_utc="2026-06-26T20:01:00Z",
    )

    assert response == {
        "accepted": True,
        "message": "External result submitted.",
        "result": {
            "status": "fail",
            "summary": {"r": 1},
            "files": ["a.csv"],
            "message": "done",
            "timestamp_utc": "2026-06-26T20:01:00Z",
            "external_measurement_request_id": "req-1",
            "design_frame": {"frame_id": "design-a", "frame_version": 4},
        },
    }
    assert runner.submitted == [response["result"]]


def test_api_route_session_result_response_rejects_missing_runner() -> None:
    response = api_route_session_result_response(
        {},
        runner=None,
        timestamp_utc="2026-06-26T20:01:00Z",
    )

    assert response == {
        "accepted": False,
        "status_code": 404,
        "message": "No external route session is waiting for a result.",
    }


def test_api_route_session_result_response_rejects_conflict() -> None:
    response = api_route_session_result_response(
        {"status": "ok"},
        runner=_RunnerForResult(False),
        timestamp_utc="2026-06-26T20:01:00Z",
    )

    assert response == {
        "accepted": False,
        "status_code": 409,
        "message": "Route session is not waiting for an external result.",
    }


def test_api_route_session_seek_response_accepts_request() -> None:
    runner = _RunnerForSeek(True)

    response = api_route_session_seek_response(runner)

    assert response == {
        "accepted": True,
        "message": "Contact seek requested for current route contact.",
    }
    assert runner.calls == 1


def test_api_route_session_seek_response_rejects_missing_runner() -> None:
    response = api_route_session_seek_response(None)

    assert response == {
        "accepted": False,
        "status_code": 404,
        "message": "No route session is active.",
    }


def test_api_route_session_seek_response_rejects_conflict() -> None:
    response = api_route_session_seek_response(_RunnerForSeek(False))

    assert response == {
        "accepted": False,
        "status_code": 409,
        "message": "Route session is not waiting for contact seek.",
    }


def test_api_route_session_action_response_handles_pause_interrupt_stop_and_confirmation() -> None:
    runner = _RunnerForAction(confirmations={"resume": True})
    interrupts: list[tuple[object, str]] = []

    pause = api_route_session_action_response(
        {"action": "pause"},
        runner=runner,
        interrupt_runner=lambda selected_runner, *, reason: interrupts.append(
            (selected_runner, reason)
        ),
    )
    interrupt = api_route_session_action_response(
        {"action": "interrupt"},
        runner=runner,
        interrupt_runner=lambda selected_runner, *, reason: interrupts.append(
            (selected_runner, reason)
        ),
    )
    stop = api_route_session_action_response(
        {"action": "stop"},
        runner=runner,
        interrupt_runner=lambda *_args, **_kwargs: None,
    )
    confirm = api_route_session_action_response(
        {"action": "resume"},
        runner=runner,
        interrupt_runner=lambda *_args, **_kwargs: None,
    )

    assert pause == {
        "accepted": True,
        "message": "Route session pause requested.",
        "action": "pause",
    }
    assert interrupt == {
        "accepted": True,
        "message": "Route session interrupt requested.",
        "action": "interrupt",
    }
    assert stop == {
        "accepted": True,
        "message": "Route session stop requested.",
        "action": "stop",
    }
    assert confirm == {
        "accepted": True,
        "message": "Route session action submitted: resume.",
        "action": "resume",
    }
    assert runner.pause_calls == 1
    assert runner.stop_calls == 1
    assert runner.confirmation_calls == ["resume"]
    assert interrupts == [
        (runner, "Route API session interrupt requested."),
    ]


def test_api_route_session_action_response_rejects_missing_runner() -> None:
    response = api_route_session_action_response(
        {"action": "pause"},
        runner=None,
        interrupt_runner=lambda *_args, **_kwargs: None,
    )

    assert response == {
        "accepted": False,
        "status_code": 404,
        "message": "No route session is active.",
    }


def test_api_route_session_action_response_rejects_unknown_confirmation() -> None:
    response = api_route_session_action_response(
        {"action": "unknown"},
        runner=_RunnerForAction(),
        interrupt_runner=lambda *_args, **_kwargs: None,
    )

    assert response == {
        "accepted": False,
        "status_code": 400,
        "message": "Unknown route session action.",
    }


def test_final_api_route_session_status_returns_payload_only_for_active_session() -> None:
    payload = final_api_route_session_status(
        "session-1",
        _RunnerWithStatus({"accepted": True, "state": "completed"}),
        logger=_Logger(),
    )

    assert payload == {"accepted": True, "state": "completed"}
    assert final_api_route_session_status(None, object(), logger=_Logger()) is None
    assert final_api_route_session_status("session-1", object(), logger=_Logger()) is None


def test_final_api_route_session_status_logs_and_returns_none_when_status_read_fails() -> None:
    class _BrokenRunner:
        def status_payload(self) -> dict[str, object]:
            raise RuntimeError("boom")

    logger = _Logger()

    payload = final_api_route_session_status("session-1", _BrokenRunner(), logger=logger)

    assert payload is None
    assert logger.messages == ["Failed to store final API route session status."]


def test_api_route_photo_artifact_metadata_and_content_type() -> None:
    point = types.SimpleNamespace(index=3, label="P003")

    metadata = api_route_photo_artifact_metadata(
        point,
        position=1,
        total=2,
        contact_number=17,
        focus_result={"focus_best_z_mm": 1.2},
    )

    assert metadata == {
        "position": 1,
        "total": 2,
        "point_index": 3,
        "contact_number": 17,
        "label": "P003",
        "focus": {"focus_best_z_mm": 1.2},
    }
    assert api_route_photo_content_type("photo.jpg") == "image/jpeg"
    assert api_route_photo_content_type("photo.JPG") == "image/jpeg"
    assert api_route_photo_content_type("photo.png") == "image/png"
