"""Standalone window that combines contact calibration and oscillation."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.settings_manager import SavedStagePositionSettings
from probe_station_gui.views.oscillation_panel import OscillationPanel


class ContactOscillationWindow(QWidget):
    """Modeless calibration window for chip contact and stone oscillation."""

    visibility_changed = Signal(bool)
    autofocus_requested = Signal()
    save_surface_position_requested = Signal(str)
    move_to_surface_position_requested = Signal(str)
    save_current_needle_height_requested = Signal()
    lower_needles_requested = Signal()
    raise_needles_requested = Signal()
    contact_seek_requested = Signal()
    contact_seek_cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.Window, True)
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setWindowTitle("Contact Calibration")
        self.resize(520, 760)

        self.oscillation_panel = OscillationPanel(self)

        self._current_stage_xyz: tuple[float, float, float] | None = None
        self._current_needle_height: Optional[float] = None
        self._saved_needle_height: Optional[float] = None

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        positions_group = QGroupBox("Chip / Stone Focus", self)
        positions_layout = QGridLayout(positions_group)
        positions_layout.addWidget(
            QLabel(
                "Run autofocus manually over the chip or stone, then save the current XYZ.",
                positions_group,
            ),
            0,
            0,
            1,
            4,
        )

        positions_layout.addWidget(QLabel("Current XYZ:", positions_group), 1, 0)
        self._current_xyz_label = QLabel("n/a", positions_group)
        positions_layout.addWidget(self._current_xyz_label, 1, 1, 1, 3)

        self._autofocus_button = QPushButton("Autofocus", positions_group)
        self._autofocus_button.clicked.connect(self.autofocus_requested.emit)
        positions_layout.addWidget(self._autofocus_button, 2, 0, 1, 4)

        positions_layout.addWidget(QLabel("Chip:", positions_group), 3, 0)
        self._chip_position_label = QLabel("n/a", positions_group)
        positions_layout.addWidget(self._chip_position_label, 3, 1)
        self._save_chip_button = QPushButton("Save Chip", positions_group)
        self._save_chip_button.clicked.connect(
            lambda: self.save_surface_position_requested.emit("chip")
        )
        positions_layout.addWidget(self._save_chip_button, 3, 2)
        self._go_chip_button = QPushButton("Go To Chip", positions_group)
        self._go_chip_button.clicked.connect(
            lambda: self.move_to_surface_position_requested.emit("chip")
        )
        positions_layout.addWidget(self._go_chip_button, 3, 3)

        positions_layout.addWidget(QLabel("Stone:", positions_group), 4, 0)
        self._stone_position_label = QLabel("n/a", positions_group)
        positions_layout.addWidget(self._stone_position_label, 4, 1)
        self._save_stone_button = QPushButton("Save Stone", positions_group)
        self._save_stone_button.clicked.connect(
            lambda: self.save_surface_position_requested.emit("stone")
        )
        positions_layout.addWidget(self._save_stone_button, 4, 2)
        self._go_stone_button = QPushButton("Go To Stone", positions_group)
        self._go_stone_button.clicked.connect(
            lambda: self.move_to_surface_position_requested.emit("stone")
        )
        positions_layout.addWidget(self._go_stone_button, 4, 3)

        positions_layout.addWidget(
            QLabel(
                "Safe transfer path: Z -> min(saved chip Z, saved stone Z) -> XY -> target Z.",
                positions_group,
            ),
            5,
            0,
            1,
            4,
        )
        root_layout.addWidget(positions_group)

        needle_group = QGroupBox("A-Axis Contact Seek", self)
        needle_layout = QGridLayout(needle_group)
        needle_layout.setContentsMargins(6, 6, 6, 6)
        needle_layout.addWidget(QLabel("Current lowering:", needle_group), 0, 0)
        self._current_needle_label = QLabel("n/a", needle_group)
        needle_layout.addWidget(self._current_needle_label, 0, 1, 1, 3)
        needle_layout.addWidget(QLabel("Saved lowering:", needle_group), 1, 0)
        self._saved_needle_label = QLabel("n/a", needle_group)
        needle_layout.addWidget(self._saved_needle_label, 1, 1, 1, 3)
        needle_layout.addWidget(QLabel("Last check:", needle_group), 2, 0)
        self._contact_seek_status_label = QLabel("n/a", needle_group)
        self._contact_seek_status_label.setWordWrap(True)
        needle_layout.addWidget(self._contact_seek_status_label, 2, 1, 1, 3)
        self._contact_seek_button = QPushButton("Find Contact", needle_group)
        self._contact_seek_button.clicked.connect(self.contact_seek_requested.emit)
        self._contact_seek_cancel_button = QPushButton("Cancel", needle_group)
        self._contact_seek_cancel_button.clicked.connect(
            self.contact_seek_cancel_requested.emit
        )
        self._contact_seek_cancel_button.setEnabled(False)
        needle_layout.addWidget(self._contact_seek_button, 3, 0, 1, 2)
        needle_layout.addWidget(self._contact_seek_cancel_button, 3, 2, 1, 2)
        root_layout.addWidget(needle_group)

        oscillation_group = QGroupBox("Stone Oscillation", self)
        oscillation_layout = QVBoxLayout(oscillation_group)
        oscillation_layout.setContentsMargins(6, 6, 6, 6)
        warning_layout = QHBoxLayout()
        warning_layout.addWidget(
            QLabel(
                "Use the joystick A controls to adjust needle height while oscillation is running.",
                oscillation_group,
            )
        )
        oscillation_layout.addLayout(warning_layout)
        oscillation_layout.addWidget(self.oscillation_panel)
        root_layout.addWidget(oscillation_group)

        self.set_current_stage_position(None)
        self.set_current_needle_lowering(None)
        self.set_saved_needle_height(None)
        self.set_saved_surface_position("chip", None)
        self.set_saved_surface_position("stone", None)

    def show_and_raise(self) -> None:
        """Show the window and bring it to the foreground."""

        self.show()
        self.raise_()
        self.activateWindow()

    def set_current_stage_position(
        self, position_xyz: Optional[tuple[float, float, float]]
    ) -> None:
        """Update the displayed live XYZ position."""

        self._current_stage_xyz = position_xyz
        if position_xyz is None:
            self._current_xyz_label.setText("n/a")
            self._save_chip_button.setEnabled(False)
            self._save_stone_button.setEnabled(False)
            return
        self._current_xyz_label.setText(self._format_xyz(position_xyz))
        self._save_chip_button.setEnabled(True)
        self._save_stone_button.setEnabled(True)

    def set_saved_surface_position(
        self,
        target: str,
        position: SavedStagePositionSettings | None,
    ) -> None:
        """Update one of the persisted chip/stone XYZ labels."""

        target_key = target.strip().lower()
        label = (
            self._chip_position_label
            if target_key == "chip"
            else self._stone_position_label
        )
        button = self._go_chip_button if target_key == "chip" else self._go_stone_button
        if position is None or not position.configured:
            label.setText("n/a")
            button.setEnabled(False)
            return
        label.setText(self._format_xyz((position.x_mm, position.y_mm, position.z_mm)))
        button.setEnabled(True)

    def set_current_needle_lowering(self, position_mm: Optional[float]) -> None:
        """Update the displayed current physical A-axis lowering."""

        self._current_needle_height = position_mm
        if position_mm is None:
            self._current_needle_label.setText("n/a")
            return
        self._current_needle_label.setText(f"{position_mm:.4f} mm")

    def set_saved_needle_height(self, position_mm: Optional[float]) -> None:
        """Update the displayed saved physical down height."""

        self._saved_needle_height = position_mm
        if position_mm is None:
            self._saved_needle_label.setText("n/a")
            return
        self._saved_needle_label.setText(f"{position_mm:.4f} mm")

    def set_contact_seek_running(self, running: bool) -> None:
        """Update contact-seek button state."""

        self._contact_seek_button.setEnabled(not running)
        self._contact_seek_cancel_button.setEnabled(bool(running))

    def set_contact_seek_result(self, message: str) -> None:
        """Show the latest automatic contact-seek status."""

        self._contact_seek_status_label.setText(message or "n/a")

    def _format_xyz(self, position_xyz: tuple[float, float, float]) -> str:
        x_value, y_value, z_value = position_xyz
        return f"X={x_value:.4f} mm, Y={y_value:.4f} mm, Z={z_value:.4f} mm"

    def closeEvent(self, event) -> None:  # type: ignore[override]
        super().closeEvent(event)
        self.visibility_changed.emit(False)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self.visibility_changed.emit(True)
