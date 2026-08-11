"""Qt adapter for Design tool controls and their pure tool session."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt, Signal
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
    QButtonGroup,
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import probe_station_gui.design.tool_session as design_tools
from probe_station_gui.design.model import SnapResult
from probe_station_gui.design.selection_model import (
    EntityOwner,
    SelectableDesignEntity,
    SelectionModel,
)
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.shared.wheel_guard import (
    GuardedDoubleSpinBox as QDoubleSpinBox,
    GuardedSpinBox as QSpinBox,
)
from probe_station_gui.views.design_navigator_enablement import (
    DesignNavigatorEnablement,
)


class DesignToolControls(QWidget):
    """Own Design tool UI state and emit immutable tool-session effects."""

    rotate_requested = Signal(int)
    active_tool_changed = Signal(str)
    route_pick_mode_changed = Signal(object)
    route_preview_changed = Signal(object)
    measure_preview_changed = Signal(object)
    measurements_changed = Signal(object)
    alignment_draft_changed = Signal(object)
    alignment_draft_accepted = Signal(object)
    alignment_draft_discarded = Signal()
    delete_selection_requested = Signal()
    guide_undo_requested = Signal()
    guide_clear_requested = Signal()
    markup_visibility_changed = Signal(bool)
    mixed_array_requested = Signal(object)
    mixed_array_preview_changed = Signal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = design_tools.DesignToolSession()
        self._selection = SelectionModel()
        self._selectable_entities: tuple[SelectableDesignEntity, ...] = ()
        self._markup_visible = True
        self._guide_count = 0
        self._guide_undo_available = False

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(8)

        self.toolbar_widget = QWidget(self)
        tool_buttons = QHBoxLayout(self.toolbar_widget)
        tool_buttons.setContentsMargins(0, 0, 0, 0)
        tool_buttons.setSpacing(4)
        self._tool_button_group = QButtonGroup(self.toolbar_widget)
        self._tool_button_group.setExclusive(True)
        self._select_tool_button = self._make_tool_button("Select", "select")
        self._move_tool_button = self._make_tool_button("Move", "move")
        self._point_tool_button = self._make_tool_button("Point", "point")
        self._align_tool_button = self._make_tool_button("Align", "align")
        self._guide_tool_button = self._make_tool_button("Guide", "guide")
        self._ruler_tool_button = self._make_tool_button("Ruler", "ruler")
        self._array_tool_button = self._make_tool_button("Array", "array")
        self._rotate_tool_button = self._make_tool_button("Rotate", "rotate")
        self._rotate_tool_button.setCheckable(False)
        for button in (
            self._select_tool_button,
            self._move_tool_button,
            self._point_tool_button,
            self._align_tool_button,
            self._guide_tool_button,
            self._ruler_tool_button,
            self._array_tool_button,
        ):
            self._tool_button_group.addButton(button)
        self._markup_visibility_button = self._make_tool_button("Markup", "eye")
        self._markup_visibility_button.setToolTip("Show Markup")
        self._markup_visibility_button.setChecked(True)
        self._select_tool_button.setChecked(True)
        for button in (
            self._select_tool_button,
            self._move_tool_button,
            self._point_tool_button,
            self._align_tool_button,
            self._guide_tool_button,
            self._ruler_tool_button,
            self._array_tool_button,
            self._rotate_tool_button,
            self._markup_visibility_button,
        ):
            tool_buttons.addWidget(button)
        tool_buttons.addStretch(1)
        root_layout.addWidget(self.toolbar_widget)

        self.options_panel = QGroupBox("Tool Options", self)
        tool_layout = QVBoxLayout(self.options_panel)
        self._tool_status_label = QLabel("", self.options_panel)
        self._tool_status_label.setWordWrap(True)
        self._tool_status_label.setStyleSheet("QLabel { color: #607d8b; }")
        tool_layout.addWidget(self._tool_status_label)
        self._tool_stack = QStackedWidget(self.options_panel)

        select_page = QWidget(self.options_panel)
        select_layout = QVBoxLayout(select_page)
        select_layout.setContentsMargins(0, 0, 0, 0)
        self._selection_count_label = QLabel("Nothing selected.", select_page)
        self._selection_delete_button = QPushButton("Delete", select_page)
        select_layout.addWidget(self._selection_count_label)
        select_layout.addWidget(self._selection_delete_button)
        self._tool_stack.addWidget(select_page)

        move_page = QWidget(self.options_panel)
        move_layout = QVBoxLayout(move_page)
        move_layout.setContentsMargins(0, 0, 0, 0)
        move_layout.addWidget(QLabel("Click a target. Drag to pan.", move_page))
        self._tool_stack.addWidget(move_page)

        point_page = QWidget(self.options_panel)
        point_layout = QVBoxLayout(point_page)
        point_layout.setContentsMargins(0, 0, 0, 0)
        point_layout.addWidget(QLabel("Click to place route points.", point_page))
        self._tool_stack.addWidget(point_page)

        guide_page = QWidget(self.options_panel)
        guide_layout = QHBoxLayout(guide_page)
        guide_layout.setContentsMargins(0, 0, 0, 0)
        self._guide_undo_button = QPushButton("Undo Last", guide_page)
        self._guide_clear_button = QPushButton("Clear All", guide_page)
        guide_layout.addWidget(self._guide_undo_button)
        guide_layout.addWidget(self._guide_clear_button)
        self._tool_stack.addWidget(guide_page)

        ruler_page = QWidget(self.options_panel)
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

        array_page = QWidget(self.options_panel)
        array_layout = QGridLayout(array_page)
        array_layout.setContentsMargins(0, 0, 0, 0)
        self._route_array_dir1_step_x_spin = self._make_distance_spinbox(array_page)
        self._route_array_dir1_step_y_spin = self._make_angle_spinbox(array_page)
        self._route_array_dir2_step_x_spin = self._make_distance_spinbox(array_page)
        self._route_array_dir2_step_y_spin = self._make_angle_spinbox(array_page)
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

        align_page = QWidget(self.options_panel)
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
        root_layout.addWidget(self.options_panel)

        tool_actions = (
            (self._select_tool_button, "select"),
            (self._move_tool_button, "move"),
            (self._point_tool_button, "point"),
            (self._align_tool_button, "align"),
            (self._guide_tool_button, "guide"),
            (self._ruler_tool_button, "ruler"),
            (self._array_tool_button, "array"),
        )
        for button, tool in tool_actions:
            button.clicked.connect(
                lambda _checked=False, selected_tool=tool: self.activate(selected_tool)
            )
        self._rotate_tool_button.clicked.connect(
            lambda _checked=False: self.rotate_requested.emit(1)
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
            lambda _checked=False: self.activate("select")
        )
        self._alignment_undo_button.clicked.connect(self._undo_alignment)
        self._alignment_clear_button.clicked.connect(self._clear_alignment)
        self._alignment_done_button.clicked.connect(self.accept_alignment_draft)
        self._route_array_pick_dir1_button.clicked.connect(
            lambda _checked=False: self._begin_array_direction("array_dir1")
        )
        self._route_array_pick_dir2_button.clicked.connect(
            lambda _checked=False: self._begin_array_direction("array_dir2")
        )
        self._route_array_create_button.clicked.connect(self._create_array)
        self._route_array_cancel_button.clicked.connect(self._cancel_array)
        for widget in self._array_controls():
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self._on_array_controls_changed)
            else:
                widget.toggled.connect(self._on_array_controls_changed)

        self._delete_shortcut = QShortcut(QKeySequence.Delete, self)
        self._delete_shortcut.setContext(Qt.WindowShortcut)
        self._delete_shortcut.activated.connect(self.delete_selection_requested.emit)
        self._apply_transition(self._session.activate("select"))

    @property
    def selection(self) -> SelectionModel:
        return self._selection

    @property
    def selectable_entities(self) -> tuple[SelectableDesignEntity, ...]:
        return self._selectable_entities

    def detach_toolbar(self) -> QWidget:
        layout = self.layout()
        if layout is not None:
            layout.removeWidget(self.toolbar_widget)
        self.toolbar_widget.setParent(None)
        return self.toolbar_widget

    def detach_options_panel(self) -> QWidget:
        layout = self.layout()
        if layout is not None:
            layout.removeWidget(self.options_panel)
        self.options_panel.setParent(None)
        return self.options_panel

    def replace_context(
        self,
        *,
        document_token: int | None,
        route: MeasurementRoute | None,
        route_changed: bool,
        edit_safe: bool,
    ) -> None:
        context = self._session.context.replace_inputs(
            document_token=document_token,
            selection_ids=self._selection.ids,
            selectable_entities=self._selectable_entities,
            edit_safe=edit_safe,
        )
        if route_changed:
            context = context.replace_route(route)
        self._apply_transition(self._session.replace_context(context))

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

    def set_markup_visible(self, visible: bool) -> None:
        self._markup_visible = bool(visible)
        self._markup_visibility_button.blockSignals(True)
        self._markup_visibility_button.setChecked(self._markup_visible)
        self._markup_visibility_button.blockSignals(False)
        self._prune_hidden_markup()

    def set_markup_state(self, *, visible: bool, guide_count: int) -> None:
        self._guide_count = max(0, int(guide_count))
        self.set_markup_visible(visible)

    def set_guide_undo_available(self, available: bool) -> None:
        self._guide_undo_available = bool(available)

    def hover(
        self,
        snap_result: SnapResult | None,
        *,
        shift: bool,
        control: bool,
    ) -> None:
        if snap_result is None:
            return
        self._apply_transition(
            self._session.hover(
                snap_result.point,
                shift=bool(shift),
                control=bool(control),
                generation=self._session.context_generation,
            )
        )

    def activate(self, tool: str) -> None:
        self._apply_transition(self._session.activate(tool))

    def cancel(self) -> None:
        self._apply_transition(self._session.cancel())

    def append_alignment_point(self, x_value: float, y_value: float) -> None:
        self._apply_transition(
            self._session.append_alignment_point((float(x_value), float(y_value)))
        )

    def accept_alignment_draft(self) -> None:
        self._apply_transition(self._session.accept_alignment())

    def apply_route_pick(
        self,
        mode: str,
        x_value: float,
        y_value: float,
        shift: bool = False,
        control: bool = False,
    ) -> None:
        self._apply_transition(
            self._session.pick(
                (float(x_value), float(y_value)),
                mode=mode,
                generation=self._session.context_generation,
                shift=bool(shift),
                control=bool(control),
            )
        )

    def refresh_array_preview(self) -> None:
        self._apply_transition(self._session.refresh_array_preview())

    def apply_enablement(self, state: DesignNavigatorEnablement) -> None:
        self._select_tool_button.setEnabled(state.can_edit_design)
        self._move_tool_button.setEnabled(state.can_move_design)
        self._point_tool_button.setEnabled(state.can_edit_design)
        self._align_tool_button.setEnabled(state.can_edit_design)
        self._guide_tool_button.setEnabled(state.can_edit_design)
        self._ruler_tool_button.setEnabled(state.can_edit_design)
        self._array_tool_button.setEnabled(state.can_edit_design)
        self._rotate_tool_button.setEnabled(state.can_use_rotate_tool)
        self._markup_visibility_button.setEnabled(state.can_use_document_controls)
        self._selection_delete_button.setEnabled(
            state.can_use_tool_options and bool(self._selection.ids)
        )
        self._guide_undo_button.setEnabled(
            state.can_use_tool_options and self._guide_undo_available
        )
        self._guide_clear_button.setEnabled(
            state.can_use_tool_options and self._guide_count > 0
        )
        for widget in self._option_controls():
            widget.setEnabled(state.can_use_tool_options)
        self._route_array_create_button.setEnabled(
            state.can_use_tool_options and bool(self._selection.ids)
        )
        self._delete_shortcut.setEnabled(not state.design_load_pending)
        if not state.has_document and (
            self._session.pick_mode is not None or self._session.active_tool != "select"
        ):
            self._apply_transition(self._session.activate("select"))
        if self._session.active_tool == "move" and not state.can_move_design:
            self._apply_transition(self._session.activate("select"))

    def _make_tool_button(self, text: str, icon_name: str) -> QToolButton:
        button = QToolButton(self.toolbar_widget)
        button.setText(text)
        button.setIcon(self._make_tool_icon(icon_name))
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

    def _make_distance_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(0.0, 1_000_000_000.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(10.0)
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(112)
        return spinbox

    def _make_angle_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(-3600.0, 3600.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(5.0)
        spinbox.setSuffix(" deg")
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(112)
        return spinbox

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
        elif name in {"move", "point"}:
            painter.setPen(QPen(accent, 2.2))
            painter.drawLine(
                QPointF(14, 5 if name == "point" else 4),
                QPointF(14, 23 if name == "point" else 24),
            )
            painter.drawLine(
                QPointF(5 if name == "point" else 4, 14),
                QPointF(23 if name == "point" else 24, 14),
            )
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
                painter.drawLine(
                    QPointF(x, 19 - index * 3),
                    QPointF(x + 2, 21 - index * 3),
                )
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
        elif name == "direction":
            painter.setPen(QPen(accent, 2.5))
            painter.drawLine(QPointF(5, 20), QPointF(22, 8))
            painter.drawLine(QPointF(22, 8), QPointF(17, 8))
            painter.drawLine(QPointF(22, 8), QPointF(21, 13))
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

    def _array_controls(self) -> tuple[QWidget, ...]:
        return (
            self._route_array_dir1_step_x_spin,
            self._route_array_dir1_step_y_spin,
            self._route_array_dir1_count_spin,
            self._route_array_dir2_step_x_spin,
            self._route_array_dir2_step_y_spin,
            self._route_array_dir2_count_spin,
            self._route_array_serpentine_checkbox,
        )

    def _option_controls(self) -> tuple[QWidget, ...]:
        return (
            self._ruler_clear_button,
            self._ruler_cancel_button,
            *self._array_controls(),
            self._route_array_pick_dir1_button,
            self._route_array_pick_dir2_button,
            self._route_array_cancel_button,
        )

    def _array_configuration(self) -> design_tools.ArrayToolConfiguration:
        return design_tools.ArrayToolConfiguration(
            direction_1_length=self._route_array_dir1_step_x_spin.value(),
            direction_1_angle_degrees=self._route_array_dir1_step_y_spin.value(),
            count_1=self._route_array_dir1_count_spin.value(),
            direction_2_length=self._route_array_dir2_step_x_spin.value(),
            direction_2_angle_degrees=self._route_array_dir2_step_y_spin.value(),
            count_2=self._route_array_dir2_count_spin.value(),
            serpentine=self._route_array_serpentine_checkbox.isChecked(),
        )

    def _on_array_controls_changed(self, *_unused: object) -> None:
        self._apply_transition(
            self._session.configure_array(self._array_configuration())
        )

    def _begin_array_direction(self, mode: str) -> None:
        self._refresh_context_inputs()
        self._apply_transition(self._session.begin_array_direction(mode))

    def _create_array(self) -> None:
        self._apply_transition(self._session.create_array())

    def _cancel_array(self) -> None:
        self._apply_transition(self._session.cancel_array())

    def _clear_ruler(self) -> None:
        self._apply_transition(self._session.clear_ruler())

    def _undo_alignment(self) -> None:
        self._apply_transition(self._session.undo_alignment_point())

    def _clear_alignment(self, *_unused: object) -> None:
        self._apply_transition(self._session.clear_alignment())

    def _on_markup_visibility_toggled(self, visible: bool) -> None:
        self._markup_visible = bool(visible)
        self._prune_hidden_markup()
        self.markup_visibility_changed.emit(self._markup_visible)

    def _prune_hidden_markup(self) -> None:
        if self._markup_visible:
            return
        self._selectable_entities = tuple(
            entity
            for entity in self._selectable_entities
            if entity.owner is not EntityOwner.MARKUP
        )
        self.set_selection(
            self._selection.prune(entity.id for entity in self._selectable_entities)
        )
        self._refresh_context_inputs()

    def _refresh_context_inputs(self) -> None:
        current = self._session.context
        context = current.replace_inputs(
            document_token=current.document_token,
            selection_ids=self._selection.ids,
            selectable_entities=self._selectable_entities,
            edit_safe=current.edit_safe,
        )
        self._apply_transition(self._session.replace_context(context))

    def _apply_transition(
        self,
        transition: design_tools.DesignToolTransition,
    ) -> None:
        if transition.stages:
            for stage in transition.stages:
                self._session = stage.session.rebase_context_from(self._session)
                self._render_session()
                for effect in stage.effects:
                    self._emit_effect(effect)
            return
        self._session = transition.session
        self._render_session()
        for effect in transition.effects:
            self._emit_effect(effect)

    def _render_session(self) -> None:
        tool = self._session.active_tool
        buttons = {
            "select": self._select_tool_button,
            "move": self._move_tool_button,
            "point": self._point_tool_button,
            "align": self._align_tool_button,
            "guide": self._guide_tool_button,
            "ruler": self._ruler_tool_button,
            "array": self._array_tool_button,
        }
        for name, button in buttons.items():
            button.blockSignals(True)
            button.setChecked(name == tool)
            button.blockSignals(False)
        self._tool_stack.setCurrentIndex(
            {
                "select": 0,
                "move": 1,
                "point": 2,
                "guide": 3,
                "ruler": 4,
                "array": 5,
                "align": 6,
            }[tool]
        )
        self.options_panel.setVisible(True)
        self._tool_status_label.setText(self._session.status_message)
        self._alignment_points_label.setText(self._session.alignment_text)
        has_alignment_points = bool(self._session.alignment_draft)
        self._alignment_undo_button.setEnabled(has_alignment_points)
        self._alignment_clear_button.setEnabled(has_alignment_points)
        self._alignment_done_button.setEnabled(self._session.alignment_valid)
        readout = self._session.ruler_readout
        self._ruler_start_label.setText(readout.start_text)
        self._ruler_end_label.setText(readout.end_text)
        self._ruler_delta_label.setText(readout.delta_text)
        self._ruler_length_label.setText(readout.length_text)
        self._render_array_configuration()

    def _render_array_configuration(self) -> None:
        config = self._session.array_configuration
        values = (
            (self._route_array_dir1_step_x_spin, config.direction_1_length),
            (self._route_array_dir1_step_y_spin, config.direction_1_angle_degrees),
            (self._route_array_dir1_count_spin, config.count_1),
            (self._route_array_dir2_step_x_spin, config.direction_2_length),
            (self._route_array_dir2_step_y_spin, config.direction_2_angle_degrees),
            (self._route_array_dir2_count_spin, config.count_2),
            (self._route_array_serpentine_checkbox, config.serpentine),
        )
        for widget, value in values:
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            else:
                widget.setValue(value)
            widget.blockSignals(False)

    def _emit_effect(self, effect: design_tools.DesignToolEffect) -> None:
        kind = effect.kind
        value = effect.value
        if kind is design_tools.DesignToolEffectKind.ACTIVE_TOOL_CHANGED:
            self.active_tool_changed.emit(str(value))
        elif kind is design_tools.DesignToolEffectKind.PICK_MODE_CHANGED:
            self.route_pick_mode_changed.emit(value)
        elif kind is design_tools.DesignToolEffectKind.ROUTE_PREVIEW_CHANGED:
            self.route_preview_changed.emit(value)
        elif kind is design_tools.DesignToolEffectKind.MEASURE_PREVIEW_CHANGED:
            self.measure_preview_changed.emit(None if value is None else list(value))
        elif kind is design_tools.DesignToolEffectKind.MEASUREMENTS_CHANGED:
            self.measurements_changed.emit(list(value))
        elif kind is design_tools.DesignToolEffectKind.ALIGNMENT_CHANGED:
            self.alignment_draft_changed.emit(tuple(value))
        elif kind is design_tools.DesignToolEffectKind.ALIGNMENT_ACCEPTED:
            self.alignment_draft_accepted.emit(tuple(value))
        elif kind is design_tools.DesignToolEffectKind.ALIGNMENT_DISCARDED:
            self.alignment_draft_discarded.emit()
        elif kind is design_tools.DesignToolEffectKind.MIXED_ARRAY_REQUESTED:
            self.mixed_array_requested.emit(value)
        elif kind is design_tools.DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED:
            preview = (
                value
                if isinstance(value, design_tools.MixedArrayPreview)
                else design_tools.MixedArrayPreview()
            )
            self.mixed_array_preview_changed.emit(
                list(preview.route_points),
                list(preview.guide_segments),
            )


__all__ = ["DesignToolControls"]
