from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import main as main_module
from main import Main
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
    window._optical_calibration_wizard = wizard
    window._flat_field_wizard_run_id = None
    window._flat_field_calibration_thread = None
    window._show_status = lambda *_args: None
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._start_flat_field_calibration = lambda: True

    Main._start_flat_field_calibration_from_wizard(window)
    assert window._flat_field_wizard_run_id == 17

    Main._on_flat_field_calibration_progress(window, "Flat field: capture 3/9.")
    Main._on_flat_field_calibration_finished(window, True, "saved", {})

    assert wizard.progress == [("Flat field: capture 3/9.", 17)]
    assert wizard.results == [("flat", True, "saved", 17)]
    assert window._flat_field_wizard_run_id is None


def test_failed_lens_wizard_stage_returns_matching_result() -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    wizard.run_id = 23
    window._optical_calibration_wizard = wizard
    window._lens_distortion_wizard_run_id = None
    window._lens_distortion_thread = None
    window._lens_distortion_dialog = None
    window._show_status = lambda *_args: None
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._start_lens_distortion_calibration = lambda: True

    Main._start_lens_distortion_calibration_from_wizard(window)
    Main._on_lens_distortion_calibration_finished(
        window,
        False,
        "fit failed",
        None,
    )

    assert wizard.results == [("lens", False, "fit failed", 23)]
    assert window._lens_distortion_wizard_run_id is None


def test_wizard_rejects_objective_change_before_start() -> None:
    window = Main.__new__(Main)
    wizard = _FakeWizard()
    starts = []
    window._optical_calibration_wizard = wizard
    window._flat_field_wizard_run_id = None
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
