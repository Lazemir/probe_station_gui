"""Dock panel for chip alignment and design-backed registration capture."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.design.model import Point2D
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox


class AlignmentPanel(QWidget):
    """Expose arbitrary matched design/stage capture rows."""

    open_design_window_requested = Signal()
    capture_point_requested = Signal(int, str)
    reset_points_requested = Signal()
    cancel_pick_requested = Signal()
    clear_registration_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._design_marks: list[Point2D | None] = []
        self._captured_points: list[Point2D | None] = [None, None]
        self._pick_slot: int | None = None
        self._registration_status = "No design registration."
        self._fit_residuals: tuple[float, float] | None = None
        self._design_labels: list[QLabel] = []
        self._stage_labels: list[QLabel] = []
        self._capture_buttons: list[QPushButton] = []

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(6)

        header_group = QGroupBox("Alignment", self)
        header_layout = QVBoxLayout(header_group)
        self._mode_label = QLabel(header_group)
        self._mode_label.setWordWrap(True)
        header_layout.addWidget(self._mode_label)
        self._status_label = QLabel(header_group)
        self._status_label.setWordWrap(True)
        header_layout.addWidget(self._status_label)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Capture mode:", header_group))
        self._capture_mode_combo = QComboBox(header_group)
        self._capture_mode_combo.addItem("Crosshair center", userData="center")
        self._capture_mode_combo.addItem("Pick in image", userData="image")
        mode_row.addWidget(self._capture_mode_combo, 1)
        header_layout.addLayout(mode_row)
        open_design_row = QHBoxLayout()
        open_design_row.addStretch(1)
        self._open_design_button = QPushButton("Open Design Window", header_group)
        open_design_row.addWidget(self._open_design_button)
        header_layout.addLayout(open_design_row)
        root_layout.addWidget(header_group)

        points_group = QGroupBox("Points", self)
        points_layout = QVBoxLayout(points_group)
        self._rows_widget = QWidget(points_group)
        self._rows_layout = QGridLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setColumnStretch(0, 1)
        self._rows_layout.setColumnStretch(1, 1)
        points_layout.addWidget(self._rows_widget)
        self._fit_status_label = QLabel(points_group)
        self._fit_status_label.setWordWrap(True)
        points_layout.addWidget(self._fit_status_label)
        self._center_label = QLabel("Center: unavailable", points_group)
        self._cursor_label = QLabel("Cursor: unavailable", points_group)
        self._center_label.setWordWrap(True)
        self._cursor_label.setWordWrap(True)
        self._center_label.setStyleSheet("QLabel { color: palette(mid); }")
        self._cursor_label.setStyleSheet("QLabel { color: palette(mid); }")
        points_layout.addWidget(self._center_label)
        points_layout.addWidget(self._cursor_label)
        footer = QHBoxLayout()
        self._reset_points_button = QPushButton("Reset Points", points_group)
        self._cancel_pick_button = QPushButton("Cancel Pick", points_group)
        footer.addWidget(self._reset_points_button)
        footer.addWidget(self._cancel_pick_button)
        points_layout.addLayout(footer)
        root_layout.addWidget(points_group)

        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        root_layout.addWidget(separator)

        registration_group = QGroupBox("Registration", self)
        registration_layout = QVBoxLayout(registration_group)
        self._registration_status_label = QLabel(registration_group)
        self._registration_status_label.setWordWrap(True)
        registration_layout.addWidget(self._registration_status_label)
        self._clear_registration_button = QPushButton(
            "Reset Design Registration", registration_group
        )
        registration_layout.addWidget(self._clear_registration_button)
        root_layout.addWidget(registration_group)
        root_layout.addStretch(1)

        self._open_design_button.clicked.connect(self.open_design_window_requested.emit)
        self._capture_mode_combo.currentIndexChanged.connect(self._refresh_ui)
        self._reset_points_button.clicked.connect(self.reset_points_requested.emit)
        self._cancel_pick_button.clicked.connect(self.cancel_pick_requested.emit)
        self._clear_registration_button.clicked.connect(
            self.clear_registration_requested.emit
        )
        self._rebuild_rows()
        self._refresh_ui()

    @property
    def row_count(self) -> int:
        return len(self._capture_buttons)

    @property
    def next_incomplete_slot(self) -> int | None:
        for index in range(len(self._design_marks)):
            if index >= len(self._captured_points) or self._captured_points[index] is None:
                return index
        return None

    @property
    def fit_ready(self) -> bool:
        return (
            len(self._design_marks) >= 2
            and len(self._captured_points) >= len(self._design_marks)
            and all(
                point is not None
                for point in self._captured_points[: len(self._design_marks)]
            )
        )

    @property
    def fit_status_label(self) -> QLabel:
        return self._fit_status_label

    def design_label(self, slot: int) -> QLabel:
        return self._design_labels[slot]

    def stage_label(self, slot: int) -> QLabel:
        return self._stage_labels[slot]

    def capture_button(self, slot: int) -> QPushButton:
        return self._capture_buttons[slot]

    def set_design_marks(self, points: Sequence[Point2D | None]) -> None:
        self._design_marks = list(points)
        desired = max(2, len(self._design_marks))
        if len(self._captured_points) < desired:
            self._captured_points.extend([None] * (desired - len(self._captured_points)))
        elif len(self._captured_points) > desired:
            self._captured_points = self._captured_points[:desired]
        self._fit_residuals = None
        self._rebuild_rows()
        self._refresh_ui()

    def set_captured_points(self, points: Sequence[Point2D | None]) -> None:
        desired = max(2, len(self._design_marks), len(points))
        self._captured_points = list(points[:desired])
        self._captured_points.extend([None] * (desired - len(self._captured_points)))
        self._fit_residuals = None
        self._rebuild_rows()
        self._refresh_ui()

    def set_pick_slot(self, slot: int | None) -> None:
        self._pick_slot = slot if slot is not None and 0 <= slot < self.row_count else None
        self._refresh_ui()

    def capture_mode(self) -> str:
        value = self._capture_mode_combo.currentData()
        return str(value) if value in {"center", "image"} else "center"

    def set_registration_status(self, text: str) -> None:
        self._registration_status = text or "No design registration."
        self._registration_status_label.setText(self._registration_status)
        self._refresh_ui()

    def set_fit_residuals(self, rms_mm: float, max_mm: float) -> None:
        self._fit_residuals = (float(rms_mm), float(max_mm))
        self._refresh_fit_status()

    def set_coordinate_labels(self, center_text: str, cursor_text: str) -> None:
        self._center_label.setText(center_text)
        self._cursor_label.setText(cursor_text)

    def _rebuild_rows(self) -> None:
        for widgets in (
            self._design_labels,
            self._stage_labels,
            self._capture_buttons,
        ):
            for widget in widgets:
                self._rows_layout.removeWidget(widget)
                widget.deleteLater()
            widgets.clear()
        count = max(2, len(self._design_marks), len(self._captured_points))
        for slot in range(count):
            design_label = QLabel(self._rows_widget)
            design_label.setWordWrap(True)
            design_label.setStyleSheet("QLabel { font-weight: 600; }")
            stage_label = QLabel(self._rows_widget)
            stage_label.setWordWrap(True)
            stage_label.setStyleSheet("QLabel { font-weight: 600; }")
            button = QPushButton(self._rows_widget)
            button.clicked.connect(
                lambda _checked=False, index=slot: self.capture_point_requested.emit(
                    index,
                    self.capture_mode(),
                )
            )
            self._design_labels.append(design_label)
            self._stage_labels.append(stage_label)
            self._capture_buttons.append(button)
            self._rows_layout.addWidget(design_label, slot, 0)
            self._rows_layout.addWidget(stage_label, slot, 1)
            self._rows_layout.addWidget(button, slot, 2)

    def _refresh_ui(self, *_unused: object) -> None:
        using_design_marks = len(self._design_marks) >= 2
        self._mode_label.setText("Mode: design-linked" if using_design_marks else "Mode: quick")
        next_slot = self.next_incomplete_slot if using_design_marks else None
        if self._pick_slot is not None:
            self._status_label.setText(f"Picking S{self._pick_slot + 1} in image.")
        elif using_design_marks and next_slot is not None:
            self._status_label.setText(f"Capture S{next_slot + 1} next.")
        elif using_design_marks:
            self._status_label.setText("All stage points are captured.")
        elif any(point is not None for point in self._captured_points):
            self._status_label.setText("Quick alignment points captured.")
        else:
            self._status_label.setText("No points captured.")

        for slot, (design_label, stage_label, button) in enumerate(
            zip(
                self._design_labels,
                self._stage_labels,
                self._capture_buttons,
                strict=True,
            )
        ):
            design_point = (
                self._design_marks[slot] if slot < len(self._design_marks) else None
            )
            stage_point = (
                self._captured_points[slot]
                if slot < len(self._captured_points)
                else None
            )
            design_label.setText(self._format_point(f"D{slot + 1}", design_point))
            stage_label.setText(self._format_point(f"S{slot + 1}", stage_point))
            button.setText("Replace" if stage_point is not None else "Capture")
            button.setEnabled(not using_design_marks or slot < len(self._design_marks))
            button.setDefault(slot == next_slot)

        has_points = any(point is not None for point in self._captured_points)
        self._reset_points_button.setEnabled(has_points)
        self._cancel_pick_button.setEnabled(self._pick_slot is not None)
        self._clear_registration_button.setEnabled(bool(self._design_marks))
        self._registration_status_label.setText(self._registration_status)
        self._refresh_fit_status()

    def _refresh_fit_status(self) -> None:
        if self._fit_residuals is not None:
            rms_mm, max_mm = self._fit_residuals
            self._fit_status_label.setText(
                f"Fit RMS {rms_mm:.4f} mm, max {max_mm:.4f} mm."
            )
        elif self.fit_ready:
            self._fit_status_label.setText("Ready to fit all captured pairs.")
        elif len(self._design_marks) >= 2:
            remaining = sum(
                1
                for index in range(len(self._design_marks))
                if index >= len(self._captured_points)
                or self._captured_points[index] is None
            )
            self._fit_status_label.setText(f"Capture {remaining} remaining stage point(s).")
        else:
            self._fit_status_label.setText("Choose at least two design points with Align.")

    @staticmethod
    def _format_point(label: str, point: Point2D | None) -> str:
        if point is None:
            return f"{label}: not set"
        return f"{label}: X={point[0]:.3f}, Y={point[1]:.3f}"


__all__ = ["AlignmentPanel"]
