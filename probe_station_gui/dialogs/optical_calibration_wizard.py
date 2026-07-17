"""Operator wizard for objective flat-field and lens calibration."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)


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
        layout = QFormLayout(self)
        self._flat_label = QLabel("Not run", self)
        self._lens_label = QLabel("Not run", self)
        self._flat_label.setWordWrap(True)
        self._lens_label.setWordWrap(True)
        layout.addRow(QLabel("Flat field", self), self._flat_label)
        layout.addRow(QLabel("Lens correction", self), self._lens_label)

    def set_flat_result(self, message: str) -> None:
        self._flat_label.setText(str(message or "Complete"))

    def set_lens_result(self, message: str) -> None:
        self._lens_label.setText(str(message or "Complete"))


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
        self.resize(560, 300)

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
    ) -> None:
        accepted = self._set_capture_result(
            self.LENS_DISTORTION_PAGE_ID,
            self._lens_page,
            success,
            message,
            run_id=run_id,
        )
        if accepted and success:
            self._result_page.set_lens_result(message)

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
