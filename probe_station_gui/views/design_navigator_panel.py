"""Combined design window and controls for GDS-backed workflows."""

from __future__ import annotations

import math
import logging
from pathlib import Path
from time import perf_counter, monotonic

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..design_model import (
    DesignDocument,
    LayerKey,
    MeasurementTarget,
    Point2D,
    SnapResult,
)

try:  # pragma: no cover - optional runtime dependency
    import pyqtgraph as pg
except ImportError:  # pragma: no cover - optional runtime dependency
    pg = None


logger = logging.getLogger(__name__)


class _DesignPlotPane(QWidget):
    """Thin wrapper around pyqtgraph for design rendering."""

    calibration_point_selected = Signal(int, float, float)
    hover_snap_changed = Signal(object)
    HOVER_SNAP_LOG_INTERVAL_S = 0.2
    HOVER_SNAP_SLOW_MS = 8.0

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document: DesignDocument | None = None
        self._targets: list[MeasurementTarget] = []
        self._selected_target_id: str | None = None
        self._source_design_marks: list[Point2D | None] = [None, None]
        self._current_design_position: Point2D | None = None
        self._fov_design_size: Point2D | None = None
        self._check_design_marks: list[Point2D] = []
        self._layer_items: list[object] = []
        self._hover_snap: SnapResult | None = None
        self._pending_hover_scene_pos = None
        self._last_hover_log_at = 0.0
        self._last_hover_log_signature: tuple[float, float, str] | None = None
        self._plot = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        if pg is None:
            status_label = QLabel("pyqtgraph is not installed.", self)
            status_label.setAlignment(Qt.AlignCenter)
            status_label.setMinimumHeight(320)
            status_label.setStyleSheet(
                "QLabel { border: 1px dashed palette(mid); color: palette(mid); }"
            )
            layout.addWidget(status_label, 1)
            return

        self._plot = pg.PlotWidget(parent=self)
        self._plot.setBackground("k")
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.setAspectLocked(True)
        self._plot.showGrid(x=False, y=False, alpha=0.0)
        view_box = self._plot.getViewBox()
        view_box.setMouseEnabled(x=True, y=True)
        self._plot.plotItem.hideAxis("bottom")
        self._plot.plotItem.hideAxis("left")

        self._route_item = self._plot.plot([], [], pen=pg.mkPen("#4dd0e1", width=2))
        self._hover_segment_item = self._plot.plot(
            [],
            [],
            pen=pg.mkPen("#ffffff", width=2),
        )
        self._hover_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffffff", width=2),
            brush=pg.mkBrush(255, 255, 255, 60),
            size=14,
            symbol="+",
        )
        self._target_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#4dd0e1", width=1),
            brush=pg.mkBrush(77, 208, 225, 150),
            size=8,
        )
        self._selected_target_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ff7043", width=2),
            brush=pg.mkBrush(255, 112, 67, 180),
            size=12,
        )
        self._current_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#81c784", width=2),
            brush=pg.mkBrush(129, 199, 132, 220),
            size=12,
            symbol="x",
        )
        self._source_mark_1_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffd54f", width=2),
            brush=pg.mkBrush(255, 213, 79, 180),
            size=12,
            symbol="o",
        )
        self._source_mark_2_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ff7043", width=2),
            brush=pg.mkBrush(255, 112, 67, 180),
            size=12,
            symbol="t",
        )
        self._check_mark_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ab47bc", width=2),
            brush=pg.mkBrush(171, 71, 188, 180),
            size=9,
            symbol="x",
        )
        self._fov_item = self._plot.plot([], [], pen=pg.mkPen("#81c784", width=1))
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(16)
        self._hover_timer.timeout.connect(self._flush_hover_snap)
        self._plot.addItem(self._hover_item)
        self._plot.addItem(self._target_item)
        self._plot.addItem(self._selected_target_item)
        self._plot.addItem(self._current_item)
        self._plot.addItem(self._source_mark_1_item)
        self._plot.addItem(self._source_mark_2_item)
        self._plot.addItem(self._check_mark_item)
        self._plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)
        self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        layout.addWidget(self._plot, 1)

    def set_document(self, document: DesignDocument | None) -> None:
        self._document = document
        if document is None:
            self._set_hover_snap(None)
        self._redraw_document()
        self._redraw_overlays()

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._targets = list(targets)
        self._selected_target_id = selected_target_id
        self._redraw_overlays()

    def set_source_design_marks(self, points: list[Point2D | None]) -> None:
        self._source_design_marks = list(points[:2])
        while len(self._source_design_marks) < 2:
            self._source_design_marks.append(None)
        self._redraw_overlays()

    def set_current_design_position(
        self,
        point: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        self._current_design_position = point
        self._fov_design_size = fov_design_size
        self._redraw_overlays()

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> None:
        self._source_design_marks = list(source_design_marks[:2])
        while len(self._source_design_marks) < 2:
            self._source_design_marks.append(None)
        self._check_design_marks = list(check_design_marks)
        self._redraw_overlays()

    def focus_bounds(self) -> None:
        if self._plot is None or self._document is None:
            return
        left, bottom, right, top = self._document.bounds
        self._plot.setXRange(left, right, padding=0.02)
        self._plot.setYRange(bottom, top, padding=0.02)

    def _redraw_document(self) -> None:
        if self._plot is None:
            return
        for item in self._layer_items:
            self._plot.removeItem(item)
        self._layer_items.clear()
        if self._document is None:
            return
        for layer_key, polygons in self._document.visible_polygons().items():
            x_data: list[float] = []
            y_data: list[float] = []
            for polygon in polygons:
                if len(polygon) < 2:
                    continue
                closed = np.vstack((polygon, polygon[0]))
                x_data.extend(float(value) for value in closed[:, 0])
                x_data.append(float("nan"))
                y_data.extend(float(value) for value in closed[:, 1])
                y_data.append(float("nan"))
            if not x_data:
                continue
            line = self._plot.plot(
                x_data,
                y_data,
                pen=pg.mkPen(self._layer_color(layer_key), width=1),
            )
            self._layer_items.append(line)
        self.focus_bounds()

    def _redraw_overlays(self) -> None:
        if self._plot is None:
            return
        if self._targets:
            x_values = [target.design_center[0] for target in self._targets]
            y_values = [target.design_center[1] for target in self._targets]
            self._target_item.setData(x_values, y_values)
            self._route_item.setData(x_values, y_values)
        else:
            self._target_item.setData([], [])
            self._route_item.setData([], [])

        selected_target = next(
            (target for target in self._targets if target.id == self._selected_target_id),
            None,
        )
        if selected_target is None:
            self._selected_target_item.setData([], [])
        else:
            self._selected_target_item.setData(
                [selected_target.design_center[0]],
                [selected_target.design_center[1]],
            )

        if self._current_design_position is None:
            self._current_item.setData([], [])
            self._fov_item.setData([], [])
        else:
            self._current_item.setData(
                [self._current_design_position[0]],
                [self._current_design_position[1]],
            )
            if self._fov_design_size is None:
                self._fov_item.setData([], [])
            else:
                half_w = abs(float(self._fov_design_size[0])) * 0.5
                half_h = abs(float(self._fov_design_size[1])) * 0.5
                cx, cy = self._current_design_position
                xs = [cx - half_w, cx + half_w, cx + half_w, cx - half_w, cx - half_w]
                ys = [cy - half_h, cy - half_h, cy + half_h, cy + half_h, cy - half_h]
                self._fov_item.setData(xs, ys)

        self._set_slot_item_data(self._source_mark_1_item, self._source_design_marks[0])
        self._set_slot_item_data(self._source_mark_2_item, self._source_design_marks[1])
        if self._check_design_marks:
            self._check_mark_item.setData(
                [point[0] for point in self._check_design_marks],
                [point[1] for point in self._check_design_marks],
            )
        else:
            self._check_mark_item.setData([], [])
        self._redraw_hover()

    def _on_mouse_clicked(self, event) -> None:  # pragma: no cover - UI interaction
        if self._plot is None or self._document is None:
            return
        if event.button() == Qt.LeftButton:
            slot = 0
        elif event.button() == Qt.RightButton:
            slot = 1
        else:
            return
        position = event.scenePos()
        if not self._plot.sceneBoundingRect().contains(position):
            return
        view_point = self._plot.getViewBox().mapSceneToView(position)
        raw_point = (float(view_point.x()), float(view_point.y()))
        started = perf_counter()
        snap_result = self._document.snap_point_info(raw_point)
        elapsed_ms = (perf_counter() - started) * 1000.0
        self._set_hover_snap(snap_result)
        logger.debug(
            "DESIGN SNAP click raw=(%.3f, %.3f) snapped=(%.3f, %.3f) mode=%s dist=%.4f elapsed_ms=%.2f",
            raw_point[0],
            raw_point[1],
            snap_result.point[0],
            snap_result.point[1],
            snap_result.mode,
            snap_result.distance,
            elapsed_ms,
        )
        self.calibration_point_selected.emit(
            slot,
            snap_result.point[0],
            snap_result.point[1],
        )

    def _on_mouse_moved(self, position) -> None:  # pragma: no cover - UI interaction
        self._pending_hover_scene_pos = position
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    def _flush_hover_snap(self) -> None:  # pragma: no cover - UI interaction
        if self._plot is None or self._document is None:
            self._set_hover_snap(None)
            return
        position = self._pending_hover_scene_pos
        self._pending_hover_scene_pos = None
        if position is None or not self._plot.sceneBoundingRect().contains(position):
            self._set_hover_snap(None)
            return
        view_point = self._plot.getViewBox().mapSceneToView(position)
        raw_point = (float(view_point.x()), float(view_point.y()))
        started = perf_counter()
        snap_result = self._document.snap_point_info(raw_point)
        elapsed_ms = (perf_counter() - started) * 1000.0
        self._set_hover_snap(snap_result)
        self._log_hover_snap(raw_point, snap_result, elapsed_ms)

    def _set_hover_snap(self, snap_result: SnapResult | None) -> None:
        self._hover_snap = snap_result
        self._redraw_hover()
        self.hover_snap_changed.emit(snap_result)

    def _redraw_hover(self) -> None:
        if self._plot is None or self._hover_snap is None:
            if self._plot is not None:
                self._hover_item.setData([], [])
                self._hover_segment_item.setData([], [])
            return
        point = self._hover_snap.point
        self._hover_item.setData([point[0]], [point[1]])
        if (
            self._hover_snap.mode == "segment"
            and self._hover_snap.segment_start is not None
            and self._hover_snap.segment_end is not None
        ):
            self._hover_segment_item.setData(
                [self._hover_snap.segment_start[0], self._hover_snap.segment_end[0]],
                [self._hover_snap.segment_start[1], self._hover_snap.segment_end[1]],
            )
        else:
            self._hover_segment_item.setData([], [])

    def _log_hover_snap(
        self,
        raw_point: Point2D,
        snap_result: SnapResult,
        elapsed_ms: float,
    ) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        signature = (
            round(snap_result.point[0], 3),
            round(snap_result.point[1], 3),
            snap_result.mode,
        )
        now = monotonic()
        should_log = (
            elapsed_ms >= self.HOVER_SNAP_SLOW_MS
            or signature != self._last_hover_log_signature
            or (now - self._last_hover_log_at) >= self.HOVER_SNAP_LOG_INTERVAL_S
        )
        if not should_log:
            return
        logger.debug(
            "DESIGN SNAP hover raw=(%.3f, %.3f) snapped=(%.3f, %.3f) mode=%s dist=%.4f elapsed_ms=%.2f",
            raw_point[0],
            raw_point[1],
            snap_result.point[0],
            snap_result.point[1],
            snap_result.mode,
            snap_result.distance,
            elapsed_ms,
        )
        self._last_hover_log_at = now
        self._last_hover_log_signature = signature

    @staticmethod
    def _set_slot_item_data(item, point: Point2D | None) -> None:
        if point is None:
            item.setData([], [])
            return
        item.setData([point[0]], [point[1]])

    @staticmethod
    def _layer_color(layer_key: LayerKey) -> QColor:
        hue = int((layer_key[0] * 57 + layer_key[1] * 19) % 360)
        return QColor.fromHsv(hue, 180, 210, 180)


class DesignNavigatorPanel(QWidget):
    """Control panel for loading a design, registration, and target navigation."""

    load_design_requested = Signal(str)
    unload_design_requested = Signal()
    top_cell_changed = Signal(str)
    layer_visibility_changed = Signal(int, int, bool)
    load_script_requested = Signal(str)
    reload_script_requested = Signal()
    move_to_target_requested = Signal(str)
    next_target_requested = Signal()
    previous_target_requested = Signal()
    target_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document: DesignDocument | None = None
        self._source_design_marks: list[Point2D | None] = [None, None]
        self._source_stage_marks: list[Point2D | None] = [None, None]
        self._targets: list[MeasurementTarget] = []
        self._selected_target_id: str | None = None

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        self._availability_label = QLabel(self)
        self._availability_label.setWordWrap(True)
        root_layout.addWidget(self._availability_label)
        self._snap_hint_label = QLabel("Hover snap: move over a line or corner.", self)
        self._snap_hint_label.setWordWrap(True)
        self._snap_hint_label.setStyleSheet("QLabel { color: #b0bec5; }")
        root_layout.addWidget(self._snap_hint_label)

        file_group = QGroupBox("Design", self)
        file_layout = QGridLayout(file_group)
        self._load_design_button = QPushButton("Load GDS...", file_group)
        self._unload_design_button = QPushButton("Unload", file_group)
        self._top_cell_combo = QComboBox(file_group)
        self._top_cell_combo.currentTextChanged.connect(self._on_top_cell_changed)
        self._load_design_button.clicked.connect(self._choose_design_file)
        self._unload_design_button.clicked.connect(self.unload_design_requested.emit)
        self._document_label = QLabel("No design loaded.", file_group)
        self._document_label.setWordWrap(True)
        file_layout.addWidget(self._load_design_button, 0, 0)
        file_layout.addWidget(self._unload_design_button, 0, 1)
        file_layout.addWidget(QLabel("Top cell:", file_group), 1, 0)
        file_layout.addWidget(self._top_cell_combo, 1, 1)
        file_layout.addWidget(self._document_label, 2, 0, 1, 2)
        root_layout.addWidget(file_group)

        layer_group = QGroupBox("Layers", self)
        layer_layout = QVBoxLayout(layer_group)
        self._layer_list = QListWidget(layer_group)
        self._layer_list.itemChanged.connect(self._on_layer_item_changed)
        layer_layout.addWidget(self._layer_list)
        root_layout.addWidget(layer_group)

        registration_group = QGroupBox("Registration", self)
        registration_layout = QVBoxLayout(registration_group)
        self._registration_hint_label = QLabel(
            "Use left click for design point 1 and right click for design point 2 in the layout view.",
            registration_group,
        )
        self._registration_hint_label.setWordWrap(True)
        registration_layout.addWidget(self._registration_hint_label)
        self._calibration_prompt_label = QLabel(registration_group)
        self._calibration_prompt_label.setWordWrap(True)
        registration_layout.addWidget(self._calibration_prompt_label)
        self._mark_1_label = QLabel(registration_group)
        self._mark_1_label.setWordWrap(True)
        self._mark_1_label.setStyleSheet("QLabel { color: #ffd54f; }")
        registration_layout.addWidget(self._mark_1_label)
        self._mark_2_label = QLabel(registration_group)
        self._mark_2_label.setWordWrap(True)
        self._mark_2_label.setStyleSheet("QLabel { color: #ff7043; }")
        registration_layout.addWidget(self._mark_2_label)
        self._chip_1_label = QLabel(registration_group)
        self._chip_1_label.setWordWrap(True)
        self._chip_1_label.setStyleSheet("QLabel { color: #ffd54f; }")
        registration_layout.addWidget(self._chip_1_label)
        self._chip_2_label = QLabel(registration_group)
        self._chip_2_label.setWordWrap(True)
        self._chip_2_label.setStyleSheet("QLabel { color: #ff7043; }")
        registration_layout.addWidget(self._chip_2_label)
        self._registration_status_label = QLabel("No design registration.", registration_group)
        self._registration_status_label.setWordWrap(True)
        registration_layout.addWidget(self._registration_status_label)
        root_layout.addWidget(registration_group)

        script_group = QGroupBox("Measurement Plan", self)
        script_layout = QVBoxLayout(script_group)
        script_buttons = QHBoxLayout()
        self._load_script_button = QPushButton("Load Script...", script_group)
        self._reload_script_button = QPushButton("Reload Script", script_group)
        script_buttons.addWidget(self._load_script_button)
        script_buttons.addWidget(self._reload_script_button)
        script_layout.addLayout(script_buttons)
        self._script_label = QLabel("No script loaded.", script_group)
        self._script_label.setWordWrap(True)
        script_layout.addWidget(self._script_label)
        self._target_table = QTableWidget(0, 4, script_group)
        self._target_table.setHorizontalHeaderLabels(["ID", "Label", "Group", "Design center"])
        self._target_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._target_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._target_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._target_table.itemSelectionChanged.connect(self._on_target_selection_changed)
        script_layout.addWidget(self._target_table)
        nav_buttons = QHBoxLayout()
        self._previous_button = QPushButton("Previous", script_group)
        self._move_button = QPushButton("Move To", script_group)
        self._next_button = QPushButton("Next", script_group)
        nav_buttons.addWidget(self._previous_button)
        nav_buttons.addWidget(self._move_button)
        nav_buttons.addWidget(self._next_button)
        script_layout.addLayout(nav_buttons)
        self._load_script_button.clicked.connect(self._choose_script_file)
        self._reload_script_button.clicked.connect(self.reload_script_requested.emit)
        self._previous_button.clicked.connect(self.previous_target_requested.emit)
        self._next_button.clicked.connect(self.next_target_requested.emit)
        self._move_button.clicked.connect(self._emit_move_to_selected_target)
        root_layout.addWidget(script_group)

        self._current_position_label = QLabel("Stage: unavailable", self)
        self._current_position_label.setWordWrap(True)
        root_layout.addWidget(self._current_position_label)
        root_layout.addStretch(1)

        self._update_availability()
        self._update_enabled_state()

    def set_document(self, document: DesignDocument | None) -> None:
        if document is self._document:
            return
        self._document = document
        self._source_design_marks = [None, None]
        self._source_stage_marks = [None, None]
        if document is None:
            self._document_label.setText("No design loaded.")
            self._top_cell_combo.blockSignals(True)
            self._top_cell_combo.clear()
            self._top_cell_combo.blockSignals(False)
            self._layer_list.blockSignals(True)
            self._layer_list.clear()
            self._layer_list.blockSignals(False)
        else:
            self._document_label.setText(
                f"{document.path.name} | bounds {self._format_bounds(document.bounds)}"
            )
            self._top_cell_combo.blockSignals(True)
            self._top_cell_combo.clear()
            self._top_cell_combo.addItems(document.cell_names)
            self._top_cell_combo.setCurrentText(document.top_cell_name)
            self._top_cell_combo.blockSignals(False)
            self._layer_list.blockSignals(True)
            self._layer_list.clear()
            for layer_key in document.layer_keys():
                item = QListWidgetItem(f"Layer {layer_key[0]}/{layer_key[1]}")
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setData(Qt.UserRole, layer_key)
                item.setCheckState(
                    Qt.Checked if layer_key in document.visible_layers else Qt.Unchecked
                )
                self._layer_list.addItem(item)
            self._layer_list.blockSignals(False)
        self._update_mark_labels()
        self._update_enabled_state()

    def set_script_path(self, script_path: str | None) -> None:
        if not script_path:
            self._script_label.setText("No script loaded.")
            return
        self._script_label.setText(str(Path(script_path)))

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._targets = list(targets)
        self._selected_target_id = selected_target_id
        self._target_table.blockSignals(True)
        self._target_table.clearSelection()
        self._target_table.setRowCount(len(targets))
        for row, target in enumerate(targets):
            self._target_table.setItem(row, 0, QTableWidgetItem(target.id))
            self._target_table.setItem(row, 1, QTableWidgetItem(target.label))
            self._target_table.setItem(row, 2, QTableWidgetItem(target.group or ""))
            self._target_table.setItem(
                row,
                3,
                QTableWidgetItem(
                    f"X={target.design_center[0]:.3f}, Y={target.design_center[1]:.3f}"
                ),
            )
            if target.id == selected_target_id:
                self._target_table.selectRow(row)
        self._target_table.blockSignals(False)
        self._update_enabled_state()

    def set_registration_status(self, text: str) -> None:
        self._registration_status_label.setText(text or "No design registration.")

    def set_calibration_prompt(self, text: str) -> None:
        self._calibration_prompt_label.setText(text)

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> None:
        _ = check_design_marks
        self._source_design_marks = list(source_design_marks[:2])
        while len(self._source_design_marks) < 2:
            self._source_design_marks.append(None)
        self._update_mark_labels()
        self._update_enabled_state()

    def set_stage_registration_marks(self, source_stage_marks: list[Point2D | None]) -> None:
        self._source_stage_marks = list(source_stage_marks[:2])
        while len(self._source_stage_marks) < 2:
            self._source_stage_marks.append(None)
        self._update_mark_labels()
        self._update_enabled_state()

    def set_current_position(
        self,
        stage_xy: Point2D | None,
        design_xy: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        _ = fov_design_size
        if stage_xy is None:
            self._current_position_label.setText("Stage: unavailable")
        elif design_xy is None:
            self._current_position_label.setText(
                f"Stage X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f}"
            )
        else:
            self._current_position_label.setText(
                f"Stage X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f} | "
                f"Design X={design_xy[0]:.3f}, Y={design_xy[1]:.3f}"
            )

    def set_status_message(self, text: str) -> None:
        self._availability_label.setText(text)

    def set_hover_snap(self, snap_result: SnapResult | None) -> None:
        if snap_result is None:
            self._snap_hint_label.setText("Hover snap: move over a line or corner.")
            return
        label = "Corner" if snap_result.mode == "vertex" else "Line"
        self._snap_hint_label.setText(
            f"Hover snap: {label} at X={snap_result.point[0]:.3f}, "
            f"Y={snap_result.point[1]:.3f} | distance {snap_result.distance:.4f}"
        )

    def _update_mark_labels(self) -> None:
        self._mark_1_label.setText(self._format_mark_label("Mark 1 (LMB)", self._source_design_marks[0]))
        self._mark_2_label.setText(self._format_mark_label("Mark 2 (RMB)", self._source_design_marks[1]))
        self._chip_1_label.setText(self._format_mark_label("Chip 1", self._source_stage_marks[0]))
        self._chip_2_label.setText(self._format_mark_label("Chip 2", self._source_stage_marks[1]))

    def _update_availability(self) -> None:
        messages: list[str] = []
        if pg is None:
            messages.append("pyqtgraph not installed: design window is disabled.")
        else:
            messages.append(
                "Load a GDS for registered navigation. Use the Alignment dock when you want to capture chip points."
            )
        self._availability_label.setText(" ".join(messages))

    def _update_enabled_state(self) -> None:
        has_document = self._document is not None
        self._unload_design_button.setEnabled(has_document)
        self._top_cell_combo.setEnabled(has_document)
        self._layer_list.setEnabled(has_document)
        self._load_script_button.setEnabled(has_document)
        self._reload_script_button.setEnabled(
            has_document and self._script_label.text() != "No script loaded."
        )
        has_targets = bool(self._targets)
        has_selection = self._selected_target_id is not None
        self._previous_button.setEnabled(has_targets)
        self._next_button.setEnabled(has_targets)
        self._move_button.setEnabled(has_targets and has_selection)

    def _choose_design_file(self) -> None:  # pragma: no cover - UI interaction
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Design",
            "",
            "Layout files (*.gds *.gdsii *.oas *.oasis);;All files (*)",
        )
        if path:
            self.load_design_requested.emit(path)

    def _choose_script_file(self) -> None:  # pragma: no cover - UI interaction
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Measurement Plan",
            "",
            "Python files (*.py);;All files (*)",
        )
        if path:
            self.load_script_requested.emit(path)

    def _on_top_cell_changed(self, cell_name: str) -> None:
        if self._document is None or not cell_name:
            return
        if cell_name == self._document.top_cell_name:
            return
        self.top_cell_changed.emit(cell_name)

    def _on_layer_item_changed(self, item: QListWidgetItem) -> None:
        layer_key = item.data(Qt.UserRole)
        if not isinstance(layer_key, tuple) or len(layer_key) != 2:
            return
        self.layer_visibility_changed.emit(
            int(layer_key[0]),
            int(layer_key[1]),
            item.checkState() == Qt.Checked,
        )

    def _on_target_selection_changed(self) -> None:
        selected_rows = self._target_table.selectionModel().selectedRows()
        if not selected_rows:
            self._selected_target_id = None
            self._update_enabled_state()
            return
        row = selected_rows[0].row()
        if row < 0 or row >= len(self._targets):
            return
        target = self._targets[row]
        self._selected_target_id = target.id
        self.target_selected.emit(target.id)
        self._update_enabled_state()

    def _emit_move_to_selected_target(self) -> None:
        if self._selected_target_id is None:
            return
        self.move_to_target_requested.emit(self._selected_target_id)

    @staticmethod
    def _format_bounds(bounds: tuple[float, float, float, float]) -> str:
        left, bottom, right, top = bounds
        width = right - left
        height = top - bottom
        diagonal = math.hypot(width, height)
        return f"{width:.3f} x {height:.3f} (diag {diagonal:.3f})"

    @staticmethod
    def _format_mark_label(label: str, point: Point2D | None) -> str:
        if point is None:
            return f"{label}: not set"
        return f"{label}: X={point[0]:.3f}, Y={point[1]:.3f}"


class DesignLayoutWindow(QWidget):
    """Top-level design window combining the layout view and design controls."""

    calibration_point_selected = Signal(int, float, float)
    hover_snap_changed = Signal(object)
    visibility_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        _ = parent
        super().__init__(None)
        self.setWindowTitle("Design Window")
        self.setWindowFlag(Qt.Window, True)
        self.resize(1480, 920)

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        self._main_view = _DesignPlotPane(parent=self)
        self.navigator_panel = DesignNavigatorPanel(self)
        self.navigator_panel.setMinimumWidth(420)
        self._main_view.calibration_point_selected.connect(
            self.calibration_point_selected.emit
        )
        self._main_view.hover_snap_changed.connect(self.hover_snap_changed.emit)
        root_layout.addWidget(self._main_view, 1)
        root_layout.addWidget(self.navigator_panel, 0)

    def set_document(self, document: DesignDocument | None) -> None:
        self._main_view.set_document(document)
        self.navigator_panel.set_document(document)

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._main_view.set_targets(targets, selected_target_id=selected_target_id)
        self.navigator_panel.set_targets(targets, selected_target_id=selected_target_id)

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> None:
        self._main_view.set_registration_marks(source_design_marks, check_design_marks)
        self.navigator_panel.set_registration_marks(source_design_marks, check_design_marks)

    def set_stage_registration_marks(self, source_stage_marks: list[Point2D | None]) -> None:
        self.navigator_panel.set_stage_registration_marks(source_stage_marks)

    def set_script_path(self, script_path: str | None) -> None:
        self.navigator_panel.set_script_path(script_path)

    def set_calibration_prompt(self, text: str) -> None:
        self.navigator_panel.set_calibration_prompt(text)

    def set_registration_status(self, text: str) -> None:
        self.navigator_panel.set_registration_status(text)

    def set_status_message(self, text: str) -> None:
        self.navigator_panel.set_status_message(text)

    def set_hover_snap(self, snap_result: SnapResult | None) -> None:
        self.navigator_panel.set_hover_snap(snap_result)

    def set_current_position(
        self,
        stage_xy: Point2D | None,
        design_xy: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        self.navigator_panel.set_current_position(
            stage_xy,
            design_xy,
            fov_design_size=fov_design_size,
        )

    def set_current_design_position(
        self,
        point: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        self._main_view.set_current_design_position(point, fov_design_size=fov_design_size)

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self.visibility_changed.emit(False)


__all__ = ["DesignLayoutWindow", "DesignNavigatorPanel"]
