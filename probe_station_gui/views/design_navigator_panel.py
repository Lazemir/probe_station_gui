"""Combined design window and controls for GDS-backed workflows."""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QPointF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.route.run_ui import RouteRunControlState
from probe_station_gui.design.navigation_geometry import (
    format_bounds,
    format_mark_label,
    format_point,
    route_pick_label_text,
    vector_from_length_angle,
    vector_length_angle,
)
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox as QDoubleSpinBox,
    GuardedSpinBox as QSpinBox,
)

from probe_station_gui.design.model import (
    DesignDocument,
    MeasurementTarget,
    Point2D,
    SnapResult,
)
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.selection_geometry import constrain_vector_endpoint
from probe_station_gui.design.selection_model import (
    EntityOwner,
    MixedArrayRequest,
    SelectableDesignEntity,
    SelectionModel,
    plan_mixed_array,
    project_entities,
    route_entity_id,
)
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.views.design_navigator_enablement import (
    DesignNavigatorEnablement,
)
from probe_station_gui.views.design_plot_pane import _DesignPlotPane, pg


class DesignNavigatorPanel(QWidget):
    """Control panel for loading a design, registration, and target navigation."""

    load_design_requested = Signal(str)
    unload_design_requested = Signal()
    top_cell_changed = Signal(str)
    layer_visibility_changed = Signal(int, int, bool)
    design_rotate_requested = Signal(int)
    move_to_target_requested = Signal(str)
    next_target_requested = Signal()
    previous_target_requested = Signal()
    target_selected = Signal(str)
    snap_enabled_changed = Signal(bool)
    route_new_requested = Signal()
    route_open_requested = Signal(str)
    route_save_requested = Signal()
    route_save_as_requested = Signal(str)
    route_add_current_requested = Signal()
    route_remove_selected_requested = Signal()
    route_clear_requested = Signal()
    route_selected = Signal(int)
    route_measurement_run_requested = Signal()
    route_measurement_measure_requested = Signal(int)
    route_measurement_stop_requested = Signal()
    route_measurement_interrupt_requested = Signal()
    route_measurement_pause_requested = Signal()
    route_measurement_save_shift_requested = Signal(int)
    route_measurement_confirmation_requested = Signal(str)
    route_measurement_jump_requested = Signal(int)
    route_measurement_move_requested = Signal(int)
    route_offsets_changed = Signal(float, float, float, float)
    route_edit_enabled_changed = Signal(bool)
    route_pick_mode_changed = Signal(object)
    route_preview_changed = Signal(object)
    tool_measure_preview_changed = Signal(object)
    tool_measurements_changed = Signal(object)
    active_design_tool_changed = Signal(str)
    alignment_draft_changed = Signal(object)
    alignment_draft_accepted = Signal(object)
    alignment_draft_discarded = Signal()
    selection_requested = Signal(object, str)
    delete_selection_requested = Signal()
    guide_undo_requested = Signal()
    guide_clear_requested = Signal()
    markup_visibility_changed = Signal(bool)
    mixed_array_requested = Signal(object)
    mixed_array_preview_changed = Signal(object, object)
    route_array_requested = Signal(
        float,
        float,
        float,
        float,
        int,
        float,
        float,
        int,
        bool,
        bool,
        object,
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document: DesignDocument | None = None
        self._design_dialog_directory = ""
        self._snap_enabled = True
        self._source_design_marks: list[Point2D | None] = [None, None]
        self._source_stage_marks: list[Point2D | None] = [None, None]
        self._targets: list[MeasurementTarget] = []
        self._selected_target_id: str | None = None
        self._route: MeasurementRoute | None = None
        self._selected_route_point_index = -1
        self._route_run_control_state = RouteRunControlState()
        self._design_registration_active = False
        self._current_design_position: Point2D | None = None
        self._active_design_tool = "select"
        self._selection = SelectionModel()
        self._selectable_entities: tuple[SelectableDesignEntity, ...] = ()
        self._markup_visible = True
        self._markup_guide_count = 0
        self._guide_undo_available = False
        self._design_load_pending = False
        self._route_pick_mode: str | None = None
        self._route_pick_anchor_mode: str | None = None
        self._route_pick_anchor_point: Point2D | None = None
        self._ruler_anchor: Point2D | None = None
        self._ruler_end: Point2D | None = None
        self._ruler_segments: list[tuple[Point2D, Point2D]] = []
        self._alignment_draft_points: list[Point2D] = []
        self._updating_route_controls = False

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        self._availability_label = QLabel(self)
        self._availability_label.setWordWrap(True)
        root_layout.addWidget(self._availability_label)
        self._snap_checkbox = QCheckBox("Snap To Geometry", self)
        self._snap_checkbox.setChecked(True)
        self._snap_checkbox.toggled.connect(self._on_snap_checkbox_toggled)
        root_layout.addWidget(self._snap_checkbox)
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
        self._mark_1_label.setStyleSheet("QLabel { font-weight: 600; }")
        registration_layout.addWidget(self._mark_1_label)
        self._mark_2_label = QLabel(registration_group)
        self._mark_2_label.setWordWrap(True)
        self._mark_2_label.setStyleSheet("QLabel { font-weight: 600; }")
        registration_layout.addWidget(self._mark_2_label)
        self._chip_1_label = QLabel(registration_group)
        self._chip_1_label.setWordWrap(True)
        self._chip_1_label.setStyleSheet("QLabel { font-weight: 600; }")
        registration_layout.addWidget(self._chip_1_label)
        self._chip_2_label = QLabel(registration_group)
        self._chip_2_label.setWordWrap(True)
        self._chip_2_label.setStyleSheet("QLabel { font-weight: 600; }")
        registration_layout.addWidget(self._chip_2_label)
        self._registration_status_label = QLabel("No design registration.", registration_group)
        self._registration_status_label.setWordWrap(True)
        registration_layout.addWidget(self._registration_status_label)
        root_layout.addWidget(registration_group)

        route_group = QGroupBox("Probe Route", self)
        route_layout = QVBoxLayout(route_group)
        self._route_layout = route_layout
        route_buttons = QHBoxLayout()
        self._route_new_button = QPushButton("New", route_group)
        self._route_open_button = QPushButton("Open...", route_group)
        self._route_save_button = QPushButton("Save", route_group)
        self._route_save_as_button = QPushButton("Save As...", route_group)
        route_buttons.addWidget(self._route_new_button)
        route_buttons.addWidget(self._route_open_button)
        route_buttons.addWidget(self._route_save_button)
        route_buttons.addWidget(self._route_save_as_button)
        route_layout.addLayout(route_buttons)
        self._route_label = QLabel("No route loaded.", route_group)
        self._route_label.setWordWrap(True)
        route_layout.addWidget(self._route_label)

        self._tool_toolbar_widget = QWidget(route_group)
        tool_buttons = QHBoxLayout(self._tool_toolbar_widget)
        tool_buttons.setContentsMargins(0, 0, 0, 0)
        tool_buttons.setSpacing(4)
        self._tool_button_group = QButtonGroup(self._tool_toolbar_widget)
        self._tool_button_group.setExclusive(True)
        self._select_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Select",
            self._make_tool_icon("select"),
        )
        self._point_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Point",
            self._make_tool_icon("point"),
        )
        self._align_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Align",
            self._make_tool_icon("align"),
        )
        self._guide_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Guide",
            self._make_tool_icon("guide"),
        )
        self._ruler_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Ruler",
            self._make_tool_icon("ruler"),
        )
        self._array_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Array",
            self._make_tool_icon("array"),
        )
        self._rotate_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Rotate",
            self._make_tool_icon("rotate"),
        )
        self._rotate_tool_button.setCheckable(False)
        self._tool_button_group.addButton(self._select_tool_button)
        self._tool_button_group.addButton(self._point_tool_button)
        self._tool_button_group.addButton(self._align_tool_button)
        self._tool_button_group.addButton(self._guide_tool_button)
        self._tool_button_group.addButton(self._ruler_tool_button)
        self._tool_button_group.addButton(self._array_tool_button)
        self._markup_visibility_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Markup",
            self._make_tool_icon("eye"),
        )
        self._markup_visibility_button.setToolTip("Show Markup")
        self._markup_visibility_button.setChecked(True)
        self._select_tool_button.setChecked(True)
        tool_buttons.addWidget(self._select_tool_button)
        tool_buttons.addWidget(self._point_tool_button)
        tool_buttons.addWidget(self._align_tool_button)
        tool_buttons.addWidget(self._guide_tool_button)
        tool_buttons.addWidget(self._ruler_tool_button)
        tool_buttons.addWidget(self._array_tool_button)
        tool_buttons.addWidget(self._rotate_tool_button)
        tool_buttons.addWidget(self._markup_visibility_button)
        tool_buttons.addStretch(1)
        route_layout.addWidget(self._tool_toolbar_widget)

        self._tool_group = QGroupBox("Tool Options", route_group)
        tool_layout = QVBoxLayout(self._tool_group)
        self._tool_status_label = QLabel("", self._tool_group)
        self._tool_status_label.setWordWrap(True)
        self._tool_status_label.setStyleSheet("QLabel { color: #607d8b; }")
        tool_layout.addWidget(self._tool_status_label)
        self._tool_stack = QStackedWidget(self._tool_group)

        select_page = QWidget(self._tool_group)
        select_layout = QVBoxLayout(select_page)
        select_layout.setContentsMargins(0, 0, 0, 0)
        self._selection_count_label = QLabel("Nothing selected.", select_page)
        self._selection_delete_button = QPushButton("Delete", select_page)
        select_layout.addWidget(self._selection_count_label)
        select_layout.addWidget(self._selection_delete_button)
        self._tool_stack.addWidget(select_page)

        point_page = QWidget(self._tool_group)
        point_layout = QVBoxLayout(point_page)
        point_layout.setContentsMargins(0, 0, 0, 0)
        point_layout.addWidget(QLabel("Click to place route points.", point_page))
        self._tool_stack.addWidget(point_page)

        guide_page = QWidget(self._tool_group)
        guide_layout = QHBoxLayout(guide_page)
        guide_layout.setContentsMargins(0, 0, 0, 0)
        self._guide_undo_button = QPushButton("Undo Last", guide_page)
        self._guide_clear_button = QPushButton("Clear All", guide_page)
        guide_layout.addWidget(self._guide_undo_button)
        guide_layout.addWidget(self._guide_clear_button)
        self._tool_stack.addWidget(guide_page)

        ruler_page = QWidget(self._tool_group)
        ruler_layout = QGridLayout(ruler_page)
        ruler_layout.setContentsMargins(0, 0, 0, 0)
        self._ruler_start_label = QLabel("Start: not set", ruler_page)
        self._ruler_end_label = QLabel("End: not set", ruler_page)
        self._ruler_delta_label = QLabel("dX=0.000, dY=0.000", ruler_page)
        self._ruler_length_label = QLabel("Length=0.000, Angle=0.000 deg", ruler_page)
        self._ruler_clear_button = QPushButton("Clear", ruler_page)
        self._ruler_cancel_button = QPushButton("Cancel", ruler_page)
        ruler_layout.addWidget(self._ruler_start_label, 0, 0, 1, 2)
        ruler_layout.addWidget(self._ruler_end_label, 1, 0, 1, 2)
        ruler_layout.addWidget(self._ruler_delta_label, 2, 0, 1, 2)
        ruler_layout.addWidget(self._ruler_length_label, 3, 0, 1, 2)
        ruler_layout.addWidget(self._ruler_clear_button, 4, 0)
        ruler_layout.addWidget(self._ruler_cancel_button, 4, 1)
        self._tool_stack.addWidget(ruler_page)

        array_page = QWidget(self._tool_group)
        array_layout = QGridLayout(array_page)
        array_layout.setContentsMargins(0, 0, 0, 0)
        self._route_array_dir1_step_x_spin = self._make_route_distance_spinbox(array_page)
        self._route_array_dir1_step_y_spin = self._make_route_angle_spinbox(array_page)
        self._route_array_dir2_step_x_spin = self._make_route_distance_spinbox(array_page)
        self._route_array_dir2_step_y_spin = self._make_route_angle_spinbox(array_page)
        self._route_array_dir1_step_x_spin.setValue(100.0)
        self._route_array_dir2_step_x_spin.setValue(100.0)
        self._route_array_dir2_step_y_spin.setValue(90.0)
        self._route_array_dir1_count_spin = QSpinBox(array_page)
        self._route_array_dir1_count_spin.setRange(1, 10000)
        self._route_array_dir1_count_spin.setValue(8)
        self._route_array_dir2_count_spin = QSpinBox(array_page)
        self._route_array_dir2_count_spin.setRange(1, 10000)
        self._route_array_dir2_count_spin.setValue(1)
        self._route_array_serpentine_checkbox = QCheckBox("Serpentine", array_page)
        self._route_array_pick_dir1_button = self._make_icon_button(
            array_page, "Pick Dir 1", "direction"
        )
        self._route_array_pick_dir2_button = self._make_icon_button(
            array_page, "Pick Dir 2", "direction"
        )
        self._route_array_create_button = self._make_icon_button(
            array_page, "Create", "accept"
        )
        self._route_array_cancel_button = self._make_icon_button(
            array_page, "Cancel", "cancel"
        )
        array_layout.addWidget(QLabel("Dir 1 length", array_page), 0, 0)
        array_layout.addWidget(self._route_array_dir1_step_x_spin, 0, 1)
        array_layout.addWidget(QLabel("angle", array_page), 0, 2)
        array_layout.addWidget(self._route_array_dir1_step_y_spin, 0, 3)
        array_layout.addWidget(QLabel("Count 1", array_page), 1, 0)
        array_layout.addWidget(self._route_array_dir1_count_spin, 1, 1)
        array_layout.addWidget(self._route_array_pick_dir1_button, 1, 2, 1, 2)
        array_layout.addWidget(QLabel("Dir 2 length", array_page), 2, 0)
        array_layout.addWidget(self._route_array_dir2_step_x_spin, 2, 1)
        array_layout.addWidget(QLabel("angle", array_page), 2, 2)
        array_layout.addWidget(self._route_array_dir2_step_y_spin, 2, 3)
        array_layout.addWidget(QLabel("Count 2", array_page), 3, 0)
        array_layout.addWidget(self._route_array_dir2_count_spin, 3, 1)
        array_layout.addWidget(self._route_array_pick_dir2_button, 3, 2, 1, 2)
        array_layout.addWidget(self._route_array_serpentine_checkbox, 4, 0, 1, 2)
        array_layout.addWidget(self._route_array_create_button, 4, 2)
        array_layout.addWidget(self._route_array_cancel_button, 4, 3)
        self._tool_stack.addWidget(array_page)

        align_page = QWidget(self._tool_group)
        align_layout = QVBoxLayout(align_page)
        align_layout.setContentsMargins(0, 0, 0, 0)
        self._alignment_points_label = QLabel("No design points.", align_page)
        self._alignment_points_label.setWordWrap(True)
        align_layout.addWidget(self._alignment_points_label)
        align_buttons = QHBoxLayout()
        self._alignment_undo_button = QPushButton("Undo", align_page)
        self._alignment_clear_button = QPushButton("Clear", align_page)
        self._alignment_done_button = QPushButton("Done", align_page)
        align_buttons.addWidget(self._alignment_undo_button)
        align_buttons.addWidget(self._alignment_clear_button)
        align_buttons.addWidget(self._alignment_done_button)
        align_layout.addLayout(align_buttons)
        self._tool_stack.addWidget(align_page)

        tool_layout.addWidget(self._tool_stack)
        route_layout.addWidget(self._tool_group)

        offset_layout = QGridLayout()
        offset_layout.addWidget(QLabel("Needle", route_group), 0, 0)
        offset_layout.addWidget(QLabel("dx", route_group), 0, 1)
        offset_layout.addWidget(QLabel("dy", route_group), 0, 2)
        offset_layout.addWidget(QLabel("1", route_group), 1, 0)
        offset_layout.addWidget(QLabel("2", route_group), 2, 0)
        self._needle_1_dx_spin = self._make_route_offset_spinbox(route_group)
        self._needle_1_dy_spin = self._make_route_offset_spinbox(route_group)
        self._needle_2_dx_spin = self._make_route_offset_spinbox(route_group)
        self._needle_2_dy_spin = self._make_route_offset_spinbox(route_group)
        offset_layout.addWidget(self._needle_1_dx_spin, 1, 1)
        offset_layout.addWidget(self._needle_1_dy_spin, 1, 2)
        offset_layout.addWidget(self._needle_2_dx_spin, 2, 1)
        offset_layout.addWidget(self._needle_2_dy_spin, 2, 2)
        route_layout.addLayout(offset_layout)

        self._route_table = QTableWidget(0, 5, route_group)
        self._route_table.setHorizontalHeaderLabels(
            ["#", "Label", "Center", "N1", "N2"]
        )
        self._route_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._route_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._route_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._route_table.itemSelectionChanged.connect(
            self._on_route_selection_changed
        )
        route_layout.addWidget(self._route_table)

        route_edit_buttons = QHBoxLayout()
        self._route_add_current_button = QPushButton("Add Current", route_group)
        self._route_remove_button = QPushButton("Remove", route_group)
        self._route_clear_button = QPushButton("Clear", route_group)
        route_edit_buttons.addWidget(self._route_add_current_button)
        route_edit_buttons.addWidget(self._route_remove_button)
        route_edit_buttons.addWidget(self._route_clear_button)
        route_layout.addLayout(route_edit_buttons)

        route_run_buttons = QHBoxLayout()
        self._route_run_button = QPushButton("Measure", route_group)
        self._route_pause_button = QPushButton("Pause", route_group)
        self._route_pause_button.setEnabled(False)
        self._route_interrupt_button = QPushButton("Interrupt", route_group)
        self._route_interrupt_button.setEnabled(False)
        self._route_interrupt_button.hide()
        self._route_stop_button = QPushButton("Stop", route_group)
        self._route_stop_button.setEnabled(False)
        route_run_buttons.addWidget(self._route_run_button)
        route_run_buttons.addWidget(self._route_pause_button)
        route_run_buttons.addWidget(self._route_interrupt_button)
        route_run_buttons.addWidget(self._route_stop_button)
        route_layout.addLayout(route_run_buttons)
        route_confirm_buttons = QHBoxLayout()
        self._route_save_shift_button = QPushButton("Save Shift", route_group)
        self._route_save_shift_button.setEnabled(False)
        self._route_remeasure_button = QPushButton("Remeasure", route_group)
        self._route_remeasure_button.setEnabled(False)
        self._route_skip_button = QPushButton("Skip", route_group)
        self._route_skip_button.setEnabled(False)
        self._route_next_button = QPushButton("Next", route_group)
        self._route_next_button.setEnabled(False)
        self._route_move_selected_button = QPushButton("Move", route_group)
        self._route_move_selected_button.setEnabled(False)
        self._route_jump_selected_button = QPushButton("Jump Selected", route_group)
        self._route_jump_selected_button.setEnabled(False)
        self._route_remeasure_button.hide()
        self._route_next_button.hide()
        self._route_jump_selected_button.hide()
        route_confirm_buttons.addWidget(self._route_save_shift_button)
        route_confirm_buttons.addWidget(self._route_skip_button)
        route_confirm_buttons.addWidget(self._route_move_selected_button)
        route_layout.addLayout(route_confirm_buttons)
        self._route_run_status_label = QLabel("Route measurement idle.", route_group)
        self._route_run_status_label.setWordWrap(True)
        route_layout.addWidget(self._route_run_status_label)

        self._route_new_button.clicked.connect(self.route_new_requested.emit)
        self._route_open_button.clicked.connect(self._choose_route_file)
        self._route_save_button.clicked.connect(self.route_save_requested.emit)
        self._route_save_as_button.clicked.connect(self._choose_route_save_file)
        self._route_add_current_button.clicked.connect(
            self.route_add_current_requested.emit
        )
        self._route_remove_button.clicked.connect(
            self.route_remove_selected_requested.emit
        )
        self._route_clear_button.clicked.connect(self.route_clear_requested.emit)
        self._route_run_button.clicked.connect(
            self._emit_route_measurement_measure_selected
        )
        self._route_pause_button.clicked.connect(
            self._emit_route_measurement_pause_or_resume
        )
        self._route_stop_button.clicked.connect(
            self.route_measurement_stop_requested.emit
        )
        self._route_interrupt_button.clicked.connect(
            self._emit_route_measurement_interrupt_or_resume
        )
        self._route_save_shift_button.clicked.connect(
            self._emit_route_measurement_save_shift_selected
        )
        self._route_remeasure_button.clicked.connect(
            lambda _checked=False: self.route_measurement_confirmation_requested.emit(
                "remeasure"
            )
        )
        self._route_skip_button.clicked.connect(
            lambda _checked=False: self.route_measurement_confirmation_requested.emit(
                "skip"
            )
        )
        self._route_next_button.clicked.connect(
            lambda _checked=False: self.route_measurement_confirmation_requested.emit(
                "next"
            )
        )
        self._route_jump_selected_button.clicked.connect(
            self._emit_route_measurement_jump_to_selected
        )
        self._route_move_selected_button.clicked.connect(
            self._emit_route_measurement_move_to_selected
        )
        self._select_tool_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("select")
        )
        self._point_tool_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("point")
        )
        self._align_tool_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("align")
        )
        self._guide_tool_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("guide")
        )
        self._ruler_tool_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("ruler")
        )
        self._array_tool_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("array")
        )
        self._rotate_tool_button.clicked.connect(
            lambda _checked=False: self.design_rotate_requested.emit(1)
        )
        self._markup_visibility_button.toggled.connect(
            self._on_markup_visibility_toggled
        )
        self._selection_delete_button.clicked.connect(
            self.delete_selection_requested.emit
        )
        self._guide_undo_button.clicked.connect(self.guide_undo_requested.emit)
        self._guide_clear_button.clicked.connect(self.guide_clear_requested.emit)
        self._ruler_clear_button.clicked.connect(self._clear_ruler)
        self._ruler_cancel_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("select")
        )
        self._alignment_undo_button.clicked.connect(self._undo_alignment_point)
        self._alignment_clear_button.clicked.connect(self._clear_alignment_draft)
        self._alignment_done_button.clicked.connect(self.accept_alignment_draft)
        self._route_array_pick_dir1_button.clicked.connect(
            lambda _checked=False: self._start_route_pick_mode("array_dir1")
        )
        self._route_array_pick_dir2_button.clicked.connect(
            lambda _checked=False: self._start_route_pick_mode("array_dir2")
        )
        self._route_array_create_button.clicked.connect(self._emit_route_array_requested)
        self._route_array_cancel_button.clicked.connect(self._cancel_route_array)
        for widget in (
            self._route_array_dir1_step_x_spin,
            self._route_array_dir1_step_y_spin,
            self._route_array_dir1_count_spin,
            self._route_array_dir2_step_x_spin,
            self._route_array_dir2_step_y_spin,
            self._route_array_dir2_count_spin,
            self._route_array_serpentine_checkbox,
        ):
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self._update_route_array_preview)
            else:
                widget.toggled.connect(self._update_route_array_preview)
        self._delete_shortcut = QShortcut(QKeySequence.Delete, self)
        self._delete_shortcut.setContext(Qt.WindowShortcut)
        self._delete_shortcut.activated.connect(
            self.delete_selection_requested.emit
        )
        for spinbox in (
            self._needle_1_dx_spin,
            self._needle_1_dy_spin,
            self._needle_2_dx_spin,
            self._needle_2_dy_spin,
        ):
            spinbox.valueChanged.connect(self._emit_route_offsets_changed)
        root_layout.addWidget(route_group)

        self._current_position_label = QLabel("Stage: unavailable", self)
        self._current_position_label.setWordWrap(True)
        root_layout.addWidget(self._current_position_label)
        root_layout.addStretch(1)

        self._update_availability()
        self._update_enabled_state()
        self._refresh_alignment_draft_ui()
        self._set_design_tool("select")

    def set_document(self, document: DesignDocument | None) -> None:
        if document is self._document:
            return
        self._document = document
        self._source_design_marks = [None, None]
        self._source_stage_marks = [None, None]
        self._clear_alignment_draft()
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

    def set_design_load_pending(self, pending: bool) -> None:
        self._design_load_pending = bool(pending)
        self._delete_shortcut.setEnabled(not self._design_load_pending)
        self._update_enabled_state()

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._targets = list(targets)
        self._selected_target_id = selected_target_id
        self._update_enabled_state()

    def set_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> None:
        previous_route = self._route
        previous_point_count = len(previous_route.points) if previous_route is not None else 0
        previous_selected_rows = self._selected_route_row_indices()
        self._route = route
        self._selected_route_point_index = selected_route_point_index
        self._updating_route_controls = True
        try:
            self._route_table.blockSignals(True)
            self._route_table.clearSelection()
            if route is None:
                self._route_label.setText("No route loaded.")
                self._route_table.setRowCount(0)
                self._set_route_offset_values(0.0, 0.0, 0.0, 0.0)
            else:
                rows_to_select: list[int]
                if (
                    route is previous_route
                    and previous_point_count == len(route.points)
                    and previous_selected_rows
                ):
                    rows_to_select = previous_selected_rows
                else:
                    rows_to_select = [selected_route_point_index]
                path_text = str(route.path) if route.path is not None else "Unsaved route."
                self._route_label.setText(f"{route.name} | {path_text}")
                self._route_table.setRowCount(len(route.points))
                for row, point in enumerate(route.points):
                    hits = route.needle_hits_for_point(point)
                    needle_1 = hits[0][1] if len(hits) > 0 else point.camera_center
                    needle_2 = hits[1][1] if len(hits) > 1 else point.camera_center
                    self._route_table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
                    self._route_table.setItem(row, 1, QTableWidgetItem(point.label))
                    self._route_table.setItem(
                        row,
                        2,
                        QTableWidgetItem(self._format_point(point.camera_center)),
                    )
                    self._route_table.setItem(
                        row,
                        3,
                        QTableWidgetItem(self._format_point(needle_1)),
                    )
                    self._route_table.setItem(
                        row,
                        4,
                        QTableWidgetItem(self._format_point(needle_2)),
                    )
                selection_model = self._route_table.selectionModel()
                if selection_model is not None:
                    for row in sorted({row for row in rows_to_select if 0 <= row < len(route.points)}):
                        selection_model.select(
                            self._route_table.model().index(row, 0),
                            QItemSelectionModel.Select | QItemSelectionModel.Rows,
                        )
                offsets = route.needle_offsets[:2]
                if len(offsets) >= 2:
                    self._set_route_offset_values(
                        offsets[0].dx,
                        offsets[0].dy,
                        offsets[1].dx,
                        offsets[1].dy,
                    )
                else:
                    self._set_route_offset_values(0.0, 0.0, 0.0, 0.0)
            self._route_table.blockSignals(False)
        finally:
            self._updating_route_controls = False
        self._update_enabled_state()
        self._update_route_array_preview()

    def set_selectable_entities(self, entities: object) -> None:
        if isinstance(entities, (list, tuple)) and all(
            isinstance(entity, SelectableDesignEntity) for entity in entities
        ):
            self._selectable_entities = tuple(entities)
        else:
            self._selectable_entities = ()
        self.set_selection(
            self._selection.prune(entity.id for entity in self._selectable_entities)
        )

    def set_selection(self, selection: SelectionModel) -> None:
        valid_ids = {entity.id for entity in self._selectable_entities}
        self._selection = selection.prune(valid_ids)
        selected_count = len(self._selection.ids)
        self._selection_count_label.setText(
            "Nothing selected."
            if selected_count == 0
            else f"Selected: {selected_count}"
        )
        route_rows = sorted(
            entity.route_index
            for entity in self._selectable_entities
            if entity.id in self._selection.ids
            and entity.owner is EntityOwner.ROUTE
            and entity.route_index is not None
        )
        self._updating_route_controls = True
        try:
            self._route_table.blockSignals(True)
            self._route_table.clearSelection()
            selection_model = self._route_table.selectionModel()
            if selection_model is not None:
                for row in route_rows:
                    if 0 <= row < self._route_table.rowCount():
                        selection_model.select(
                            self._route_table.model().index(row, 0),
                            QItemSelectionModel.Select | QItemSelectionModel.Rows,
                        )
        finally:
            self._route_table.blockSignals(False)
            self._updating_route_controls = False
        self._selected_route_point_index = route_rows[0] if route_rows else -1
        self._update_enabled_state()
        self._update_route_array_preview()

    def set_markup_visible(self, visible: bool) -> None:
        self._markup_visible = bool(visible)
        self._markup_visibility_button.blockSignals(True)
        self._markup_visibility_button.setChecked(self._markup_visible)
        self._markup_visibility_button.blockSignals(False)
        if not self._markup_visible:
            self._selectable_entities = tuple(
                entity
                for entity in self._selectable_entities
                if entity.owner is not EntityOwner.MARKUP
            )
            self.set_selection(
                self._selection.prune(
                    entity.id for entity in self._selectable_entities
                )
            )
        self._update_enabled_state()
        self._update_route_array_preview()

    def set_markup_state(self, *, visible: bool, guide_count: int) -> None:
        self._markup_guide_count = max(0, int(guide_count))
        self.set_markup_visible(visible)

    def set_guide_undo_available(self, available: bool) -> None:
        self._guide_undo_available = bool(available)
        self._update_enabled_state()

    def _on_markup_visibility_toggled(self, visible: bool) -> None:
        self._markup_visible = bool(visible)
        if not self._markup_visible:
            self._selectable_entities = tuple(
                entity
                for entity in self._selectable_entities
                if entity.owner is not EntityOwner.MARKUP
            )
            self.set_selection(
                self._selection.prune(
                    entity.id for entity in self._selectable_entities
                )
            )
        self.markup_visibility_changed.emit(self._markup_visible)
        self._update_enabled_state()

    def _route_measurement_control_state(self) -> RouteRunControlState:
        state = getattr(self, "_route_run_control_state", None)
        if isinstance(state, RouteRunControlState):
            return state
        state = RouteRunControlState()
        self._route_run_control_state = state
        return state

    @property
    def _route_measurement_running(self) -> bool:
        return self._route_measurement_control_state().running

    @_route_measurement_running.setter
    def _route_measurement_running(self, value: bool) -> None:
        self._route_run_control_state = self._route_measurement_control_state().with_running(
            value
        )

    @property
    def _route_measurement_waiting(self) -> bool:
        return self._route_measurement_control_state().waiting

    @_route_measurement_waiting.setter
    def _route_measurement_waiting(self, value: bool) -> None:
        state = self._route_measurement_control_state()
        self._route_run_control_state = state.with_waiting(value, state.waiting_reason)

    @property
    def _route_measurement_waiting_reason(self) -> str:
        return self._route_measurement_control_state().waiting_reason

    @_route_measurement_waiting_reason.setter
    def _route_measurement_waiting_reason(self, value: str) -> None:
        self._route_run_control_state = RouteRunControlState(
            running=self._route_measurement_running,
            waiting=self._route_measurement_waiting,
            waiting_reason=str(value or ""),
            pause_request_pending=self._route_pause_request_pending,
            interrupt_request_pending=self._route_interrupt_request_pending,
        )

    @property
    def _route_pause_request_pending(self) -> bool:
        return self._route_measurement_control_state().pause_request_pending

    @_route_pause_request_pending.setter
    def _route_pause_request_pending(self, value: bool) -> None:
        self._route_run_control_state = self._route_measurement_control_state().with_pause_request_pending(
            value
        )

    @property
    def _route_interrupt_request_pending(self) -> bool:
        return self._route_measurement_control_state().interrupt_request_pending

    @_route_interrupt_request_pending.setter
    def _route_interrupt_request_pending(self, value: bool) -> None:
        self._route_run_control_state = self._route_measurement_control_state().with_interrupt_request_pending(
            value
        )

    def set_route_measurement_running(self, running: bool) -> None:
        self._route_run_control_state = self._route_measurement_control_state().with_running(
            running
        )
        if self._route_measurement_running:
            self._route_run_status_label.setText("Route measurement running.")
        elif self._route_run_status_label.text() == "Route measurement running.":
            self._route_run_status_label.setText("Route measurement idle.")
        self._update_enabled_state()

    def set_route_measurement_waiting(
        self,
        waiting: bool,
        reason: str = "",
    ) -> None:
        self._route_run_control_state = self._route_measurement_control_state().with_waiting(
            waiting,
            reason,
        )
        self._update_enabled_state()

    def set_route_measurement_pause_request_pending(self, pending: bool) -> None:
        self._route_run_control_state = self._route_measurement_control_state().with_pause_request_pending(
            pending
        )
        self._update_enabled_state()

    def set_route_measurement_interrupt_request_pending(self, pending: bool) -> None:
        self._route_run_control_state = self._route_measurement_control_state().with_interrupt_request_pending(
            pending
        )
        self._update_enabled_state()

    def set_route_measurement_status(self, text: str) -> None:
        self._route_run_status_label.setText(text or "Route measurement idle.")

    def set_registration_status(self, text: str) -> None:
        self._registration_status_label.setText(text or "No design registration.")

    def set_design_registration_active(self, active: bool) -> None:
        self._design_registration_active = bool(active)
        self._update_enabled_state()

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
            self._current_design_position = None
        elif design_xy is None:
            self._current_design_position = None
            self._current_position_label.setText(
                f"Stage X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f}"
            )
        else:
            self._current_design_position = design_xy
            self._current_position_label.setText(
                f"Stage X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f} | "
                f"Design X={design_xy[0]:.3f}, Y={design_xy[1]:.3f}"
            )
        self._update_enabled_state()

    def set_status_message(self, text: str) -> None:
        self._availability_label.setText(text)

    def detach_tool_toolbar(self) -> QWidget:
        self._route_layout.removeWidget(self._tool_toolbar_widget)
        self._tool_toolbar_widget.setParent(None)
        return self._tool_toolbar_widget

    def detach_tool_options_panel(self) -> QWidget:
        self._route_layout.removeWidget(self._tool_group)
        self._tool_group.setParent(None)
        return self._tool_group

    def set_design_dialog_directory(self, directory: str | Path | None) -> None:
        """Update the preferred starting directory for opening GDS files."""

        if directory is None:
            self._design_dialog_directory = ""
            return
        self._design_dialog_directory = str(Path(directory))

    def set_hover_snap(self, snap_result: SnapResult | None) -> None:
        if not self._snap_enabled:
            self._snap_hint_label.setText("Snap off: clicks use the exact cursor position.")
            return
        if snap_result is None:
            self._snap_hint_label.setText("Hover snap: move over a line or corner.")
            return
        if snap_result.mode == "free":
            self._snap_hint_label.setText("Hover snap: no nearby geometry, click uses the exact cursor position.")
            return
        label = {
            "segment_center": "Center",
            "segment": "Line",
            "vertex": "Corner",
            "guide_end": "Guide End",
            "guide_center": "Guide Center",
            "guide_intersection": "Guide Intersection",
        }.get(snap_result.mode, "Line")
        self._snap_hint_label.setText(
            f"Hover snap: {label} at X={snap_result.point[0]:.3f}, "
            f"Y={snap_result.point[1]:.3f} | distance {snap_result.distance:.4f}"
        )

    def set_tool_hover_snap(
        self,
        snap_result: SnapResult | None,
        shift: bool,
        control: bool,
    ) -> None:
        self._update_tool_hover_preview(
            snap_result,
            shift=bool(shift),
            control=bool(control),
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
                "Load a GDS for registered navigation. Use Alignment to capture chip points, then click in the design view to move."
            )
        self._availability_label.setText(" ".join(messages))

    def _update_enabled_state(self) -> None:
        state = self._enabled_state()
        self._apply_document_enabled_state(state)
        self._apply_design_tool_enabled_state(state)
        self._apply_route_edit_enabled_state(state)
        self._apply_route_run_enabled_state(state)
        self._apply_route_tool_options_enabled_state(state)

    def _enabled_state(self) -> DesignNavigatorEnablement:
        route = self._route
        has_route = route is not None
        return DesignNavigatorEnablement(
            has_document=self._document is not None,
            has_route=has_route,
            route_saved=bool(route is not None and route.path is not None),
            route_has_points=bool(route is not None and route.points),
            has_route_selection=bool(
                route is not None
                and 0 <= self._selected_route_point_index < len(route.points)
            ),
            has_current_design_position=self._current_design_position is not None,
            route_running=self._route_measurement_running,
            design_registration_active=self._design_registration_active,
            route_control=self._route_measurement_control_state().presentation(),
            design_load_pending=self._design_load_pending,
        )

    def _apply_document_enabled_state(
        self,
        state: DesignNavigatorEnablement,
    ) -> None:
        self._unload_design_button.setEnabled(state.can_use_document_controls)
        self._top_cell_combo.setEnabled(state.can_use_document_controls)
        self._layer_list.setEnabled(state.can_use_document_controls)
        self._snap_checkbox.setEnabled(state.can_use_document_controls)
        self._route_new_button.setEnabled(state.can_edit_design)
        self._route_open_button.setEnabled(state.can_edit_design)
        self._route_save_button.setEnabled(state.can_save_route)
        self._route_save_as_button.setEnabled(state.can_save_route_as)
        self._clear_document_dependent_modes_if_needed(state)

    def _clear_document_dependent_modes_if_needed(
        self,
        state: DesignNavigatorEnablement,
    ) -> None:
        if state.has_document:
            return
        if self._route_pick_mode is not None:
            self._clear_route_pick_mode("Load a design to pick route geometry.")
        if self._active_design_tool != "select":
            self._set_design_tool("select")

    def _apply_design_tool_enabled_state(
        self,
        state: DesignNavigatorEnablement,
    ) -> None:
        self._select_tool_button.setEnabled(state.can_edit_design)
        self._point_tool_button.setEnabled(state.can_edit_design)
        self._align_tool_button.setEnabled(state.can_edit_design)
        self._guide_tool_button.setEnabled(state.can_edit_design)
        self._ruler_tool_button.setEnabled(state.can_edit_design)
        self._array_tool_button.setEnabled(state.can_edit_design)
        self._rotate_tool_button.setEnabled(state.can_use_rotate_tool)
        self._markup_visibility_button.setEnabled(
            state.can_use_document_controls
        )

    def _apply_route_edit_enabled_state(
        self,
        state: DesignNavigatorEnablement,
    ) -> None:
        for spinbox in (
            self._needle_1_dx_spin,
            self._needle_1_dy_spin,
            self._needle_2_dx_spin,
            self._needle_2_dy_spin,
        ):
            spinbox.setEnabled(state.can_edit_route_offsets)
        self._route_table.setEnabled(
            state.has_route and not state.design_load_pending
        )
        self._route_add_current_button.setEnabled(state.can_add_current_route_point)
        self._route_remove_button.setEnabled(state.can_remove_route_point)
        self._route_clear_button.setEnabled(state.can_clear_route)
        can_mutate_selection = state.can_use_tool_options and bool(self._selection.ids)
        self._selection_delete_button.setEnabled(can_mutate_selection)
        self._guide_undo_button.setEnabled(
            state.can_use_tool_options and self._guide_undo_available
        )
        self._guide_clear_button.setEnabled(
            state.can_use_tool_options
            and self._markup_guide_count > 0
        )

    def _apply_route_run_enabled_state(
        self,
        state: DesignNavigatorEnablement,
    ) -> None:
        route_control = state.route_control
        self._route_run_button.setEnabled(state.can_run_selected)
        self._route_stop_button.setEnabled(state.route_running)
        self._route_pause_button.setText(route_control.pause_text)
        self._route_pause_button.setEnabled(route_control.pause_enabled)
        self._route_interrupt_button.setText(route_control.interrupt_text)
        self._route_interrupt_button.setEnabled(route_control.interrupt_enabled)
        self._route_save_shift_button.setEnabled(state.can_save_shift)
        self._route_remeasure_button.setEnabled(state.can_confirm_waiting)
        self._route_skip_button.setEnabled(state.can_confirm_waiting)
        self._route_next_button.setEnabled(state.can_confirm_waiting)
        self._route_move_selected_button.setEnabled(state.can_move_selected)
        self._route_jump_selected_button.setEnabled(state.can_jump_selected)

    def _apply_route_tool_options_enabled_state(
        self,
        state: DesignNavigatorEnablement,
    ) -> None:
        for widget in (
            self._ruler_clear_button,
            self._ruler_cancel_button,
            self._route_array_dir1_step_x_spin,
            self._route_array_dir1_step_y_spin,
            self._route_array_dir1_count_spin,
            self._route_array_dir2_step_x_spin,
            self._route_array_dir2_step_y_spin,
            self._route_array_dir2_count_spin,
            self._route_array_serpentine_checkbox,
            self._route_array_pick_dir1_button,
            self._route_array_pick_dir2_button,
            self._route_array_cancel_button,
        ):
            widget.setEnabled(state.can_use_tool_options)
        self._route_array_create_button.setEnabled(
            state.can_use_tool_options and bool(self._selection.ids)
        )

    def _make_route_offset_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(-1_000_000_000.0, 1_000_000_000.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(1.0)
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(96)
        return spinbox

    def _make_route_coordinate_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(-1_000_000_000.0, 1_000_000_000.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(10.0)
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(112)
        return spinbox

    def _make_route_distance_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(0.0, 1_000_000_000.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(10.0)
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(112)
        return spinbox

    def _make_route_angle_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(-3600.0, 3600.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(5.0)
        spinbox.setSuffix(" deg")
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(112)
        return spinbox

    def _make_tool_button(
        self,
        parent: QWidget,
        text: str,
        icon: QIcon,
    ) -> QToolButton:
        button = QToolButton(parent)
        button.setText(text)
        button.setIcon(icon)
        button.setIconSize(QSize(28, 28))
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        button.setCheckable(True)
        button.setAutoRaise(True)
        return button

    def _make_icon_button(
        self,
        parent: QWidget,
        text: str,
        icon_name: str,
    ) -> QToolButton:
        button = QToolButton(parent)
        button.setText(text)
        button.setIcon(self._make_tool_icon(icon_name))
        button.setIconSize(QSize(18, 18))
        button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        button.setAutoRaise(False)
        return button

    def _make_tool_icon(self, name: str) -> QIcon:
        pixmap = QPixmap(28, 28)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        dark = QColor("#263238")
        accent = QColor("#0277bd")
        soft = QColor("#ffca28")
        painter.setPen(QPen(dark, 2.0))
        painter.setBrush(Qt.NoBrush)
        if name == "select":
            polygon = QPolygonF(
                [
                    QPointF(7, 4),
                    QPointF(20, 16),
                    QPointF(14, 17),
                    QPointF(17, 25),
                    QPointF(13, 26),
                    QPointF(10, 18),
                    QPointF(6, 23),
                ]
            )
            painter.setBrush(QBrush(QColor("#eceff1")))
            painter.drawPolygon(polygon)
        elif name == "point":
            painter.setPen(QPen(accent, 2.2))
            painter.drawLine(QPointF(14, 5), QPointF(14, 23))
            painter.drawLine(QPointF(5, 14), QPointF(23, 14))
            painter.setBrush(QBrush(soft))
            painter.drawEllipse(QPointF(14, 14), 3.2, 3.2)
        elif name == "guide":
            painter.setPen(QPen(accent, 2.2, Qt.DashLine))
            painter.drawLine(QPointF(5, 22), QPointF(23, 6))
            painter.setBrush(QBrush(soft))
            painter.drawEllipse(QPointF(5, 22), 2.5, 2.5)
            painter.drawEllipse(QPointF(23, 6), 2.5, 2.5)
        elif name == "eye":
            painter.setPen(QPen(accent, 2.0))
            painter.drawEllipse(4, 8, 20, 12)
            painter.setBrush(QBrush(soft))
            painter.drawEllipse(QPointF(14, 14), 3.5, 3.5)
        elif name == "ruler":
            painter.setPen(QPen(accent, 3.0))
            painter.drawLine(QPointF(5, 21), QPointF(23, 7))
            painter.setPen(QPen(dark, 1.5))
            for index in range(5):
                x = 7 + index * 4
                painter.drawLine(QPointF(x, 19 - index * 3), QPointF(x + 2, 21 - index * 3))
        elif name == "array":
            painter.setPen(QPen(accent, 2.0))
            painter.setBrush(QBrush(QColor("#e1f5fe")))
            for row in range(2):
                for column in range(3):
                    painter.drawEllipse(QPointF(8 + column * 7, 9 + row * 8), 2.2, 2.2)
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(QPointF(8, 9), QPointF(22, 17))
        elif name == "rotate":
            painter.save()
            painter.translate(28, 0)
            painter.scale(-1, 1)
            painter.setPen(QPen(accent, 2.4))
            painter.drawArc(5, 5, 18, 18, 35 * 16, 285 * 16)
            painter.drawLine(QPointF(20, 5), QPointF(23, 10))
            painter.drawLine(QPointF(20, 5), QPointF(15, 7))
            painter.setPen(QPen(dark, 1.8))
            painter.drawLine(QPointF(14, 9), QPointF(14, 19))
            painter.drawLine(QPointF(9, 14), QPointF(19, 14))
            painter.restore()
        elif name == "origin":
            painter.setPen(QPen(accent, 2.0))
            painter.drawLine(QPointF(14, 5), QPointF(14, 23))
            painter.drawLine(QPointF(5, 14), QPointF(23, 14))
            painter.setBrush(QBrush(soft))
            painter.drawEllipse(QPointF(14, 14), 3, 3)
        elif name == "direction":
            painter.setPen(QPen(accent, 2.5))
            painter.drawLine(QPointF(5, 20), QPointF(22, 8))
            painter.drawLine(QPointF(22, 8), QPointF(17, 8))
            painter.drawLine(QPointF(22, 8), QPointF(21, 13))
        elif name == "extent":
            painter.setPen(QPen(accent, 2.0))
            painter.drawLine(QPointF(6, 14), QPointF(22, 14))
            painter.drawLine(QPointF(22, 14), QPointF(18, 10))
            painter.drawLine(QPointF(22, 14), QPointF(18, 18))
            painter.setPen(QPen(soft, 2.0))
            painter.drawLine(QPointF(6, 8), QPointF(6, 20))
        elif name == "accept":
            painter.setPen(QPen(QColor("#2e7d32"), 3.0))
            painter.drawLine(QPointF(6, 15), QPointF(12, 21))
            painter.drawLine(QPointF(12, 21), QPointF(23, 7))
        elif name == "cancel":
            painter.setPen(QPen(QColor("#c62828"), 3.0))
            painter.drawLine(QPointF(8, 8), QPointF(21, 21))
            painter.drawLine(QPointF(21, 8), QPointF(8, 21))
        else:
            fallback = self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
            painter.end()
            return fallback
        painter.end()
        return QIcon(pixmap)

    def _set_route_offset_values(
        self,
        needle_1_dx: float,
        needle_1_dy: float,
        needle_2_dx: float,
        needle_2_dy: float,
    ) -> None:
        values = (
            (self._needle_1_dx_spin, needle_1_dx),
            (self._needle_1_dy_spin, needle_1_dy),
            (self._needle_2_dx_spin, needle_2_dx),
            (self._needle_2_dy_spin, needle_2_dy),
        )
        for spinbox, value in values:
            spinbox.blockSignals(True)
            spinbox.setValue(float(value))
            spinbox.blockSignals(False)

    def _emit_route_offsets_changed(self, *_unused: object) -> None:
        if self._updating_route_controls or self._route is None:
            return
        self.route_offsets_changed.emit(
            self._needle_1_dx_spin.value(),
            self._needle_1_dy_spin.value(),
            self._needle_2_dx_spin.value(),
            self._needle_2_dy_spin.value(),
        )
        self._update_route_array_preview()

    def _set_design_tool(self, tool: str) -> None:
        if tool not in {"select", "point", "guide", "ruler", "array", "align"}:
            tool = "select"
        previous_tool = self._active_design_tool
        if previous_tool == "align" and tool != "align" and self._alignment_draft_points:
            self._clear_alignment_draft(emit_discarded=True)
        self._active_design_tool = tool
        button_by_tool = {
            "select": self._select_tool_button,
            "point": self._point_tool_button,
            "align": self._align_tool_button,
            "guide": self._guide_tool_button,
            "ruler": self._ruler_tool_button,
            "array": self._array_tool_button,
        }
        for name, button in button_by_tool.items():
            button.blockSignals(True)
            button.setChecked(name == tool)
            button.blockSignals(False)
        stack_index_by_tool = {
            "select": 0,
            "point": 1,
            "guide": 2,
            "ruler": 3,
            "array": 4,
            "align": 5,
        }
        self._tool_stack.setCurrentIndex(stack_index_by_tool[tool])
        self._clear_route_pick_mode("")
        self._tool_group.setVisible(True)
        self.active_design_tool_changed.emit(tool)
        if tool == "select":
            self.route_preview_changed.emit(None)
            self.mixed_array_preview_changed.emit([], [])
            self.tool_measure_preview_changed.emit(None)
            self._tool_status_label.setText("")
        elif tool == "point":
            self.route_preview_changed.emit(None)
            self.mixed_array_preview_changed.emit([], [])
            self.tool_measure_preview_changed.emit(None)
            self._tool_status_label.setText("")
        elif tool == "align":
            self.route_preview_changed.emit(None)
            self.mixed_array_preview_changed.emit([], [])
            self.tool_measure_preview_changed.emit(None)
            self._refresh_alignment_draft_ui()
        elif tool == "guide":
            self.route_preview_changed.emit(None)
            self.mixed_array_preview_changed.emit([], [])
            self.tool_measure_preview_changed.emit(None)
            self._tool_status_label.setText("Click two points.")
        elif tool == "ruler":
            self.route_preview_changed.emit(None)
            self.mixed_array_preview_changed.emit([], [])
            self._ruler_anchor = None
            self._ruler_end = None
            self._update_ruler_labels()
            self._tool_status_label.setText("")
            self._route_pick_mode = "ruler"
            self.route_pick_mode_changed.emit("ruler")
        else:
            self.tool_measure_preview_changed.emit(None)
            self._tool_status_label.setText("Adjust array directions and counts.")
            self._update_route_array_preview()

    def cancel_active_tool(self) -> None:
        if self._active_design_tool == "select":
            self._clear_route_pick_mode("")
            return
        if self._active_design_tool == "ruler":
            self._ruler_anchor = None
            self._ruler_end = None
            self._update_ruler_labels()
            self.tool_measure_preview_changed.emit(None)
        if self._active_design_tool == "align":
            self._clear_alignment_draft(emit_discarded=True)
        self._set_design_tool("select")

    @property
    def alignment_draft_points(self) -> tuple[Point2D, ...]:
        return tuple(self._alignment_draft_points)

    def append_alignment_point(self, x_value: float, y_value: float) -> None:
        if self._active_design_tool != "align":
            return
        self._alignment_draft_points.append((float(x_value), float(y_value)))
        self.alignment_draft_changed.emit(self.alignment_draft_points)
        self._refresh_alignment_draft_ui()

    def accept_alignment_draft(self) -> None:
        if self._active_design_tool != "align" or not self._alignment_draft_is_valid():
            return
        points = self.alignment_draft_points
        self._clear_alignment_draft()
        self._set_design_tool("select")
        self.alignment_draft_accepted.emit(points)

    def _undo_alignment_point(self) -> None:
        if not self._alignment_draft_points:
            return
        self._alignment_draft_points.pop()
        self.alignment_draft_changed.emit(self.alignment_draft_points)
        self._refresh_alignment_draft_ui()

    def _clear_alignment_draft(self, *, emit_discarded: bool = False) -> None:
        had_points = bool(self._alignment_draft_points)
        self._alignment_draft_points.clear()
        if had_points:
            self.alignment_draft_changed.emit(())
        if emit_discarded:
            self.alignment_draft_discarded.emit()
        self._refresh_alignment_draft_ui()

    def _alignment_draft_is_valid(self) -> bool:
        return (
            len(self._alignment_draft_points) >= 2
            and len(set(self._alignment_draft_points)) >= 2
        )

    def _refresh_alignment_draft_ui(self) -> None:
        if not self._alignment_draft_points:
            text = "Click geometry to add D1, D2, and more."
        else:
            text = "\n".join(
                f"D{index}: {self._format_point(point)}"
                for index, point in enumerate(self._alignment_draft_points, start=1)
            )
        self._alignment_points_label.setText(text)
        self._alignment_undo_button.setEnabled(bool(self._alignment_draft_points))
        self._alignment_clear_button.setEnabled(bool(self._alignment_draft_points))
        self._alignment_done_button.setEnabled(self._alignment_draft_is_valid())

    def _clear_ruler(self) -> None:
        self._ruler_anchor = None
        self._ruler_end = None
        self._ruler_segments.clear()
        self._update_ruler_labels()
        self.tool_measure_preview_changed.emit(None)
        self.tool_measurements_changed.emit([])
        if self._active_design_tool == "ruler":
            self._tool_status_label.setText("")

    def _update_ruler_labels(self) -> None:
        self._ruler_start_label.setText(
            "Start: not set"
            if self._ruler_anchor is None
            else f"Start: {self._format_point(self._ruler_anchor)}"
        )
        self._ruler_end_label.setText(
            "End: not set"
            if self._ruler_end is None
            else f"End: {self._format_point(self._ruler_end)}"
        )
        if self._ruler_anchor is None or self._ruler_end is None:
            self._ruler_delta_label.setText("dX=0.000, dY=0.000")
            self._ruler_length_label.setText("Length=0.000, Angle=0.000 deg")
            if self._ruler_segments:
                self._ruler_length_label.setText(
                    f"{len(self._ruler_segments)} measurements"
                )
            return
        dx = self._ruler_end[0] - self._ruler_anchor[0]
        dy = self._ruler_end[1] - self._ruler_anchor[1]
        length = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx)) if length > 0.0 else 0.0
        self._ruler_delta_label.setText(f"dX={dx:.3f}, dY={dy:.3f}")
        self._ruler_length_label.setText(f"Length={length:.3f}, Angle={angle:.3f} deg")

    def _start_route_pick_mode(self, mode: str) -> None:
        if self._document is None:
            self._tool_status_label.setText("Load a design to pick geometry.")
            return
        if self._active_design_tool != "array":
            self._set_design_tool("array")
        self._route_pick_mode = mode
        self._route_pick_anchor_mode = None
        self._route_pick_anchor_point = None
        self.tool_measure_preview_changed.emit(None)
        self._tool_status_label.setText(f"Click design for {self._route_pick_label_text(mode)}.")
        self.route_pick_mode_changed.emit(mode)

    def _clear_route_pick_mode(self, status: str = "") -> None:
        self._route_pick_anchor_mode = None
        self._route_pick_anchor_point = None
        if self._route_pick_mode is not None:
            self._route_pick_mode = None
            self.route_pick_mode_changed.emit(None)
        self.tool_measure_preview_changed.emit(None)
        self._tool_status_label.setText(status)

    def apply_route_pick(
        self,
        mode: str,
        x_value: float,
        y_value: float,
        shift: bool = False,
        control: bool = False,
    ) -> None:
        point = (float(x_value), float(y_value))
        status = ""
        if mode == "ruler":
            if self._ruler_anchor is None:
                self._ruler_anchor = point
                self._ruler_end = None
                self.tool_measure_preview_changed.emit([point])
            else:
                point = constrain_vector_endpoint(
                    self._ruler_anchor,
                    point,
                    shift=bool(shift),
                    control=bool(control),
                )
                self._ruler_end = point
                self._ruler_segments.append((self._ruler_anchor, point))
                self.tool_measurements_changed.emit(list(self._ruler_segments))
                self.tool_measure_preview_changed.emit(None)
                self._ruler_anchor = None
                self._ruler_end = None
            self._update_ruler_labels()
            self._route_pick_mode = "ruler"
            self.route_pick_mode_changed.emit("ruler")
            return
        if mode == "array_dir1":
            anchor = self._route_vector_anchor_or_none(mode, point, "Direction 1")
            if anchor is None:
                return
            point = constrain_vector_endpoint(
                anchor,
                point,
                shift=bool(shift),
                control=bool(control),
            )
            step = (point[0] - anchor[0], point[1] - anchor[1])
            length, angle = self._vector_length_angle(step)
            self._route_array_dir1_step_x_spin.setValue(length)
            self._route_array_dir1_step_y_spin.setValue(angle)
            status = f"Direction 1 set to length={length:.3f}, angle={angle:.3f} deg."
        elif mode == "array_dir2":
            anchor = self._route_vector_anchor_or_none(mode, point, "Direction 2")
            if anchor is None:
                return
            point = constrain_vector_endpoint(
                anchor,
                point,
                shift=bool(shift),
                control=bool(control),
            )
            step = (point[0] - anchor[0], point[1] - anchor[1])
            length, angle = self._vector_length_angle(step)
            self._route_array_dir2_step_x_spin.setValue(length)
            self._route_array_dir2_step_y_spin.setValue(angle)
            status = f"Direction 2 set to length={length:.3f}, angle={angle:.3f} deg."
        else:
            status = "Unknown route pick mode."
        self._clear_route_pick_mode(status)
        self._update_route_array_preview()

    def _route_vector_anchor_or_none(
        self,
        mode: str,
        point: Point2D,
        label: str,
    ) -> Point2D | None:
        if self._route_pick_anchor_mode != mode or self._route_pick_anchor_point is None:
            self._route_pick_anchor_mode = mode
            self._route_pick_anchor_point = point
            self.tool_measure_preview_changed.emit([point])
            self._tool_status_label.setText(
                f"{label} vector starts at {self._format_point(point)}; click endpoint."
            )
            return None
        return self._route_pick_anchor_point

    def _emit_route_array_requested(self) -> None:
        if not self._selection.ids:
            return
        request = MixedArrayRequest(
            direction_1=self._route_array_dir1_step(),
            count_1=self._route_array_dir1_count_spin.value(),
            direction_2=self._route_array_dir2_step(),
            count_2=self._route_array_dir2_count_spin.value(),
            serpentine=self._route_array_serpentine_checkbox.isChecked(),
            source_ids=self._selection.ids,
        )
        self.mixed_array_requested.emit(request)
        self._set_design_tool("select")

    def _cancel_route_array(self) -> None:
        self.route_preview_changed.emit(None)
        self.mixed_array_preview_changed.emit([], [])
        self._set_design_tool("select")

    def _update_tool_hover_preview(
        self,
        snap_result: SnapResult | None,
        *,
        shift: bool = False,
        control: bool = False,
    ) -> None:
        if snap_result is None:
            return
        point = snap_result.point
        if self._active_design_tool == "ruler":
            if self._ruler_anchor is not None and self._ruler_end is None:
                point = constrain_vector_endpoint(
                    self._ruler_anchor,
                    point,
                    shift=bool(shift),
                    control=bool(control),
                )
                self.tool_measure_preview_changed.emit([self._ruler_anchor, point])
                self._ruler_end = point
                self._update_ruler_labels()
                self._ruler_end = None
            return
        if self._active_design_tool != "array":
            return
        if self._route_pick_mode == "array_dir1" and self._route_pick_anchor_point is not None:
            point = constrain_vector_endpoint(
                self._route_pick_anchor_point,
                point,
                shift=bool(shift),
                control=bool(control),
            )
            step = (
                point[0] - self._route_pick_anchor_point[0],
                point[1] - self._route_pick_anchor_point[1],
            )
            self.tool_measure_preview_changed.emit([self._route_pick_anchor_point, point])
            self._update_route_array_preview(dir1_override=step)
        elif self._route_pick_mode == "array_dir2" and self._route_pick_anchor_point is not None:
            point = constrain_vector_endpoint(
                self._route_pick_anchor_point,
                point,
                shift=bool(shift),
                control=bool(control),
            )
            step = (
                point[0] - self._route_pick_anchor_point[0],
                point[1] - self._route_pick_anchor_point[1],
            )
            self.tool_measure_preview_changed.emit([self._route_pick_anchor_point, point])
            self._update_route_array_preview(dir2_override=step)

    def _update_route_array_preview(
        self,
        *_unused: object,
        dir1_override: Point2D | None = None,
        dir2_override: Point2D | None = None,
    ) -> None:
        if (
            self._document is None
            or self._active_design_tool != "array"
            or not self._selection.ids
        ):
            self.route_preview_changed.emit(None)
            self.mixed_array_preview_changed.emit([], [])
            return
        dir1 = dir1_override or self._route_array_dir1_step()
        dir2 = dir2_override or self._route_array_dir2_step()
        request = MixedArrayRequest(
            direction_1=dir1,
            count_1=self._route_array_dir1_count_spin.value(),
            direction_2=dir2,
            count_2=self._route_array_dir2_count_spin.value(),
            serpentine=self._route_array_serpentine_checkbox.isChecked(),
            source_ids=self._selection.ids,
        )
        plan = plan_mixed_array(
            self._selectable_entities,
            self._selection.ids,
            request,
            route=self._route,
            edit_safe=not self._route_measurement_running,
        )
        if not plan.accepted:
            self.mixed_array_preview_changed.emit([], [])
            return
        self.mixed_array_preview_changed.emit(
            [point.camera_center for point in plan.route_copies],
            [guide.geometry() for guide in plan.guide_copies],
        )

    def _route_array_dir1_step(self) -> Point2D:
        return self._vector_from_length_angle(
            self._route_array_dir1_step_x_spin.value(),
            self._route_array_dir1_step_y_spin.value(),
        )

    def _route_array_dir2_step(self) -> Point2D:
        return self._vector_from_length_angle(
            self._route_array_dir2_step_x_spin.value(),
            self._route_array_dir2_step_y_spin.value(),
        )

    def _current_route_offset_vectors(self) -> list[Point2D]:
        if self._route is not None and len(self._route.needle_offsets) >= 2:
            return [
                (self._route.needle_offsets[0].dx, self._route.needle_offsets[0].dy),
                (self._route.needle_offsets[1].dx, self._route.needle_offsets[1].dy),
            ]
        return [
            (self._needle_1_dx_spin.value(), self._needle_1_dy_spin.value()),
            (self._needle_2_dx_spin.value(), self._needle_2_dy_spin.value()),
        ]

    @staticmethod
    def _vector_from_length_angle(length: float, angle_degrees: float) -> Point2D:
        return vector_from_length_angle(length, angle_degrees)

    @staticmethod
    def _vector_length_angle(vector: Point2D) -> tuple[float, float]:
        return vector_length_angle(vector)

    @staticmethod
    def _route_pick_label_text(mode: str) -> str:
        return route_pick_label_text(mode)

    def _choose_design_file(self) -> None:  # pragma: no cover - UI interaction
        start_directory = self._design_dialog_directory
        if not start_directory and self._document is not None:
            start_directory = str(self._document.path.parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Design",
            start_directory,
            "Layout files (*.gds *.gdsii *.oas *.oasis);;All files (*)",
        )
        if path:
            self.load_design_requested.emit(path)

    def _choose_route_file(self) -> None:  # pragma: no cover - UI interaction
        start_directory = ""
        if self._route is not None and self._route.path is not None:
            start_directory = str(self._route.path.parent)
        elif self._document is not None:
            start_directory = str(self._document.path.parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Probe Route",
            start_directory,
            "Probe routes (*.probe-route.json *.json);;All files (*)",
        )
        if path:
            self.route_open_requested.emit(path)

    def _choose_route_save_file(self) -> None:  # pragma: no cover - UI interaction
        if self._route is None:
            return
        start_directory = ""
        if self._route.path is not None:
            start_directory = str(self._route.path.parent)
        elif self._document is not None:
            start_directory = str(self._document.path.parent)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Probe Route",
            start_directory,
            "Probe routes (*.probe-route.json);;JSON files (*.json);;All files (*)",
        )
        if path:
            self.route_save_as_requested.emit(path)

    def _on_top_cell_changed(self, cell_name: str) -> None:
        if self._document is None or not cell_name:
            return
        if cell_name == self._document.top_cell_name:
            return
        self.top_cell_changed.emit(cell_name)

    def set_snap_enabled(self, enabled: bool) -> None:
        """Update the visible snap toggle state without re-emitting it."""

        self._snap_enabled = bool(enabled)
        self._snap_checkbox.blockSignals(True)
        self._snap_checkbox.setChecked(self._snap_enabled)
        self._snap_checkbox.blockSignals(False)
        self.set_hover_snap(None)

    def _on_snap_checkbox_toggled(self, checked: bool) -> None:
        self._snap_enabled = bool(checked)
        self.set_hover_snap(None)
        self.snap_enabled_changed.emit(self._snap_enabled)

    def _on_layer_item_changed(self, item: QListWidgetItem) -> None:
        layer_key = item.data(Qt.UserRole)
        if not isinstance(layer_key, tuple) or len(layer_key) != 2:
            return
        self.layer_visibility_changed.emit(
            int(layer_key[0]),
            int(layer_key[1]),
            item.checkState() == Qt.Checked,
        )

    def _on_route_selection_changed(self) -> None:
        if self._updating_route_controls:
            return
        selected_rows = self._selected_route_row_indices()
        row = selected_rows[0] if selected_rows else -1
        self._selected_route_point_index = row
        self.route_selected.emit(row)
        route_ids = {
            route_entity_id(self._route.points[index].id)
            for index in selected_rows
            if self._route is not None and 0 <= index < len(self._route.points)
        }
        modifiers = self._route_selection_modifiers()
        if modifiers & Qt.ControlModifier:
            existing_route_ids = {
                entity_id
                for entity_id in self._selection.ids
                if entity_id.startswith("route:")
            }
            self.selection_requested.emit(
                existing_route_ids ^ route_ids,
                "invert",
            )
        elif modifiers & Qt.ShiftModifier:
            self.selection_requested.emit(route_ids, "add")
        else:
            self.selection_requested.emit(route_ids, "replace")
        self._update_enabled_state()
        self._update_route_array_preview()

    def _route_selection_modifiers(self) -> Qt.KeyboardModifiers:
        return QApplication.keyboardModifiers()

    def _selected_route_row_indices(self) -> list[int]:
        selection_model = self._route_table.selectionModel()
        if selection_model is None:
            return []
        return sorted({index.row() for index in selection_model.selectedRows()})

    def _emit_route_measurement_jump_to_selected(self) -> None:
        if self._selected_route_point_index < 0:
            return
        self.route_measurement_jump_requested.emit(
            self._selected_route_point_index + 1
        )

    def _emit_route_measurement_measure_selected(self) -> None:
        if self._route_measurement_waiting:
            self.route_measurement_confirmation_requested.emit("measure")
            return
        if self._selected_route_point_index < 0:
            return
        self.route_measurement_measure_requested.emit(
            self._selected_route_point_index + 1
        )

    def _emit_route_measurement_save_shift_selected(self) -> None:
        if self._selected_route_point_index < 0:
            return
        self.route_measurement_save_shift_requested.emit(
            self._selected_route_point_index + 1
        )

    def _emit_route_measurement_move_to_selected(self) -> None:
        if self._selected_route_point_index < 0:
            return
        self.route_measurement_move_requested.emit(
            self._selected_route_point_index + 1
        )

    def _emit_route_measurement_pause_or_resume(self) -> None:
        action = self._route_measurement_control_state().pause_action()
        if action == "resume":
            self.route_measurement_confirmation_requested.emit("next")
            return
        if action == "interrupt":
            self.set_route_measurement_interrupt_request_pending(True)
            self.route_measurement_interrupt_requested.emit()
            return
        self.set_route_measurement_pause_request_pending(True)
        self.route_measurement_pause_requested.emit()

    def _emit_route_measurement_interrupt_or_resume(self) -> None:
        self._emit_route_measurement_pause_or_resume()

    @staticmethod
    def _format_bounds(bounds: tuple[float, float, float, float]) -> str:
        return format_bounds(bounds)

    @staticmethod
    def _format_mark_label(label: str, point: Point2D | None) -> str:
        return format_mark_label(label, point)

    @staticmethod
    def _format_point(point: Point2D) -> str:
        return format_point(point)


class DesignLayoutWindow(QWidget):
    """Top-level design window combining the layout view and design controls."""

    calibration_point_selected = Signal(int, float, float)
    alignment_draft_accepted = Signal(object)
    alignment_draft_discarded = Signal()
    move_requested = Signal(float, float)
    route_point_requested = Signal(float, float)
    point_requested = Signal(float, float)
    guide_requested = Signal(object, object)
    selection_changed = Signal(object)
    delete_selection_requested = Signal()
    guide_undo_requested = Signal()
    guide_clear_requested = Signal()
    markup_visibility_changed = Signal(bool)
    mixed_array_requested = Signal(object)
    hover_snap_changed = Signal(object)
    visibility_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        _ = parent
        super().__init__(None)
        self._document: DesignDocument | None = None
        self._probe_route: MeasurementRoute | None = None
        self._markup: MarkupDocument | None = None
        self._selection = SelectionModel()
        self._selection_initialized = False
        self._selectable_entities: tuple[SelectableDesignEntity, ...] = ()
        self.setWindowTitle("Design Window")
        self.setWindowFlag(Qt.Window, True)
        self.resize(1480, 920)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        self._main_view = _DesignPlotPane(parent=self)
        self.navigator_panel = DesignNavigatorPanel(self)
        tool_toolbar = self.navigator_panel.detach_tool_toolbar()
        tool_toolbar.setObjectName("DesignToolToolbar")
        tool_toolbar.setMinimumHeight(52)
        tool_options = self.navigator_panel.detach_tool_options_panel()
        tool_options.setMinimumWidth(300)
        tool_options.setMaximumWidth(360)
        self.navigator_panel.setMinimumWidth(400)
        navigator_scroll = QScrollArea(self)
        navigator_scroll.setWidgetResizable(True)
        navigator_scroll.setFrameShape(QScrollArea.NoFrame)
        navigator_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        navigator_scroll.setWidget(self.navigator_panel)
        navigator_scroll.setMinimumWidth(430)
        self._main_view.calibration_point_selected.connect(
            self.calibration_point_selected.emit
        )
        self._main_view.move_requested.connect(self.move_requested.emit)
        self._main_view.route_point_requested.connect(self.route_point_requested.emit)
        self._main_view.point_requested.connect(self.point_requested.emit)
        self._main_view.alignment_point_requested.connect(
            self.navigator_panel.append_alignment_point
        )
        self._main_view.guide_requested.connect(self.guide_requested.emit)
        self._main_view.entity_selection_requested.connect(
            self._apply_selection_request
        )
        self._main_view.route_pick_requested.connect(
            self.navigator_panel.apply_route_pick
        )
        self._main_view.tool_hover_snap_changed.connect(
            self.navigator_panel.set_tool_hover_snap
        )
        self._main_view.hover_snap_changed.connect(self.hover_snap_changed.emit)
        self.navigator_panel.route_pick_mode_changed.connect(
            self._main_view.set_route_pick_mode
        )
        self.navigator_panel.route_preview_changed.connect(
            self._main_view.set_probe_route_preview
        )
        self.navigator_panel.tool_measure_preview_changed.connect(
            self._main_view.set_tool_measure_points
        )
        self.navigator_panel.tool_measurements_changed.connect(
            self._main_view.set_tool_measure_segments
        )
        self.navigator_panel.active_design_tool_changed.connect(
            self._main_view.set_active_design_tool
        )
        self.navigator_panel.alignment_draft_changed.connect(
            self._main_view.set_alignment_draft_points
        )
        self.navigator_panel.alignment_draft_accepted.connect(
            self.alignment_draft_accepted.emit
        )
        self.navigator_panel.alignment_draft_discarded.connect(
            self.alignment_draft_discarded.emit
        )
        self.navigator_panel.selection_requested.connect(
            self._apply_selection_request
        )
        self.navigator_panel.delete_selection_requested.connect(
            self.delete_selection_requested.emit
        )
        self.navigator_panel.guide_undo_requested.connect(
            self.guide_undo_requested.emit
        )
        self.navigator_panel.guide_clear_requested.connect(
            self.guide_clear_requested.emit
        )
        self.navigator_panel.markup_visibility_changed.connect(
            self.markup_visibility_changed.emit
        )
        self.navigator_panel.mixed_array_requested.connect(
            self.mixed_array_requested.emit
        )
        self.navigator_panel.mixed_array_preview_changed.connect(
            self._main_view.set_mixed_array_preview
        )
        self._escape_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._escape_shortcut.setContext(Qt.WindowShortcut)
        self._escape_shortcut.activated.connect(self._cancel_active_interaction)
        self._home_shortcut = QShortcut(QKeySequence("Home"), self)
        self._home_shortcut.setContext(Qt.WindowShortcut)
        self._home_shortcut.activated.connect(self._main_view.focus_gds_bounds)
        self._accept_alignment_shortcut = QShortcut(QKeySequence("Return"), self)
        self._accept_alignment_shortcut.setContext(Qt.WindowShortcut)
        self._accept_alignment_shortcut.activated.connect(
            self.navigator_panel.accept_alignment_draft
        )
        self._accept_alignment_enter_shortcut = QShortcut(QKeySequence("Enter"), self)
        self._accept_alignment_enter_shortcut.setContext(Qt.WindowShortcut)
        self._accept_alignment_enter_shortcut.activated.connect(
            self.navigator_panel.accept_alignment_draft
        )
        self._main_view.set_active_design_tool("select")

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.addWidget(tool_options, 0)
        content_layout.addWidget(self._main_view, 1)
        content_layout.addWidget(navigator_scroll, 0)
        root_layout.addWidget(tool_toolbar, 0)
        root_layout.addLayout(content_layout, 1)

    def set_document(self, document: DesignDocument | None) -> None:
        self._document = document
        self._main_view.set_document(document)
        self.navigator_panel.set_document(document)

    def set_design_load_pending(self, pending: bool) -> None:
        self.navigator_panel.set_design_load_pending(pending)
        self._escape_shortcut.setEnabled(not bool(pending))

    def set_document_preview(self, document: DesignDocument) -> None:
        self._main_view.set_document_preview(document)
        self.navigator_panel.set_status_message("Loading Markup...")

    def finish_document_preview(
        self,
        document: DesignDocument | None,
    ) -> None:
        self._main_view.finish_document_preview(document)
        self.navigator_panel.set_status_message("")

    def set_markup(self, markup: MarkupDocument | None) -> None:
        self._markup = markup
        self._main_view.set_markup(markup)
        self.navigator_panel.set_markup_state(
            visible=bool(markup is not None and markup.visible),
            guide_count=len(markup.guides) if markup is not None else 0,
        )
        self._refresh_selectable_entities()

    def set_selection(self, selection: SelectionModel) -> None:
        self._selection_initialized = True
        self._selection = selection.prune(
            entity.id for entity in self._selectable_entities
        )
        self._main_view.set_selection(self._selection)
        self.navigator_panel.set_selection(self._selection)

    @property
    def selection(self) -> SelectionModel:
        return self._selection

    def set_guide_undo_available(self, available: bool) -> None:
        self.navigator_panel.set_guide_undo_available(available)

    def _refresh_selectable_entities(self) -> None:
        self._selectable_entities = project_entities(
            self._probe_route,
            self._markup,
        )
        self._main_view.set_selectable_entities(self._selectable_entities)
        self.navigator_panel.set_selectable_entities(self._selectable_entities)
        self._selection = self._selection.prune(
            entity.id for entity in self._selectable_entities
        )
        self._main_view.set_selection(self._selection)
        self.navigator_panel.set_selection(self._selection)

    def _apply_selection_request(self, entity_ids: object, mode: str) -> None:
        ids = (
            {str(entity_id) for entity_id in entity_ids}
            if isinstance(entity_ids, (set, frozenset, list, tuple))
            else set()
        )
        self.set_selection(self._selection.apply(ids, str(mode)))
        self.selection_changed.emit(self._selection)

    def _cancel_active_interaction(self) -> None:
        self._main_view.cancel_active_interaction()
        self.navigator_panel.cancel_active_tool()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._main_view.set_document(None)
        super().closeEvent(event)

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._main_view.set_targets(targets, selected_target_id=selected_target_id)
        self.navigator_panel.set_targets(targets, selected_target_id=selected_target_id)

    def set_probe_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> None:
        initialize_selection = not self._selection_initialized
        self._probe_route = route
        self._main_view.set_probe_route(
            route,
            selected_route_point_index=selected_route_point_index,
        )
        self.navigator_panel.set_route(
            route,
            selected_route_point_index=selected_route_point_index,
        )
        self._refresh_selectable_entities()
        if (
            initialize_selection
            and route is not None
            and 0 <= selected_route_point_index < len(route.points)
        ):
            self.set_selection(
                SelectionModel(
                    frozenset(
                        {route_entity_id(route.points[selected_route_point_index].id)}
                    )
                )
            )

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> None:
        self._main_view.set_registration_marks(source_design_marks, check_design_marks)
        self.navigator_panel.set_registration_marks(source_design_marks, check_design_marks)

    def set_alignment_capture_points(self, points: object) -> None:
        """Keep accepted D1..Dn visible while their stage points are captured."""

        self._main_view.set_alignment_draft_points(points)

    def set_navigation_enabled(self, enabled: bool) -> None:
        """Toggle click-to-move behavior in the design plot."""

        self._main_view.set_navigation_enabled(enabled)

    def set_route_edit_enabled(self, enabled: bool) -> None:
        """Toggle route point placement in the design plot."""

        self._main_view.set_route_edit_enabled(enabled)

    def set_snap_enabled(self, enabled: bool) -> None:
        """Toggle geometry snapping in the design plot and sidebar."""

        self._main_view.set_snap_enabled(enabled)
        self.navigator_panel.set_snap_enabled(enabled)

    def set_stage_registration_marks(self, source_stage_marks: list[Point2D | None]) -> None:
        self.navigator_panel.set_stage_registration_marks(source_stage_marks)

    def set_calibration_prompt(self, text: str) -> None:
        self.navigator_panel.set_calibration_prompt(text)

    def set_registration_status(self, text: str) -> None:
        self.navigator_panel.set_registration_status(text)

    def set_status_message(self, text: str) -> None:
        self._main_view.set_status_message(text)
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
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.setWindowState(self.windowState() | Qt.WindowActive)
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # type: ignore[override]
        if self._document is not None and self._main_view._document is None:
            self._main_view.set_document(self._document)
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self.visibility_changed.emit(False)


__all__ = ["DesignLayoutWindow", "DesignNavigatorPanel"]
