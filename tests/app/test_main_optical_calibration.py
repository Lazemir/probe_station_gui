from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import main as main_module
from main import Main
from probe_station_gui.camera.optical_calibration_runtime import (
    OpticalCalibrationOutcome,
    OpticalCalibrationProgress,
)
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
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(parent_session_token=None)
    )
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
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(parent_session_token="outer-token")
    )
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
    context = OpticalCalibrationOutcome(
        "flat-17", 17, "flat", True, "saved", "X20", True,
        flat_payload={},
    )
    window._optical_calibration_wizard = wizard
    window._show_status = lambda *_args: None

    Main._on_flat_field_calibration_progress(
        window,
        OpticalCalibrationProgress("flat-17", 17, "flat", "Flat field: capture 3/9."),
        "Flat field: capture 3/9.",
    )
    Main._on_flat_field_calibration_finished(window, context, True, "saved", {})

    assert wizard.progress == [("Flat field: capture 3/9.", 17)]
    assert wizard.results == [("flat", True, "saved", 17)]

def test_failed_lens_wizard_stage_returns_matching_result() -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    wizard.run_id = 23
    context = OpticalCalibrationOutcome(
        "lens-23", 23, "lens", False, "fit failed", "X20", True,
    )
    window._optical_calibration_wizard = wizard
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
    statuses: list[str] = []
    restored: list[str] = []
    manager.settings.objectives.active_name = "X20"
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(is_busy=lambda: False)
    window._api_stage_command_worker_active = lambda: False
    window._microscope_scan_running = lambda: False
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id="lens-pre-stage", parent_session_token=None)
    )
    window._sync_objective_combo = lambda name: restored.append(name)
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    window._refresh_objective_calibration_ui = lambda: None

    Main._set_active_objective(window, "X5", apply_motion=True)

    assert manager.settings.objectives.active_name == "X20"
    assert restored == ["X20"]
    assert statuses == ["Stage is busy; objective not changed."]

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
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(parent_session_token="outer-token")
    )
    window._start_lens_distortion_calibration = lambda **_kwargs: {
        "accepted": False,
        "status_code": 409,
        "message": "Lens distortion calibration did not start.",
    }
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
