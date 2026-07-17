from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage
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


def _preview(color: QColor) -> QImage:
    image = QImage(320, 180, QImage.Format_RGBA8888)
    image.fill(color)
    return image


def _has_no_pixmap(label) -> bool:
    pixmap = label.pixmap()
    return pixmap is None or pixmap.isNull()


def _start_lens_capture(
    wizard: OpticalCalibrationWizard,
    qt_app: QApplication,
) -> int:
    assert wizard.prepare(OpticalCalibrationMode.LENS_DISTORTION)
    _show(wizard, qt_app)
    assert wizard.currentId() == wizard.LENS_DISTORTION_PAGE_ID
    wizard.next()
    run_id = wizard.active_run_id()
    assert run_id is not None
    return run_id


def _complete_lens_capture(
    wizard: OpticalCalibrationWizard,
    qt_app: QApplication,
    *,
    run_id: int,
    before: QImage,
    after: QImage,
) -> None:
    wizard.set_lens_distortion_result(
        True,
        "Lens correction saved.",
        run_id=run_id,
        before_preview=before,
        after_preview=after,
    )
    qt_app.processEvents()
    assert wizard.currentId() == wizard.RESULT_PAGE_ID


def test_full_mode_runs_flat_field_then_lens_distortion(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    assert wizard.wizardStyle() == QWizard.ModernStyle
    wizard.set_mode(OpticalCalibrationMode.FULL)
    flat_spy = QSignalSpy(wizard.start_flat_field_requested)
    lens_spy = QSignalSpy(wizard.start_lens_distortion_requested)
    _show(wizard, qt_app)

    wizard.next()
    assert wizard.currentId() == wizard.FLAT_FIELD_PAGE_ID

    wizard.next()
    assert flat_spy.count() == 1
    assert len(flat_spy.at(0)) == 0
    assert wizard.currentId() == wizard.FLAT_FIELD_PAGE_ID
    assert wizard.is_running()

    wizard.set_flat_field_result(True, "Flat field saved.")
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


def test_stale_progress_does_not_overwrite_current_capture(
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
    wizard.next()
    lens_run_id = wizard.active_run_id()

    assert wizard.set_progress("stale flat progress", run_id=flat_run_id) is False
    assert "stale" not in wizard.currentPage().status_text()
    assert wizard.set_progress("Lens: capture 2/9", run_id=lens_run_id) is True
    assert "capture 2/9" in wizard.currentPage().status_text()

    wizard.set_lens_distortion_result(False, "stopped", run_id=lens_run_id)
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


def test_lens_result_displays_copied_previews_in_equal_bounds(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    before = _preview(QColor("red"))
    after = _preview(QColor("green"))
    run_id = _start_lens_capture(wizard, qt_app)

    _complete_lens_capture(
        wizard,
        qt_app,
        run_id=run_id,
        before=before,
        after=after,
    )

    result = wizard._result_page
    assert result._comparison_container.isVisible()
    before_pixmap = result._before_preview_label.pixmap()
    after_pixmap = result._after_preview_label.pixmap()
    assert before_pixmap is not None and not before_pixmap.isNull()
    assert after_pixmap is not None and not after_pixmap.isNull()
    assert result._before_preview_source is not before
    assert result._after_preview_source is not after
    assert result._before_preview_label.contentsRect().size() == (
        result._after_preview_label.contentsRect().size()
    )
    assert before_pixmap.size() == after_pixmap.size()
    assert before_pixmap.toImage().pixelColor(0, 0) == QColor("red")
    assert after_pixmap.toImage().pixelColor(0, 0) == QColor("green")
    wizard.close()


def test_lens_preview_pixmaps_rescale_with_wizard_resize(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    run_id = _start_lens_capture(wizard, qt_app)
    _complete_lens_capture(
        wizard,
        qt_app,
        run_id=run_id,
        before=_preview(QColor("red")),
        after=_preview(QColor("green")),
    )
    result = wizard._result_page
    original_bounds = result._before_preview_label.contentsRect().size()

    wizard.resize(1040, 720)
    qt_app.processEvents()

    before_pixmap = result._before_preview_label.pixmap()
    after_pixmap = result._after_preview_label.pixmap()
    assert result._before_preview_label.contentsRect().size() == (
        result._after_preview_label.contentsRect().size()
    )
    assert result._before_preview_label.contentsRect().size().width() > (
        original_bounds.width()
    )
    assert before_pixmap is not None and not before_pixmap.isNull()
    assert after_pixmap is not None and not after_pixmap.isNull()
    assert before_pixmap.width() <= result._before_preview_label.contentsRect().width()
    assert before_pixmap.height() <= result._before_preview_label.contentsRect().height()
    assert after_pixmap.width() <= result._after_preview_label.contentsRect().width()
    assert after_pixmap.height() <= result._after_preview_label.contentsRect().height()
    wizard.close()


def test_prepare_clears_lens_previews(qt_app: QApplication) -> None:
    wizard = OpticalCalibrationWizard()
    run_id = _start_lens_capture(wizard, qt_app)
    _complete_lens_capture(
        wizard,
        qt_app,
        run_id=run_id,
        before=_preview(QColor("red")),
        after=_preview(QColor("green")),
    )

    assert wizard.prepare(OpticalCalibrationMode.LENS_DISTORTION)

    result = wizard._result_page
    assert result._comparison_container.isHidden()
    assert result._before_preview_source is None
    assert result._after_preview_source is None
    assert _has_no_pixmap(result._before_preview_label)
    assert _has_no_pixmap(result._after_preview_label)
    wizard.close()


def test_stale_lens_result_cannot_overwrite_current_previews(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    first_run_id = _start_lens_capture(wizard, qt_app)
    _complete_lens_capture(
        wizard,
        qt_app,
        run_id=first_run_id,
        before=_preview(QColor("red")),
        after=_preview(QColor("green")),
    )

    wizard.back()
    qt_app.processEvents()
    assert wizard.currentId() == wizard.LENS_DISTORTION_PAGE_ID
    wizard._completed_pages.discard(wizard.LENS_DISTORTION_PAGE_ID)
    wizard.next()
    current_run_id = wizard.active_run_id()
    assert current_run_id is not None
    assert current_run_id != first_run_id
    assert wizard.is_running()

    wizard.set_lens_distortion_result(
        True,
        "Stale result.",
        run_id=first_run_id,
        before_preview=_preview(QColor("red")),
        after_preview=_preview(QColor("green")),
    )

    result = wizard._result_page
    assert wizard.is_running()
    assert wizard.active_run_id() == current_run_id
    assert result._before_preview_label.pixmap().toImage().pixelColor(0, 0) == QColor(
        "red"
    )
    assert result._after_preview_label.pixmap().toImage().pixelColor(0, 0) == QColor(
        "green"
    )
    wizard.set_lens_distortion_result(False, "stopped", run_id=current_run_id)
    wizard.close()


def test_failed_lens_result_stays_on_capture_page_and_clears_previews(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    run_id = _start_lens_capture(wizard, qt_app)

    wizard.set_lens_distortion_result(
        False,
        "Calibration failed.",
        run_id=run_id,
        before_preview=_preview(QColor("red")),
        after_preview=_preview(QColor("green")),
    )
    qt_app.processEvents()

    result = wizard._result_page
    assert wizard.currentId() == wizard.LENS_DISTORTION_PAGE_ID
    assert result._before_preview_source is None
    assert result._after_preview_source is None
    assert _has_no_pixmap(result._after_preview_label)
    wizard.close()


def test_current_failed_lens_result_clears_and_hides_rendered_previews(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    run_id = _start_lens_capture(wizard, qt_app)
    result = wizard._result_page
    result.set_lens_result(
        "Previous calibration.",
        before_preview=_preview(QColor("red")),
        after_preview=_preview(QColor("green")),
    )
    qt_app.processEvents()
    assert not result._comparison_container.isHidden()
    assert not _has_no_pixmap(result._before_preview_label)
    assert not _has_no_pixmap(result._after_preview_label)

    wizard.set_lens_distortion_result(False, "Calibration failed.", run_id=run_id)
    qt_app.processEvents()

    assert result._comparison_container.isHidden()
    assert result._before_preview_source is None
    assert result._after_preview_source is None
    assert _has_no_pixmap(result._before_preview_label)
    assert _has_no_pixmap(result._after_preview_label)
    wizard.close()


def test_flat_field_result_hides_empty_lens_comparison(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    wizard.set_mode(OpticalCalibrationMode.FLAT_FIELD)
    _show(wizard, qt_app)
    wizard.next()
    wizard.next()
    wizard.set_flat_field_result(True, "Flat field saved.", run_id=wizard.active_run_id())
    qt_app.processEvents()

    result = wizard._result_page
    assert result._comparison_container.isHidden()
    assert _has_no_pixmap(result._before_preview_label)
    assert _has_no_pixmap(result._after_preview_label)
    wizard.close()
