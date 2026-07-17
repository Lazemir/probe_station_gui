from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QWizard

from probe_station_gui.dialogs.optical_calibration_wizard import (
    OpticalCalibrationMode,
    OpticalCalibrationWizard,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _show(wizard: OpticalCalibrationWizard, qt_app: QApplication) -> None:
    wizard.show()
    qt_app.processEvents()


def test_full_mode_runs_flat_field_then_lens_distortion(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    wizard.set_mode(OpticalCalibrationMode.FULL)
    flat_spy = QSignalSpy(wizard.start_flat_field_requested)
    lens_spy = QSignalSpy(wizard.start_lens_distortion_requested)
    _show(wizard, qt_app)

    wizard.next()
    assert wizard.currentId() == wizard.FLAT_FIELD_PAGE_ID

    wizard.next()
    assert flat_spy.count() == 1
    assert wizard.currentId() == wizard.FLAT_FIELD_PAGE_ID
    assert wizard.is_running()

    wizard.set_flat_field_result(
        True,
        "Flat field saved.",
        run_id=wizard.active_run_id(),
    )
    qt_app.processEvents()
    assert wizard.currentId() == wizard.LENS_DISTORTION_PAGE_ID
    assert not wizard.is_running()

    wizard.next()
    assert lens_spy.count() == 1
    assert wizard.currentId() == wizard.LENS_DISTORTION_PAGE_ID

    wizard.set_lens_distortion_result(
        True,
        "Lens correction saved.",
        run_id=wizard.active_run_id(),
    )
    qt_app.processEvents()
    assert wizard.currentId() == wizard.RESULT_PAGE_ID
    assert wizard.button(QWizard.FinishButton).isEnabled()

    wizard.close()


@pytest.mark.parametrize(
    ("mode", "capture_page_id", "signal_name"),
    (
        (
            OpticalCalibrationMode.FLAT_FIELD,
            OpticalCalibrationWizard.FLAT_FIELD_PAGE_ID,
            "start_flat_field_requested",
        ),
        (
            OpticalCalibrationMode.LENS_DISTORTION,
            OpticalCalibrationWizard.LENS_DISTORTION_PAGE_ID,
            "start_lens_distortion_requested",
        ),
    ),
)
def test_individual_mode_skips_the_other_capture(
    qt_app: QApplication,
    mode: OpticalCalibrationMode,
    capture_page_id: int,
    signal_name: str,
) -> None:
    wizard = OpticalCalibrationWizard()
    wizard.set_mode(mode)
    spy = QSignalSpy(getattr(wizard, signal_name))
    _show(wizard, qt_app)

    wizard.next()
    assert wizard.currentId() == capture_page_id
    wizard.next()
    assert spy.count() == 1

    if mode is OpticalCalibrationMode.FLAT_FIELD:
        wizard.set_flat_field_result(True, "saved", run_id=wizard.active_run_id())
    else:
        wizard.set_lens_distortion_result(
            True,
            "saved",
            run_id=wizard.active_run_id(),
        )
    qt_app.processEvents()

    assert wizard.currentId() == wizard.RESULT_PAGE_ID
    wizard.close()


def test_failed_capture_stays_on_page_and_can_retry(qt_app: QApplication) -> None:
    wizard = OpticalCalibrationWizard()
    wizard.set_mode(OpticalCalibrationMode.FLAT_FIELD)
    spy = QSignalSpy(wizard.start_flat_field_requested)
    _show(wizard, qt_app)
    wizard.next()

    wizard.next()
    wizard.set_flat_field_result(
        False,
        "Camera frame timeout.",
        run_id=wizard.active_run_id(),
    )
    qt_app.processEvents()

    assert wizard.currentId() == wizard.FLAT_FIELD_PAGE_ID
    assert not wizard.is_running()
    assert "Camera frame timeout" in wizard.currentPage().status_text()

    wizard.next()
    assert spy.count() == 2
    wizard.set_flat_field_result(False, "stopped", run_id=wizard.active_run_id())
    wizard.close()


def test_running_capture_rejects_close(qt_app: QApplication) -> None:
    wizard = OpticalCalibrationWizard()
    wizard.set_mode(OpticalCalibrationMode.FLAT_FIELD)
    _show(wizard, qt_app)
    wizard.next()
    wizard.next()

    wizard.close()
    qt_app.processEvents()

    assert wizard.isVisible()
    wizard.set_flat_field_result(False, "stopped", run_id=wizard.active_run_id())
    wizard.close()


def test_prepare_during_capture_preserves_running_close_guard(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    wizard.set_mode(OpticalCalibrationMode.FLAT_FIELD)
    _show(wizard, qt_app)
    wizard.next()
    wizard.next()
    run_id = wizard.active_run_id()

    assert wizard.prepare(OpticalCalibrationMode.LENS_DISTORTION) is False
    assert wizard.is_running()
    assert wizard.active_run_id() == run_id
    assert wizard.currentId() == wizard.FLAT_FIELD_PAGE_ID

    wizard.close()
    qt_app.processEvents()
    assert wizard.isVisible()
    wizard.set_flat_field_result(False, "stopped", run_id=run_id)
    wizard.close()


def test_stale_or_duplicate_completion_does_not_unlock_capture(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    wizard.set_mode(OpticalCalibrationMode.FULL)
    _show(wizard, qt_app)
    wizard.next()
    wizard.next()
    flat_run_id = wizard.active_run_id()
    wizard.set_flat_field_result(True, "saved", run_id=flat_run_id)
    qt_app.processEvents()
    assert wizard.currentId() == wizard.LENS_DISTORTION_PAGE_ID

    wizard.next()
    lens_run_id = wizard.active_run_id()
    wizard.set_flat_field_result(False, "stale", run_id=flat_run_id)
    assert wizard.is_running()
    assert wizard.active_run_id() == lens_run_id
    assert wizard.currentId() == wizard.LENS_DISTORTION_PAGE_ID

    wizard.set_lens_distortion_result(True, "saved", run_id=lens_run_id)
    qt_app.processEvents()
    assert wizard.currentId() == wizard.RESULT_PAGE_ID
    wizard.set_lens_distortion_result(False, "duplicate", run_id=lens_run_id)
    assert wizard.currentId() == wizard.RESULT_PAGE_ID
    wizard.close()


def test_prepare_full_starts_with_flat_field(qt_app: QApplication) -> None:
    wizard = OpticalCalibrationWizard()

    assert wizard.prepare(OpticalCalibrationMode.FULL) is True
    _show(wizard, qt_app)

    assert wizard.currentId() == wizard.FLAT_FIELD_PAGE_ID
    wizard.close()


def test_objective_and_progress_are_visible(qt_app: QApplication) -> None:
    wizard = OpticalCalibrationWizard()
    wizard.set_objective("X20", flat_field_configured=True, lens_configured=False)
    wizard.set_mode(OpticalCalibrationMode.FLAT_FIELD)
    _show(wizard, qt_app)
    wizard.next()
    wizard.next()

    wizard.set_progress("Flat field: capture 4/9.")

    assert "X20" in wizard.objective_text()
    assert "capture 4/9" in wizard.currentPage().status_text()
    wizard.set_flat_field_result(False, "stopped", run_id=wizard.active_run_id())
    wizard.close()
