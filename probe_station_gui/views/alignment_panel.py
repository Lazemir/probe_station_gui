"""Dock panel for chip alignment and design-backed registration capture."""

from __future__ import annotations

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

from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox

from ..design_model import Point2D


class AlignmentPanel(QWidget):
    """Expose stage-side alignment capture controls."""

    open_design_window_requested = Signal()
    capture_point_requested = Signal(int, str)
    reset_points_requested = Signal()
    cancel_pick_requested = Signal()
    clear_registration_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._design_marks: list[Point2D | None] = [None, None]
        self._captured_points: list[Point2D | None] = [None, None]
        self._pick_slot: int | None = None
        self._registration_status = "No design registration."

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
        points_layout = QGridLayout(points_group)
        self._design_point_1_label = QLabel(points_group)
        self._design_point_1_label.setWordWrap(True)
        self._design_point_1_label.setStyleSheet("QLabel { font-weight: 600; }")
        self._design_point_2_label = QLabel(points_group)
        self._design_point_2_label.setWordWrap(True)
        self._design_point_2_label.setStyleSheet("QLabel { font-weight: 600; }")
        self._captured_point_1_label = QLabel(points_group)
        self._captured_point_1_label.setWordWrap(True)
        self._captured_point_1_label.setStyleSheet("QLabel { font-weight: 600; }")
        self._captured_point_2_label = QLabel(points_group)
        self._captured_point_2_label.setWordWrap(True)
        self._captured_point_2_label.setStyleSheet("QLabel { font-weight: 600; }")
        self._center_label = QLabel(points_group)
        self._center_label.setWordWrap(True)
        self._cursor_label = QLabel(points_group)
        self._cursor_label.setWordWrap(True)
        self._center_label.setStyleSheet("QLabel { color: palette(mid); }")
        self._cursor_label.setStyleSheet("QLabel { color: palette(mid); }")

        self._set_point_1_button = QPushButton("Set Point 1", points_group)
        self._set_point_2_button = QPushButton("Set Point 2", points_group)
        self._reset_points_button = QPushButton("Reset Points", points_group)
        self._cancel_pick_button = QPushButton("Cancel Pick", points_group)

        points_layout.addWidget(self._design_point_1_label, 0, 0, 1, 2)
        points_layout.addWidget(self._design_point_2_label, 1, 0, 1, 2)
        points_layout.addWidget(self._captured_point_1_label, 2, 0, 1, 2)
        points_layout.addWidget(self._captured_point_2_label, 3, 0, 1, 2)
        points_layout.addWidget(self._set_point_1_button, 4, 0, 1, 2)
        points_layout.addWidget(self._set_point_2_button, 5, 0, 1, 2)
        points_layout.addWidget(self._center_label, 6, 0, 1, 2)
        points_layout.addWidget(self._cursor_label, 7, 0, 1, 2)
        points_layout.addWidget(self._reset_points_button, 8, 0)
        points_layout.addWidget(self._cancel_pick_button, 8, 1)
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
        self._capture_mode_combo.currentIndexChanged.connect(self._on_capture_mode_changed)
        self._set_point_1_button.clicked.connect(
            lambda: self.capture_point_requested.emit(0, self.capture_mode())
        )
        self._set_point_2_button.clicked.connect(
            lambda: self.capture_point_requested.emit(1, self.capture_mode())
        )
        self._reset_points_button.clicked.connect(self.reset_points_requested.emit)
        self._cancel_pick_button.clicked.connect(self.cancel_pick_requested.emit)
        self._clear_registration_button.clicked.connect(
            self.clear_registration_requested.emit
        )

        self._center_label.setText("Center: unavailable")
        self._cursor_label.setText("Cursor: unavailable")
        self._refresh_ui()

    def set_design_marks(self, points: list[Point2D | None]) -> None:
        self._design_marks = list(points[:2])
        while len(self._design_marks) < 2:
            self._design_marks.append(None)
        self._refresh_ui()

    def set_captured_points(self, points: list[Point2D | None]) -> None:
        self._captured_points = list(points[:2])
        while len(self._captured_points) < 2:
            self._captured_points.append(None)
        self._refresh_ui()

    def set_pick_slot(self, slot: int | None) -> None:
        self._pick_slot = slot if slot in (0, 1) else None
        self._refresh_ui()

    def capture_mode(self) -> str:
        value = self._capture_mode_combo.currentData()
        return str(value) if value in {"center", "image"} else "center"

    def set_registration_status(self, text: str) -> None:
        self._registration_status = text or "No design registration."
        self._registration_status_label.setText(self._registration_status)
        self._refresh_ui()

    def set_coordinate_labels(self, center_text: str, cursor_text: str) -> None:
        self._center_label.setText(center_text)
        self._cursor_label.setText(cursor_text)

    def _refresh_ui(self) -> None:
        using_design_marks = all(point is not None for point in self._design_marks)
        if using_design_marks:
            self._mode_label.setText("Mode: design-linked")
        else:
            self._mode_label.setText("Mode: quick")

        if self._pick_slot is not None:
            self._status_label.setText(f"Picking point {self._pick_slot + 1} in image.")
        elif self._captured_points[0] is None and self._captured_points[1] is None:
            self._status_label.setText("No points captured.")
        elif self._captured_points[0] is None:
            self._status_label.setText("Point 2 is set.")
        elif self._captured_points[1] is None:
            self._status_label.setText("Point 1 is set.")
        else:
            self._status_label.setText("Both points are set.")

        self._design_point_1_label.setText(
            self._format_point("D1", self._design_marks[0])
        )
        self._design_point_2_label.setText(
            self._format_point("D2", self._design_marks[1])
        )
        self._captured_point_1_label.setText(
            self._format_point("S1", self._captured_points[0])
        )
        self._captured_point_2_label.setText(
            self._format_point("S2", self._captured_points[1])
        )

        self._set_point_1_button.setText(
            "Picking Point 1..." if self._pick_slot == 0 else "Set Point 1"
        )
        self._set_point_2_button.setText(
            "Picking Point 2..." if self._pick_slot == 1 else "Set Point 2"
        )

        has_points = any(point is not None for point in self._captured_points)
        has_design_state = any(point is not None for point in self._design_marks)
        self._reset_points_button.setEnabled(has_points)
        self._cancel_pick_button.setEnabled(self._pick_slot is not None)
        self._clear_registration_button.setEnabled(has_design_state)

    def _on_capture_mode_changed(self) -> None:
        self._refresh_ui()

    @staticmethod
    def _format_point(label: str, point: Point2D | None) -> str:
        if point is None:
            return f"{label}: not set"
        return f"{label}: X={point[0]:.2f}, Y={point[1]:.2f}"


__all__ = ["AlignmentPanel"]
