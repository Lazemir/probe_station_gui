from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.lens_distortion_dialog import LensDistortionDialog
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_lens_distortion_dialog_reports_active_objective_status() -> None:
    _qt_app()
    dialog = LensDistortionDialog()
    objectives = ObjectivesSettings(
        active_name="X50",
        objectives={
            "X5": ObjectiveCalibrationSettings(name="X5", magnification=5.0),
            "X50": ObjectiveCalibrationSettings(
                name="X50",
                magnification=50.0,
                distortion_correction={
                    "model_version": 1,
                    "frame_size": [640, 480],
                    "residual_mean_px": 0.23,
                    "residual_max_px": 0.91,
                },
                distortion_correction_configured=True,
            ),
        },
    )

    dialog.set_objectives(objectives)

    assert dialog._objective_label.text() == "X50"
    assert dialog._status_label.text() == "Configured"
    assert dialog._mean_error_label.text() == "0.23 px"
    assert dialog._max_error_label.text() == "0.91 px"

    dialog.deleteLater()
