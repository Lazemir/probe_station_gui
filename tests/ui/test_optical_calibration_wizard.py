from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QLabel, QWizard

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
    without_calibration_metrics: tuple[float, float] = (2.56, 5.00),
    with_calibration_metrics: tuple[float, float] = (2.42, 6.41),
) -> None:
    wizard.set_lens_distortion_result(
        True,
        "Lens correction saved.",
        run_id=run_id,
        before_preview=before,
        after_preview=after,
        without_calibration_metrics=without_calibration_metrics,
        with_calibration_metrics=with_calibration_metrics,
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

    _complete_lens_capture(
        wizard,
        qt_app,
        run_id=wizard.active_run_id(),
        before=_preview(QColor("red")),
        after=_preview(QColor("green")),
    )
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
        _complete_lens_capture(
            wizard,
            qt_app,
            run_id=wizard.active_run_id(),
            before=_preview(QColor("red")),
            after=_preview(QColor("green")),
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


def test_running_capture_close_requests_asynchronous_cancellation(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    wizard.set_mode(OpticalCalibrationMode.FLAT_FIELD)
    cancel_spy = QSignalSpy(wizard.cancel_requested)
    _show(wizard, qt_app)
    wizard.next()
    wizard.next()
    run_id = wizard.active_run_id()

    wizard.close()
    qt_app.processEvents()

    assert not wizard.isVisible()
    assert cancel_spy.count() == 1
    assert cancel_spy.at(0) == [run_id]
    wizard.set_flat_field_result(False, "stopped", run_id=run_id)


def test_prepare_during_capture_preserves_run_identity_until_cancel_completes(
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
    assert not wizard.isVisible()
    wizard.set_flat_field_result(False, "stopped", run_id=run_id)


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

    _complete_lens_capture(
        wizard,
        qt_app,
        run_id=lens_run_id,
        before=_preview(QColor("red")),
        after=_preview(QColor("green")),
    )
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
    assert result._without_heading.text() == "Without calibration"
    assert result._with_heading.text() == "With calibration"
    assert result._without_metrics.text() == "2.56 px mean \u00b7 5.00 px max"
    assert result._with_metrics.text() == "2.42 px mean \u00b7 6.41 px max"
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


def test_lens_result_requires_both_finite_metric_pairs_for_previews(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    result = wizard._result_page

    with pytest.raises(ValueError, match="finite and non-negative"):
        result.set_lens_result(
            "Lens correction saved.",
            before_preview=_preview(QColor("red")),
            after_preview=_preview(QColor("green")),
            without_calibration_metrics=(2.56, 5.00),
        )

    qt_app.processEvents()
    assert result._comparison_container.isHidden()
    assert result._without_metrics.text() == ""
    assert result._with_metrics.text() == ""
    wizard.close()


@pytest.mark.parametrize(
    "invalid_result",
    (
        {"before_preview": QImage()},
        {"after_preview": None},
        {"without_calibration_metrics": None},
        {"with_calibration_metrics": (float("nan"), 6.41)},
    ),
    ids=(
        "null-before-preview",
        "missing-after-preview",
        "missing-without-metrics",
        "nonfinite-with-metrics",
    ),
)
def test_invalid_lens_result_does_not_complete_active_capture(
    qt_app: QApplication,
    invalid_result: dict[str, object],
) -> None:
    wizard = OpticalCalibrationWizard()
    run_id = _start_lens_capture(wizard, qt_app)
    result = wizard._result_page
    result_args: dict[str, object] = {
        "before_preview": _preview(QColor("red")),
        "after_preview": _preview(QColor("green")),
        "without_calibration_metrics": (2.56, 5.00),
        "with_calibration_metrics": (2.42, 6.41),
    }
    result_args.update(invalid_result)

    with pytest.raises(ValueError):
        wizard.set_lens_distortion_result(
            True,
            "Lens correction saved.",
            run_id=run_id,
            **result_args,
        )

    qt_app.processEvents()
    assert wizard.currentId() == wizard.LENS_DISTORTION_PAGE_ID
    assert wizard.is_running()
    assert wizard.active_run_id() == run_id
    assert wizard.LENS_DISTORTION_PAGE_ID not in wizard._completed_pages
    assert result._comparison_container.isHidden()
    wizard.set_lens_distortion_result(False, "stopped", run_id=run_id)
    wizard.close()


@pytest.mark.parametrize("metric_index", (0, 1), ids=("mean", "max"))
@pytest.mark.parametrize("metric_pair", ("without", "with"))
@pytest.mark.parametrize(
    "invalid_value",
    (-0.01, float("nan"), float("inf"), -float("inf")),
    ids=("negative", "nan", "positive-infinity", "negative-infinity"),
)
def test_lens_result_rejects_invalid_preview_metrics(
    qt_app: QApplication,
    metric_index: int,
    metric_pair: str,
    invalid_value: float,
) -> None:
    wizard = OpticalCalibrationWizard()
    result = wizard._result_page
    without_metrics = [2.56, 5.00]
    with_metrics = [2.42, 6.41]
    metric_pairs = {"without": without_metrics, "with": with_metrics}
    metric_pairs[metric_pair][metric_index] = invalid_value

    with pytest.raises(ValueError, match="finite and non-negative"):
        result.set_lens_result(
            "Lens correction saved.",
            before_preview=_preview(QColor("red")),
            after_preview=_preview(QColor("green")),
            without_calibration_metrics=tuple(without_metrics),
            with_calibration_metrics=tuple(with_metrics),
        )

    qt_app.processEvents()
    assert result._comparison_container.isHidden()
    assert result._before_preview_source is None
    assert result._after_preview_source is None
    assert _has_no_pixmap(result._before_preview_label)
    assert _has_no_pixmap(result._after_preview_label)
    assert result._without_metrics.text() == ""
    assert result._with_metrics.text() == ""
    wizard.close()


def test_lens_result_formats_signed_zero_preview_metrics_without_minus_sign(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    result = wizard._result_page

    result.set_lens_result(
        "Lens correction saved.",
        before_preview=_preview(QColor("red")),
        after_preview=_preview(QColor("green")),
        without_calibration_metrics=(-0.0, 0.0),
        with_calibration_metrics=(0.0, -0.0),
    )

    qt_app.processEvents()
    assert not result._comparison_container.isHidden()
    assert result._without_metrics.text() == "0.00 px mean · 0.00 px max"
    assert result._with_metrics.text() == "0.00 px mean · 0.00 px max"
    wizard.close()


def test_lens_result_uses_compact_metrics_without_widening_layout(
    qt_app: QApplication,
) -> None:
    wizard = OpticalCalibrationWizard()
    result = wizard._result_page

    result.set_lens_result(
        "Lens correction saved.",
        before_preview=_preview(QColor("red")),
        after_preview=_preview(QColor("green")),
        without_calibration_metrics=(1e300, 1e300),
        with_calibration_metrics=(2.42, 6.41),
    )

    qt_app.processEvents()
    assert result._without_metrics.text() == (
        "1.00e+300 px mean \u00b7 1.00e+300 px max"
    )
    assert result._without_metrics.minimumSizeHint().width() < 500
    wizard.close()


def test_lens_result_displays_three_rgb_seam_pictograms(
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
    assert len(result._seam_legend_icons) == 3
    assert [icon.toolTip() for icon in result._seam_legend_icons] == [
        "Horizontal seams: center compared with left and right.",
        "Vertical seams: center compared with top and bottom.",
        "Corner seams: center compared with four corners.",
    ]
    assert [icon.accessibleName() for icon in result._seam_legend_icons] == [
        "Horizontal seams",
        "Vertical seams",
        "Corner seams",
    ]
    assert [icon.accessibleDescription() for icon in result._seam_legend_icons] == [
        "Center compared with left and right.",
        "Center compared with top and bottom.",
        "Center compared with four corners.",
    ]
    assert not [
        label
        for label in result._comparison_container.findChildren(QLabel)
        if "seam" in label.text().lower()
    ]

    horizontal, vertical, corners = (
        icon.grab().toImage() for icon in result._seam_legend_icons
    )
    assert horizontal.pixelColor(4, 14) == QColor(255, 63, 72)
    assert horizontal.pixelColor(24, 14) == QColor(255, 63, 72)
    assert vertical.pixelColor(14, 4) == QColor(45, 219, 104)
    assert vertical.pixelColor(14, 24) == QColor(45, 219, 104)
    for x, y in ((4, 4), (24, 4), (4, 24), (24, 24)):
        assert corners.pixelColor(x, y) == QColor(67, 132, 255)
    for image in (horizontal, vertical, corners):
        center = image.pixelColor(14, 14)
        assert center.red() < 80
        assert center.green() < 80
        assert center.blue() < 80
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
    assert result._without_metrics.text() == ""
    assert result._with_metrics.text() == ""
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
        without_calibration_metrics=(99.00, 100.00),
        with_calibration_metrics=(98.00, 101.00),
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
    assert result._without_metrics.text() == "2.56 px mean \u00b7 5.00 px max"
    assert result._with_metrics.text() == "2.42 px mean \u00b7 6.41 px max"
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
        without_calibration_metrics=(2.56, 5.00),
        with_calibration_metrics=(2.42, 6.41),
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
    assert result._without_metrics.text() == ""
    assert result._with_metrics.text() == ""
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
