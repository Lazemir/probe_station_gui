from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import main as main_module
from main import Main
from probe_station_gui.settings.manager import Settings
from probe_station_gui.dialogs.optical_calibration_wizard import (
    OpticalCalibrationMode,
)
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)


class _Signal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args: object) -> None:
        for callback in tuple(self.callbacks):
            callback(*args)


class _RecordingSignal:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        self.calls.append(tuple(args))


class _FakeWizard:
    def __init__(self, _parent=None) -> None:
        self.start_flat_field_requested = _Signal()
        self.start_lens_distortion_requested = _Signal()
        self.cancel_requested = _Signal()
        self.prepared = []
        self.objectives = []
        self.results = []
        self.progress = []
        self.shown = 0
        self.raised = 0
        self.activated = 0
        self.run_id = 17
        self.selected_mode = OpticalCalibrationMode.FULL

    def prepare(self, mode=None) -> bool:
        self.prepared.append(mode)
        return True

    def set_objective(self, name, **kwargs) -> None:
        self.objectives.append((name, kwargs))

    def active_run_id(self):
        return self.run_id

    def objective_text(self) -> str:
        return "X20"

    def mode(self) -> OpticalCalibrationMode:
        return self.selected_mode

    def set_flat_field_result(self, success, message, *, run_id=None) -> None:
        self.results.append(("flat", success, message, run_id))

    def set_lens_distortion_result(
        self,
        success,
        message,
        *,
        run_id=None,
        **kwargs,
    ) -> None:
        self.results.append(("lens", success, message, run_id))

    def set_progress(self, message, *, run_id=None) -> bool:
        self.progress.append((message, run_id))
        return True

    def show(self) -> None:
        self.shown += 1

    def raise_(self) -> None:
        self.raised += 1

    def activateWindow(self) -> None:
        self.activated += 1


class _FakeLensDialog:
    def __init__(self, _parent=None) -> None:
        self.calibrate_requested = _Signal()
        self.reset_requested = _Signal()
        self.shown = 0

    def show(self) -> None:
        self.shown += 1

    def raise_(self) -> None:
        pass

    def activateWindow(self) -> None:
        pass


class _LiveThread:
    def is_alive(self) -> bool:
        return True


class _StartFailThread:
    def __init__(self, **_kwargs) -> None:
        pass

    def is_alive(self) -> bool:
        return False

    def start(self) -> None:
        raise RuntimeError("thread start failed")


class _SessionLease:
    def __init__(self, token: str, operation: str, events: list[tuple[object, ...]]) -> None:
        self.token = token
        self.operation = operation
        self.events = events

    def snapshot(self) -> dict[str, object]:
        return {"operation": self.operation, "fixed_exposure_us": 2600.0}

    def close(self) -> dict[str, object]:
        self.events.append(("close", self.operation))
        return {"accepted": True}


class _SessionManager:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self._counter = 0

    def open(self, operation: str, parent_token=None) -> _SessionLease:
        self._counter += 1
        token = f"session-{self._counter}"
        self.events.append(("open", operation, parent_token, token))
        return _SessionLease(token, operation, self.events)


class _ObjectiveSettingsManager:
    def __init__(self) -> None:
        self.settings = Settings()

    def replace(self, settings: Settings) -> None:
        self.settings = settings

    def save(self) -> None:
        raise AssertionError("Objective change must not be persisted while calibration runs.")


def _objective_settings() -> ObjectivesSettings:
    return ObjectivesSettings(
        active_name="X20",
        objectives={
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                magnification=20.0,
                pixels_to_mm=[[-0.001, 0.0], [0.0, -0.001]],
                xy_calibration_configured=True,
                distortion_correction_configured=False,
            )
        },
    )


def test_show_optical_calibration_wizard_reports_active_objective(
    monkeypatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "current.json"
    manifest.write_text("{}", encoding="utf-8")
    window = Main.__new__(Main)
    window._optical_calibration_wizard = None
    window.settings_manager = SimpleNamespace(
        objectives_configuration=_objective_settings,
    )
    window._flat_field_calibration_store = SimpleNamespace(
        current_manifest_path=lambda _objective: manifest,
    )
    monkeypatch.setattr(main_module, "OpticalCalibrationWizard", _FakeWizard)

    Main._show_optical_calibration_wizard(window)

    wizard = window._optical_calibration_wizard
    assert wizard.prepared == [None]
    assert wizard.objectives == [
        (
            "X20",
            {"flat_field_configured": True, "lens_configured": False},
        )
    ]
    assert wizard.shown == wizard.raised == wizard.activated == 1


def test_show_optical_calibration_wizard_does_not_reprepare_retained_full_run(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "current.json"
    manifest.write_text("{}", encoding="utf-8")
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    statuses: list[tuple[str, int]] = []
    window._optical_calibration_wizard = wizard
    window._optical_calibration_outer_lease = object()
    window._optical_calibration_outer_close_in_progress = False
    window.settings_manager = SimpleNamespace(
        objectives_configuration=_objective_settings,
    )
    window._flat_field_calibration_store = SimpleNamespace(
        current_manifest_path=lambda _objective: manifest,
    )
    window._show_status = lambda message, timeout=0: statuses.append(
        (str(message), int(timeout))
    )

    Main._show_optical_calibration_wizard(window)

    assert wizard.run_id == 17
    assert wizard.prepared == []
    assert wizard.objectives == []
    assert wizard.shown == wizard.raised == wizard.activated == 1
    assert statuses == [("Optical calibration is still active.", 5000)]


def test_wizard_routes_run_identity_through_progress_and_completion() -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    context = main_module._OpticalCalibrationRunContext(
        operation_id="flat-17",
        wizard_run_id=17,
        objective_name="X20",
    )
    window._optical_calibration_wizard = wizard
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = context
    window._show_status = lambda *_args: None

    Main._on_flat_field_calibration_progress(
        window,
        context,
        "Flat field: capture 3/9.",
    )
    Main._on_flat_field_calibration_finished(window, context, True, "saved", {})

    assert wizard.progress == [("Flat field: capture 3/9.", 17)]
    assert wizard.results == [("flat", True, "saved", 17)]
    assert window._flat_field_calibration_context is None


def test_flat_completion_schedules_cleanup_when_cancel_arrived_while_queued() -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    context = main_module._OpticalCalibrationRunContext(
        operation_id="flat-cancel-race",
        wizard_run_id=17,
        objective_name="X20",
        parent_session_token="outer-token",
        full_wizard=True,
    )
    scheduled: list[str] = []
    window._optical_calibration_wizard = wizard
    window._flat_field_calibration_thread = _LiveThread()
    window._flat_field_calibration_context = context
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._show_status = lambda *_args: None
    window._schedule_optical_calibration_outer_close = lambda: scheduled.append(
        "close"
    )

    Main._cancel_optical_calibration_wizard(window, 17)
    assert scheduled == []

    window._flat_field_calibration_thread = None
    Main._on_flat_field_calibration_finished(
        window,
        context,
        False,
        "Flat-field calibration stopped by user.",
        None,
    )

    assert window._flat_field_calibration_context is None
    assert scheduled == ["close"]


def test_failed_lens_wizard_stage_returns_matching_result() -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    wizard.run_id = 23
    context = main_module._OpticalCalibrationRunContext(
        operation_id="lens-23",
        wizard_run_id=23,
        objective_name="X20",
    )
    window._optical_calibration_wizard = wizard
    window._lens_distortion_thread = None
    window._lens_distortion_context = context
    window._lens_distortion_dialog = None
    window._show_status = lambda *_args: None

    Main._on_lens_distortion_calibration_finished(
        window,
        context,
        False,
        "fit failed",
        None,
    )

    assert wizard.results == [("lens", False, "fit failed", 23)]
    assert window._lens_distortion_context is None


def test_wizard_rejects_objective_change_before_start() -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    starts = []
    window._optical_calibration_wizard = wizard
    window._active_objective_metadata = lambda: ("X5", 5.0)
    window._start_flat_field_calibration = lambda: starts.append(True) or True

    Main._start_flat_field_calibration_from_wizard(window)

    assert starts == []
    assert wizard.results == [
        (
            "flat",
            False,
            "Active objective changed. Reopen optical calibration.",
            17,
        )
    ]


def test_objective_change_is_blocked_while_calibration_context_is_active() -> None:
    window = Main.__new__(Main)
    manager = _ObjectiveSettingsManager()
    context = main_module._OpticalCalibrationRunContext(
        operation_id="lens-pre-stage",
        wizard_run_id=None,
        objective_name="X20",
    )
    statuses: list[str] = []
    restored: list[str] = []
    manager.settings.objectives.active_name = "X20"
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(is_busy=lambda: False)
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = context
    window._sync_objective_combo = lambda name: restored.append(name)
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    window._refresh_objective_calibration_ui = lambda: None

    Main._set_active_objective(window, "X5", apply_motion=True)

    assert manager.settings.objectives.active_name == "X20"
    assert restored == ["X20"]
    assert statuses == ["Stage is busy; objective not changed."]


def test_lens_start_captures_context_before_worker_thread_starts(monkeypatch) -> None:
    window = Main.__new__(Main)
    started_contexts = []

    class _Thread:
        def __init__(self, *, target, args, daemon) -> None:
            self.target = target
            self.args = args
            self.daemon = daemon

        def is_alive(self) -> bool:
            return False

        def start(self) -> None:
            context = self.args[-1]
            assert window._lens_distortion_context is context
            started_contexts.append(context)

    window.stage_controller = SimpleNamespace(
        is_busy=lambda: False,
        current_stage_position=lambda: (10.0, 20.0),
    )
    window._stage_serial_ready = lambda: True
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._coordinate_feedrate_for_axes = lambda _axes: 120.0
    window._current_needle_feedrate = lambda: 70.0
    window._lens_distortion_dialog = None
    window._show_status = lambda *_args: None
    monkeypatch.setattr(main_module.threading, "Thread", _Thread)

    assert Main._start_lens_distortion_calibration(window, wizard_run_id=41) is True

    assert len(started_contexts) == 1
    assert started_contexts[0].wizard_run_id == 41
    assert started_contexts[0].objective_name == "X20"


def test_flat_worker_start_failure_clears_only_unstarted_worker_state(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
    statuses: list[str] = []
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: False,
        current_stage_position=lambda: (10.0, 20.0),
    )
    window._stage_serial_ready = lambda: True
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._coordinate_feedrate_for_axes = lambda _axes: 120.0
    window._current_needle_feedrate = lambda: 70.0
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    monkeypatch.setattr(
        Main._start_flat_field_calibration.__globals__["threading"],
        "Thread",
        _StartFailThread,
    )

    started = Main._start_flat_field_calibration(window, wizard_run_id=41)

    assert started is False
    assert window._flat_field_calibration_thread is None
    assert window._flat_field_calibration_context is None
    assert statuses[-1] == "Flat-field calibration could not start: thread start failed"


def test_lens_worker_start_failure_preserves_existing_outer_lease(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
    statuses: list[str] = []
    outer_lease = object()
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: False,
        current_stage_position=lambda: (10.0, 20.0),
    )
    window._stage_serial_ready = lambda: True
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._optical_calibration_outer_lease = outer_lease
    window._optical_calibration_outer_token = "outer-token"
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._coordinate_feedrate_for_axes = lambda _axes: 120.0
    window._current_needle_feedrate = lambda: 70.0
    window._lens_distortion_dialog = None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    monkeypatch.setattr(
        Main._start_lens_distortion_calibration.__globals__["threading"],
        "Thread",
        _StartFailThread,
    )

    started = Main._start_lens_distortion_calibration(
        window,
        wizard_run_id=42,
        parent_session_token="outer-token",
        full_wizard=True,
    )

    assert started is False
    assert window._lens_distortion_thread is None
    assert window._lens_distortion_context is None
    assert window._optical_calibration_outer_lease is outer_lease
    assert window._optical_calibration_outer_token == "outer-token"
    assert statuses[-1] == "Lens distortion calibration could not start: thread start failed"


def test_full_wizard_uses_one_outer_session_and_explicit_nested_stage_tokens() -> None:
    window = Main.__new__(Main)
    manager = _SessionManager()
    window._optical_session_manager = manager
    flat_context = main_module._OpticalCalibrationRunContext(
        operation_id="flat-full",
        wizard_run_id=41,
        objective_name="X20",
        full_wizard=True,
    )

    flat_lease, flat_context = Main._open_optical_calibration_stage_session(
        window,
        "flat-field calibration",
        flat_context,
    )
    flat_lease.close()
    lens_context = main_module._OpticalCalibrationRunContext(
        operation_id="lens-full",
        wizard_run_id=42,
        objective_name="X20",
        parent_session_token=flat_context.parent_session_token,
        full_wizard=True,
    )
    lens_lease, lens_context = Main._open_optical_calibration_stage_session(
        window,
        "lens distortion calibration",
        lens_context,
    )
    lens_lease.close()
    close_error = Main._close_optical_calibration_outer_session(window)

    outer_token = flat_context.parent_session_token
    assert outer_token is not None
    assert lens_context.parent_session_token == outer_token
    assert manager.events == [
        ("open", "optical calibration", None, outer_token),
        ("open", "flat-field calibration", outer_token, "session-2"),
        ("close", "flat-field calibration"),
        ("open", "lens distortion calibration", outer_token, "session-3"),
        ("close", "lens distortion calibration"),
        ("close", "optical calibration"),
    ]
    assert close_error == ""


def test_outer_session_close_waits_through_exposure_policy_contention(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
    attempts = 0
    busy_error = Main._close_optical_calibration_outer_session.__globals__[
        "ExposurePolicyBusyError"
    ]

    class _ContendedLease:
        def close(self) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts <= 25:
                raise busy_error("Camera exposure policy is busy.")
            return {"accepted": True}

    window._optical_calibration_outer_lease = _ContendedLease()
    window._optical_calibration_outer_token = "outer-token"
    monkeypatch.setitem(
        Main._close_optical_calibration_outer_session.__globals__,
        "time",
        SimpleNamespace(sleep=lambda _seconds: None),
    )

    close_error = Main._close_optical_calibration_outer_session(window)

    assert attempts == 26
    assert close_error == ""
    assert window._optical_calibration_outer_lease is None


def test_outer_session_close_retains_owner_while_nested_child_is_active() -> None:
    window = Main.__new__(Main)
    child_active = True

    class _OuterLease:
        token = "outer-token"

        def __init__(self) -> None:
            self.active = True

        def close(self) -> dict[str, object]:
            if child_active:
                raise RuntimeError("Close nested optical sessions before their parent.")
            self.active = False
            return {"accepted": True}

        def is_active(self) -> bool:
            return self.active

    lease = _OuterLease()
    window._optical_calibration_outer_lease = lease
    window._optical_calibration_outer_token = lease.token
    window._optical_calibration_outer_close_requested = True

    first_error = Main._close_optical_calibration_outer_session(window)

    assert "nested optical sessions" in first_error
    assert window._optical_calibration_outer_lease is lease
    assert window._optical_calibration_outer_token == lease.token
    assert window._optical_calibration_outer_close_requested is True

    child_active = False
    second_error = Main._close_optical_calibration_outer_session(window)

    assert second_error == ""
    assert window._optical_calibration_outer_lease is None
    assert window._optical_calibration_outer_token is None
    assert window._optical_calibration_outer_close_requested is False


def test_full_wizard_objective_mismatch_requests_outer_session_close() -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    closes = []
    window._optical_calibration_wizard = wizard
    window._active_objective_metadata = lambda: ("X5", 5.0)
    window._start_lens_distortion_calibration = lambda **_kwargs: pytest.fail(
        "mismatched objective must not launch lens calibration"
    )
    window._cancel_optical_calibration_wizard = lambda run_id=None: closes.append(run_id)

    Main._start_lens_distortion_calibration_from_wizard(window)

    assert closes == [17]
    assert wizard.results == [
        (
            "lens",
            False,
            "Active objective changed. Reopen optical calibration.",
            17,
        )
    ]


def test_full_wizard_lens_launch_failure_requests_outer_session_close() -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    closes = []
    window._optical_calibration_wizard = wizard
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._optical_calibration_outer_parent_token = lambda: "outer-token"
    window._start_lens_distortion_calibration = lambda **_kwargs: False
    window._cancel_optical_calibration_wizard = lambda run_id=None: closes.append(run_id)

    Main._start_lens_distortion_calibration_from_wizard(window)

    assert closes == [17]
    assert wizard.results == [
        (
            "lens",
            False,
            "Lens distortion calibration did not start.",
            17,
        )
    ]


def test_wizard_cancel_closes_retained_outer_session_off_gui_thread(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
    manager = _SessionManager()
    window._optical_session_manager = manager
    context = main_module._OpticalCalibrationRunContext(
        operation_id="flat-full",
        wizard_run_id=51,
        objective_name="X20",
        full_wizard=True,
    )
    nested, _context = Main._open_optical_calibration_stage_session(
        window,
        "flat-field calibration",
        context,
    )
    nested.close()
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window.status_message_requested = _RecordingSignal()
    created_threads = []

    class _DeferredThread:
        def __init__(self, *, target, name, daemon) -> None:
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False
            created_threads.append(self)

        def is_alive(self) -> bool:
            return self.started

        def start(self) -> None:
            self.started = True

    monkeypatch.setattr(main_module.threading, "Thread", _DeferredThread)

    Main._cancel_optical_calibration_wizard(window, 51)

    assert Main._optical_calibration_cancel_event(window).is_set()
    assert len(created_threads) == 1
    assert created_threads[0].started is True
    assert ("close", "optical calibration") not in manager.events

    created_threads[0].target()

    assert manager.events[-1] == ("close", "optical calibration")


def test_outer_close_worker_start_failure_retains_owner_and_reports_failure(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
    outer_lease = object()
    window._optical_calibration_outer_lease = outer_lease
    window._optical_calibration_outer_token = "outer-token"
    window._optical_calibration_outer_close_requested = True
    window._optical_calibration_outer_close_thread = None
    window.status_message_requested = _RecordingSignal()
    monkeypatch.setattr(
        Main._schedule_optical_calibration_outer_close.__globals__["threading"],
        "Thread",
        _StartFailThread,
    )

    scheduled = Main._schedule_optical_calibration_outer_close(window)

    assert scheduled is False
    assert window._optical_calibration_outer_close_thread is None
    assert window._optical_calibration_outer_lease is outer_lease
    assert window._optical_calibration_outer_token == "outer-token"
    assert window._optical_calibration_outer_close_requested is True
    assert window.status_message_requested.calls == [
        (
            "Exposure policy restore could not start: thread start failed",
            10000,
        )
    ]


@pytest.mark.parametrize(
    (
        "operation",
        "progress_handler",
        "completion_handler",
        "context_attribute",
        "thread_attribute",
    ),
    [
        pytest.param(
            "flat",
            Main._on_flat_field_calibration_progress,
            Main._on_flat_field_calibration_finished,
            "_flat_field_calibration_context",
            "_flat_field_calibration_thread",
            id="flat-field",
        ),
        pytest.param(
            "lens",
            Main._on_lens_distortion_calibration_progress,
            Main._on_lens_distortion_calibration_finished,
            "_lens_distortion_context",
            "_lens_distortion_thread",
            id="lens-distortion",
        ),
    ],
)
def test_stale_calibration_events_do_not_affect_newer_active_context(
    operation,
    progress_handler,
    completion_handler,
    context_attribute,
    thread_attribute,
) -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    stale = main_module._OpticalCalibrationRunContext(
        operation_id=f"{operation}-a",
        wizard_run_id=31,
        objective_name="X20",
    )
    active = main_module._OpticalCalibrationRunContext(
        operation_id=f"{operation}-b",
        wizard_run_id=32,
        objective_name="X5",
    )
    thread_b = _LiveThread()
    window._optical_calibration_wizard = wizard
    setattr(window, context_attribute, active)
    setattr(window, thread_attribute, thread_b)
    window._lens_distortion_dialog = None
    window._show_status = lambda *_args: None

    progress_handler(window, stale, "A progress")
    completion_handler(
        window,
        stale,
        False,
        "A failed",
        None,
    )

    assert wizard.progress == []
    assert wizard.results == []
    assert getattr(window, context_attribute) is active
    assert getattr(window, thread_attribute) is thread_b


@pytest.mark.parametrize(
    ("reporter", "progress_signal_name"),
    (
        (
            Main._report_flat_field_calibration_progress,
            "flat_field_calibration_progress",
        ),
        (
            Main._report_lens_distortion_calibration_progress,
            "lens_distortion_calibration_progress",
        ),
    ),
)
def test_calibration_worker_progress_uses_queued_status_signal(
    reporter,
    progress_signal_name,
) -> None:
    context = main_module._OpticalCalibrationRunContext(
        operation_id="worker-progress",
        wizard_run_id=8,
        objective_name="X20",
    )
    status_signal = _RecordingSignal()
    progress_signal = _RecordingSignal()
    owner = SimpleNamespace(
        status_message_requested=status_signal,
        **{progress_signal_name: progress_signal},
    )

    reporter(owner, "capture 2/9", context)

    assert status_signal.calls == [("capture 2/9", 0)]
    assert progress_signal.calls == [(context, "capture 2/9")]


def test_lens_dialog_calibrate_opens_lens_only_wizard(monkeypatch) -> None:
    window = Main.__new__(Main)
    window._lens_distortion_dialog = None
    modes = []
    window._refresh_lens_distortion_ui = lambda: None
    window._reset_lens_distortion_calibration = lambda: None
    window._show_optical_calibration_wizard = lambda mode=None: modes.append(mode)
    monkeypatch.setattr(main_module, "LensDistortionDialog", _FakeLensDialog)

    Main._show_lens_distortion_dialog(window)
    window._lens_distortion_dialog.calibrate_requested.emit()

    assert modes == [OpticalCalibrationMode.LENS_DISTORTION]
