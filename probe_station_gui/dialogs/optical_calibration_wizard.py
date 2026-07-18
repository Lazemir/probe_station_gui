"""Operator wizard for objective flat-field and lens calibration."""

from __future__ import annotations

from enum import Enum
import math

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCloseEvent, QImage, QPaintEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWizard,
    QWizardPage,
    QWidget,
)


class _SeamLegendIcon(QWidget):
    """Pictogram showing the cells compared across one seam orientation."""

    _CELL_GAP = 2
    _CENTER_COLOR = QColor(42, 47, 54)
    _INACTIVE_COLOR = QColor(137, 144, 153)

    def __init__(
        self,
        highlighted_cells: tuple[tuple[int, int], ...],
        color: QColor,
        tooltip: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._highlighted_cells = frozenset(highlighted_cells)
        self._color = QColor(color)
        self.setFixedSize(28, 28)
        self.setToolTip(tooltip)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt virtual method
        del event
        painter = QPainter(self)
        cell_size = (self.width() - (2 * self._CELL_GAP)) // 3
        for row in range(3):
            for column in range(3):
                if (row, column) == (1, 1):
                    color = self._CENTER_COLOR
                elif (row, column) in self._highlighted_cells:
                    color = self._color
                else:
                    color = self._INACTIVE_COLOR
                painter.fillRect(
                    column * (cell_size + self._CELL_GAP),
                    row * (cell_size + self._CELL_GAP),
                    cell_size,
                    cell_size,
                    color,
                )


def _format_residual_metrics(metrics: tuple[float, float]) -> str:
    mean_px, max_px = metrics
    if not all(math.isfinite(value) and value >= 0.0 for value in metrics):
        raise ValueError(
            "Lens calibration preview metrics must be finite and non-negative."
        )
    return f"{mean_px:.2f} px mean · {max_px:.2f} px max"


class OpticalCalibrationMode(str, Enum):
    """Calibration stages selected by the operator."""

    FULL = "full"
    FLAT_FIELD = "flat_field"
    LENS_DISTORTION = "lens_distortion"


class _ModePage(QWizardPage):
    def __init__(self, parent: QWizard | None = None) -> None:
        super().__init__(parent)
        self.setTitle("Optical Calibration")

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._objective_label = QLabel("--", self)
        self._objective_label.setTextInteractionFlags(
            self._objective_label.textInteractionFlags()
        )
        self._flat_status_label = QLabel("Not configured", self)
        self._lens_status_label = QLabel("Not configured", self)
        form.addRow(QLabel("Objective", self), self._objective_label)
        form.addRow(QLabel("Flat field", self), self._flat_status_label)
        form.addRow(QLabel("Lens correction", self), self._lens_status_label)
        layout.addLayout(form)

        self._button_group = QButtonGroup(self)
        self._buttons: dict[OpticalCalibrationMode, QRadioButton] = {}
        for mode, label in (
            (OpticalCalibrationMode.FULL, "Full calibration"),
            (OpticalCalibrationMode.FLAT_FIELD, "Flat field only"),
            (OpticalCalibrationMode.LENS_DISTORTION, "Lens distortion only"),
        ):
            button = QRadioButton(label, self)
            self._button_group.addButton(button)
            self._buttons[mode] = button
            layout.addWidget(button)
        self._buttons[OpticalCalibrationMode.FULL].setChecked(True)
        layout.addStretch(1)

    def mode(self) -> OpticalCalibrationMode:
        for mode, button in self._buttons.items():
            if button.isChecked():
                return mode
        return OpticalCalibrationMode.FULL

    def set_mode(self, mode: OpticalCalibrationMode) -> None:
        self._buttons[OpticalCalibrationMode(mode)].setChecked(True)

    def set_objective(
        self,
        name: str,
        *,
        flat_field_configured: bool,
        lens_configured: bool,
    ) -> None:
        self._objective_label.setText(str(name or "--"))
        self._flat_status_label.setText(
            "Configured" if flat_field_configured else "Not configured"
        )
        self._lens_status_label.setText(
            "Configured" if lens_configured else "Not configured"
        )

    def objective_text(self) -> str:
        return self._objective_label.text()

    def nextId(self) -> int:  # noqa: N802 - Qt virtual method
        wizard = self.wizard()
        if not isinstance(wizard, OpticalCalibrationWizard):
            return -1
        if self.mode() is OpticalCalibrationMode.LENS_DISTORTION:
            return wizard.LENS_DISTORTION_PAGE_ID
        return wizard.FLAT_FIELD_PAGE_ID


class _CapturePage(QWizardPage):
    def __init__(
        self,
        title: str,
        instruction: str,
        *,
        next_page_id: int,
        parent: QWizard | None = None,
    ) -> None:
        super().__init__(parent)
        self._next_page_id = int(next_page_id)
        self.setTitle(title)
        layout = QVBoxLayout(self)
        instruction_label = QLabel(instruction, self)
        instruction_label.setWordWrap(True)
        self._status_label = QLabel("Ready.", self)
        self._status_label.setWordWrap(True)
        layout.addWidget(instruction_label)
        layout.addWidget(self._status_label)
        layout.addStretch(1)

    def set_status(self, message: str) -> None:
        self._status_label.setText(str(message or "Ready."))

    def status_text(self) -> str:
        return self._status_label.text()

    def nextId(self) -> int:  # noqa: N802 - Qt virtual method
        return self._next_page_id


class _FlatFieldPage(_CapturePage):
    def nextId(self) -> int:  # noqa: N802 - Qt virtual method
        wizard = self.wizard()
        if (
            isinstance(wizard, OpticalCalibrationWizard)
            and wizard.mode() is OpticalCalibrationMode.FULL
        ):
            return wizard.LENS_DISTORTION_PAGE_ID
        return super().nextId()


class _ResultPage(QWizardPage):
    def __init__(self, parent: QWizard | None = None) -> None:
        super().__init__(parent)
        self.setTitle("Calibration Complete")
        layout = QVBoxLayout(self)
        results_layout = QFormLayout()
        self._flat_label = QLabel("Not run", self)
        self._lens_label = QLabel("Not run", self)
        self._flat_label.setWordWrap(True)
        self._lens_label.setWordWrap(True)
        results_layout.addRow(QLabel("Flat field", self), self._flat_label)
        results_layout.addRow(QLabel("Lens correction", self), self._lens_label)
        layout.addLayout(results_layout)

        self._comparison_container = QWidget(self)
        preview_layout = QGridLayout(self._comparison_container)
        preview_layout.setColumnStretch(0, 1)
        preview_layout.setColumnStretch(1, 1)
        preview_layout.setRowStretch(1, 1)
        self._without_heading = QLabel("Without calibration", self)
        self._with_heading = QLabel("With calibration", self)
        self._without_metrics = QLabel(self)
        self._with_metrics = QLabel(self)
        without_heading_layout = QHBoxLayout()
        with_heading_layout = QHBoxLayout()
        for heading, metrics, heading_layout in (
            (
                self._without_heading,
                self._without_metrics,
                without_heading_layout,
            ),
            (self._with_heading, self._with_metrics, with_heading_layout),
        ):
            heading.setAlignment(Qt.AlignCenter)
            metrics.setAlignment(Qt.AlignCenter)
            heading_layout.addWidget(heading)
            heading_layout.addWidget(metrics)
        self._before_preview_label = self._create_preview_label()
        self._after_preview_label = self._create_preview_label()
        preview_layout.addLayout(without_heading_layout, 0, 0)
        preview_layout.addLayout(with_heading_layout, 0, 1)
        preview_layout.addWidget(self._before_preview_label, 1, 0)
        preview_layout.addWidget(self._after_preview_label, 1, 1)
        seam_legend_layout = QHBoxLayout()
        seam_legend_layout.addStretch(1)
        self._seam_legend_icons = [
            _SeamLegendIcon(
                ((1, 0), (1, 2)),
                QColor(255, 63, 72),
                "Horizontal seams: center compared with left and right.",
                self._comparison_container,
            ),
            _SeamLegendIcon(
                ((0, 1), (2, 1)),
                QColor(45, 219, 104),
                "Vertical seams: center compared with top and bottom.",
                self._comparison_container,
            ),
            _SeamLegendIcon(
                ((0, 0), (0, 2), (2, 0), (2, 2)),
                QColor(67, 132, 255),
                "Corner seams: center compared with four corners.",
                self._comparison_container,
            ),
        ]
        for icon in self._seam_legend_icons:
            seam_legend_layout.addWidget(icon)
        seam_legend_layout.addStretch(1)
        preview_layout.addLayout(seam_legend_layout, 2, 0, 1, 2)
        layout.addWidget(self._comparison_container, 1)
        self._comparison_container.hide()

        self._before_preview_source: QImage | None = None
        self._after_preview_source: QImage | None = None

    def _create_preview_label(self) -> QLabel:
        label = QLabel(self)
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumSize(280, 180)
        label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        return label

    def set_flat_result(self, message: str) -> None:
        self._flat_label.setText(str(message or "Complete"))

    def set_lens_result(
        self,
        message: str,
        *,
        before_preview: QImage | None = None,
        after_preview: QImage | None = None,
        without_calibration_metrics: tuple[float, float] | None = None,
        with_calibration_metrics: tuple[float, float] | None = None,
    ) -> None:
        self._lens_label.setText(str(message or "Complete"))
        if (
            before_preview is None
            or after_preview is None
            or before_preview.isNull()
            or after_preview.isNull()
        ):
            self.clear_lens_previews()
            return
        if (
            without_calibration_metrics is None
            or with_calibration_metrics is None
        ):
            self.clear_lens_previews()
            raise ValueError(
                "Lens calibration preview metrics must be finite and non-negative."
            )
        try:
            without_metrics_text = _format_residual_metrics(
                without_calibration_metrics
            )
            with_metrics_text = _format_residual_metrics(with_calibration_metrics)
        except (TypeError, ValueError):
            self.clear_lens_previews()
            raise
        self._before_preview_source = before_preview.copy()
        self._after_preview_source = after_preview.copy()
        self._without_metrics.setText(without_metrics_text)
        self._with_metrics.setText(with_metrics_text)
        self._comparison_container.show()
        self._refresh_preview_pixmaps()

    def clear_lens_previews(self) -> None:
        self._before_preview_source = None
        self._after_preview_source = None
        self._before_preview_label.clear()
        self._after_preview_label.clear()
        self._without_metrics.clear()
        self._with_metrics.clear()
        self._comparison_container.hide()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().resizeEvent(event)
        self._refresh_preview_pixmaps()

    def _refresh_preview_pixmaps(self) -> None:
        self._set_preview_pixmap(
            self._before_preview_label,
            self._before_preview_source,
        )
        self._set_preview_pixmap(
            self._after_preview_label,
            self._after_preview_source,
        )

    @staticmethod
    def _set_preview_pixmap(label: QLabel, source: QImage | None) -> None:
        if source is None or source.isNull():
            label.clear()
            return
        bounds = label.contentsRect().size()
        if bounds.isEmpty():
            return
        label.setPixmap(
            QPixmap.fromImage(source).scaled(
                bounds,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )


class OpticalCalibrationWizard(QWizard):
    """Guide the operator through one or both optical calibrations."""

    MODE_PAGE_ID = 0
    FLAT_FIELD_PAGE_ID = 1
    LENS_DISTORTION_PAGE_ID = 2
    RESULT_PAGE_ID = 3

    start_flat_field_requested = Signal()
    start_lens_distortion_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Optical Calibration")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.NoBackButtonOnLastPage, True)
        self.setModal(False)
        self.setMinimumSize(680, 460)
        self.resize(820, 580)

        self._mode_page = _ModePage(self)
        self._flat_page = _FlatFieldPage(
            "Flat Field",
            "Center a clean, feature-free area of the current substrate.",
            next_page_id=self.RESULT_PAGE_ID,
            parent=self,
        )
        self._lens_page = _CapturePage(
            "Lens Distortion",
            "Center the calibration structure in the camera view.",
            next_page_id=self.RESULT_PAGE_ID,
            parent=self,
        )
        self._result_page = _ResultPage(self)
        self.setPage(self.MODE_PAGE_ID, self._mode_page)
        self.setPage(self.FLAT_FIELD_PAGE_ID, self._flat_page)
        self.setPage(self.LENS_DISTORTION_PAGE_ID, self._lens_page)
        self.setPage(self.RESULT_PAGE_ID, self._result_page)
        self.setStartId(self.MODE_PAGE_ID)

        self._running = False
        self._run_counter = 0
        self._active_run_id: int | None = None
        self._active_page_id: int | None = None
        self._completed_pages: set[int] = set()
        self.currentIdChanged.connect(self._update_button_text)
        self._update_button_text(self.currentId())

    def prepare(self, mode: OpticalCalibrationMode | None = None) -> bool:
        """Reset results and optionally open directly at one calibration stage."""

        if self._running:
            return False
        self._active_run_id = None
        self._active_page_id = None
        self._completed_pages.clear()
        self._flat_page.set_status("Ready.")
        self._lens_page.set_status("Ready.")
        self._result_page.set_flat_result("Not run")
        self._result_page.set_lens_result("Not run")
        self._result_page.clear_lens_previews()
        if mode is None:
            self.setStartId(self.MODE_PAGE_ID)
        else:
            selected = OpticalCalibrationMode(mode)
            self.set_mode(selected)
            self.setStartId(
                self.LENS_DISTORTION_PAGE_ID
                if selected is OpticalCalibrationMode.LENS_DISTORTION
                else self.FLAT_FIELD_PAGE_ID
            )
        self.restart()
        self._set_navigation_enabled(True)
        return True

    def mode(self) -> OpticalCalibrationMode:
        return self._mode_page.mode()

    def set_mode(self, mode: OpticalCalibrationMode) -> None:
        self._mode_page.set_mode(OpticalCalibrationMode(mode))

    def set_objective(
        self,
        name: str,
        *,
        flat_field_configured: bool,
        lens_configured: bool,
    ) -> None:
        self._mode_page.set_objective(
            name,
            flat_field_configured=flat_field_configured,
            lens_configured=lens_configured,
        )

    def objective_text(self) -> str:
        return self._mode_page.objective_text()

    def is_running(self) -> bool:
        return self._running

    def active_run_id(self) -> int | None:
        return self._active_run_id

    def set_progress(self, message: str, *, run_id: int | None = None) -> bool:
        if not self._running:
            return False
        if run_id is not None and run_id != self._active_run_id:
            return False
        page = self.currentPage()
        if isinstance(page, _CapturePage):
            page.set_status(message)
            return True
        return False

    def set_flat_field_result(
        self,
        success: bool,
        message: str,
        *,
        run_id: int | None = None,
    ) -> None:
        accepted = self._set_capture_result(
            self.FLAT_FIELD_PAGE_ID,
            self._flat_page,
            success,
            message,
            run_id=run_id,
        )
        if accepted and success:
            self._result_page.set_flat_result(message)

    def set_lens_distortion_result(
        self,
        success: bool,
        message: str,
        *,
        run_id: int | None = None,
        before_preview: QImage | None = None,
        after_preview: QImage | None = None,
        without_calibration_metrics: tuple[float, float] | None = None,
        with_calibration_metrics: tuple[float, float] | None = None,
    ) -> None:
        accepted = self._set_capture_result(
            self.LENS_DISTORTION_PAGE_ID,
            self._lens_page,
            success,
            message,
            run_id=run_id,
        )
        if not accepted:
            return
        if success:
            self._result_page.set_lens_result(
                message,
                before_preview=before_preview,
                after_preview=after_preview,
                without_calibration_metrics=without_calibration_metrics,
                with_calibration_metrics=with_calibration_metrics,
            )
            return
        self._result_page.clear_lens_previews()

    def validateCurrentPage(self) -> bool:  # noqa: N802 - Qt virtual method
        page_id = self.currentId()
        if page_id in self._completed_pages:
            return True
        if page_id == self.FLAT_FIELD_PAGE_ID:
            self._start_capture(self._flat_page, self.start_flat_field_requested)
            return False
        if page_id == self.LENS_DISTORTION_PAGE_ID:
            self._start_capture(
                self._lens_page,
                self.start_lens_distortion_requested,
            )
            return False
        return super().validateCurrentPage()

    def reject(self) -> None:
        if self._running:
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt virtual method
        if self._running:
            event.ignore()
            return
        super().closeEvent(event)

    def _start_capture(self, page: _CapturePage, signal: object) -> None:
        if self._running:
            return
        page.set_status("Starting.")
        self._run_counter += 1
        run_id = self._run_counter
        self._running = True
        self._active_run_id = run_id
        self._active_page_id = self.currentId()
        self._set_navigation_enabled(False)
        signal.emit()

    def _set_capture_result(
        self,
        page_id: int,
        page: _CapturePage,
        success: bool,
        message: str,
        *,
        run_id: int | None,
    ) -> bool:
        if (
            not self._running
            or self._active_page_id != page_id
            or (
                run_id is not None
                and self._active_run_id != run_id
            )
        ):
            return False
        page.set_status(message)
        self._running = False
        self._active_run_id = None
        self._active_page_id = None
        self._set_navigation_enabled(True)
        if not success:
            self._completed_pages.discard(page_id)
            return True
        self._completed_pages.add(page_id)
        if self.currentId() == page_id:
            QTimer.singleShot(
                0,
                lambda: self._advance_completed_page(page_id),
            )
        return True

    def _advance_completed_page(self, page_id: int) -> None:
        if self.currentId() == page_id and page_id in self._completed_pages:
            self.next()

    def _set_navigation_enabled(self, enabled: bool) -> None:
        for button_id in (
            QWizard.BackButton,
            QWizard.NextButton,
            QWizard.CancelButton,
            QWizard.FinishButton,
        ):
            button = self.button(button_id)
            if button is not None:
                button.setEnabled(bool(enabled))

    def _update_button_text(self, page_id: int) -> None:
        self.setButtonText(
            QWizard.NextButton,
            "Start"
            if page_id in (self.FLAT_FIELD_PAGE_ID, self.LENS_DISTORTION_PAGE_ID)
            else "Continue",
        )
        self.setButtonText(QWizard.FinishButton, "Close")


__all__ = ["OpticalCalibrationMode", "OpticalCalibrationWizard"]
