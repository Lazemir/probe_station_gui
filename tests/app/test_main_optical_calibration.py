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

    def emit(self) -> None:
        for callback in tuple(self.callbacks):
            callback()


class _RecordingSignal:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        self.calls.append(tuple(args))


class _FakeWizard:
    def __init__(self, _parent=None) -> None:
        self.start_flat_field_requested = _Signal()
        self.start_lens_distortion_requested = _Signal()
        self.prepared = []
        self.objectives = []
        self.results = []
        self.progress = []
        self.shown = 0
        self.raised = 0
        self.activated = 0
        self.run_id = 17

    def prepare(self, mode=None) -> bool:
        self.prepared.append(mode)
        return True

    def set_objective(self, name, **kwargs) -> None:
        self.objectives.append((name, kwargs))

    def active_run_id(self):
        return self.run_id

    def objective_text(self) -> str:
        return "X20"

    def set_flat_field_result(self, success, message, *, run_id=None) -> None:
        self.results.append(("flat", success, message, run_id))

    def set_lens_distortion_result(self, success, message, *, run_id=None) -> None:
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
