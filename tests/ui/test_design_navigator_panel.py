from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication

import probe_station_gui.design.tool_context as tool_contexts
from probe_station_gui.design.model import SnapResult
from probe_station_gui.design.selection_geometry import PointGeometry, SegmentGeometry
from probe_station_gui.design.selection_model import (
    EntityOwner,
    SelectableDesignEntity,
    SelectionModel,
    markup_entity_id,
    route_entity_id,
)
from probe_station_gui.route.model import MeasurementRoute, RouteDesignBinding, RoutePoint
from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _route(*, path: str | None = None, point_count: int = 1) -> MeasurementRoute:
    route = MeasurementRoute(
        name="test route",
        design=RouteDesignBinding(
            path="design.gds",
            sha256="0" * 64,
            top_cell_name="TOP",
            bounds=(0.0, 0.0, 100.0, 100.0),
            dbu=0.001,
        ),
        points=[
            RoutePoint(
                id=f"p{index + 1:03d}",
                label=f"P{index + 1:03d}",
                camera_center=(float(index), float(index)),
            )
            for index in range(point_count)
        ],
    )
    if path is not None:
        route.path = Path(path)
    return route


def _set_document_available(panel: DesignNavigatorPanel) -> None:
    panel.document_controls._document = object()
    panel._replace_tool_context()
    panel._update_enabled_state()


def test_design_navigator_disables_document_and_route_controls_without_design(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    panel.tool_controls._ruler_tool_button.click()

    panel.set_document(None)

    assert not panel.document_controls.unload_button.isEnabled()
    assert not panel.document_controls.top_cell_combo.isEnabled()
    assert not panel.document_controls.layer_list.isEnabled()
    assert not panel._snap_checkbox.isEnabled()
    assert not panel.route_controls.new_button.isEnabled()
    assert not panel.route_controls.open_button.isEnabled()
    assert not panel.tool_controls._select_tool_button.isEnabled()
    assert not panel.tool_controls._ruler_tool_button.isEnabled()
    assert not panel.tool_controls._array_tool_button.isEnabled()
    assert not panel.tool_controls._rotate_tool_button.isEnabled()
    assert panel.tool_controls._select_tool_button.isChecked()

    panel.deleteLater()


def test_focus_reference_controls_use_finished_product_copy_and_readiness(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_design_registration_active(True)

    panel.set_focus_reference_state(z_ready=False, a_ready=False)

    assert panel.registration_controls.find_focus_button.text() == "Find focus reference"
    assert panel.registration_controls.use_selected_focus_button.text() == "Use selected point"
    assert panel.registration_controls.reset_focus_button.text() == "Reset focus reference"
    assert panel.registration_controls.find_focus_button.isEnabled()
    assert not panel.registration_controls.use_selected_focus_button.isEnabled()
    assert not panel.registration_controls.reset_focus_button.isEnabled()

    panel.set_focus_selection_available(True)
    assert panel.registration_controls.use_selected_focus_button.isEnabled()

    panel.set_focus_reference_state(z_ready=True, a_ready=True)

    assert not panel.registration_controls.find_focus_button.isEnabled()
    assert panel.registration_controls.reset_focus_button.isEnabled()
    assert panel.registration_controls.focus_status_label.text() == "Focus and contact references ready."
    panel.deleteLater()


def test_registration_instance_selector_uses_uuid_and_obeys_edit_guard(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    selected: list[str] = []
    created: list[bool] = []
    panel.registration_instance_selected.connect(selected.append)
    panel.new_registration_requested.connect(lambda: created.append(True))

    panel.set_registration_instances(
        (
            ("11111111-1111-4111-8111-111111111111", "loaded"),
            ("22222222-2222-4222-8222-222222222222", "loaded (2)"),
        ),
        selected_frame_id="11111111-1111-4111-8111-111111111111",
    )

    assert selected == []
    assert panel.registration_controls.instance_combo.itemData(0) == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert panel.registration_controls.instance_combo.itemData(1) == (
        "22222222-2222-4222-8222-222222222222"
    )
    panel.registration_controls.instance_combo.setCurrentIndex(1)
    assert selected == ["22222222-2222-4222-8222-222222222222"]
    panel.registration_controls.new_button.click()
    assert created == [True]

    panel.set_route_measurement_running(True)
    assert not panel.registration_controls.instance_combo.isEnabled()
    assert not panel.registration_controls.new_button.isEnabled()
    panel.deleteLater()


def test_design_navigator_enables_idle_route_controls_with_route_selection(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_route(_route(point_count=2), selected_route_point_index=0)
    panel.set_current_position((1.0, 2.0), (3.0, 4.0))

    assert panel.route_controls.new_button.isEnabled()
    assert panel.route_controls.open_button.isEnabled()
    assert not panel.route_controls.save_button.isEnabled()
    assert panel.route_controls.save_as_button.isEnabled()
    assert panel.tool_controls._select_tool_button.isEnabled()
    assert panel.tool_controls._ruler_tool_button.isEnabled()
    assert panel.tool_controls._array_tool_button.isEnabled()
    assert panel.tool_controls._rotate_tool_button.isEnabled()
    assert panel.route_controls.add_current_button.isEnabled()
    assert panel.route_controls.remove_button.isEnabled()
    assert panel.route_controls.clear_button.isEnabled()
    assert panel.route_run_controls.measure_button.isEnabled()
    assert panel.route_run_controls.move_selected_button.isEnabled()
    assert not panel.route_run_controls.stop_button.isEnabled()
    assert not panel.route_run_controls.save_shift_button.isEnabled()
    assert not panel.route_run_controls.jump_selected_button.isEnabled()

    panel.deleteLater()


def test_design_navigator_locks_old_design_controls_while_new_design_loads(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_route(_route(point_count=2), selected_route_point_index=0)
    panel.set_current_position((1.0, 2.0), (3.0, 4.0))

    panel.set_design_load_pending(True)

    assert not panel.document_controls.unload_button.isEnabled()
    assert not panel.document_controls.top_cell_combo.isEnabled()
    assert not panel.document_controls.layer_list.isEnabled()
    assert not panel._snap_checkbox.isEnabled()
    assert not panel.route_controls.new_button.isEnabled()
    assert not panel.route_controls.open_button.isEnabled()
    assert not panel.route_controls.save_as_button.isEnabled()
    assert not panel.route_controls.route_table.isEnabled()
    assert not panel.route_controls.add_current_button.isEnabled()
    assert not panel.route_controls.remove_button.isEnabled()
    assert not panel.route_controls.clear_button.isEnabled()
    assert not panel.route_run_controls.measure_button.isEnabled()
    assert not panel.route_run_controls.move_selected_button.isEnabled()
    assert not panel.tool_controls._markup_visibility_button.isEnabled()
    assert not panel.tool_controls._delete_shortcut.isEnabled()

    panel.deleteLater()


def test_design_tools_are_exclusive_and_markup_eye_is_independent(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    tools: list[str] = []
    panel.active_design_tool_changed.connect(tools.append)

    assert panel.tool_controls._select_tool_button.isChecked()
    assert panel.tool_controls._markup_visibility_button.isChecked()
    panel.tool_controls._point_tool_button.click()
    assert panel.tool_controls._point_tool_button.isChecked()
    assert not panel.tool_controls._select_tool_button.isChecked()
    panel.tool_controls._guide_tool_button.click()
    assert panel.tool_controls._guide_tool_button.isChecked()
    assert not panel.tool_controls._point_tool_button.isChecked()
    panel.tool_controls._markup_visibility_button.click()

    assert panel.tool_controls._guide_tool_button.isChecked()
    assert tools[-2:] == ["point", "guide"]
    panel.set_design_registration_active(True)
    panel.tool_controls._markup_visibility_button.setChecked(True)
    panel.tool_controls._move_tool_button.click()
    assert panel.tool_controls._move_tool_button.isChecked()
    assert not panel.tool_controls._guide_tool_button.isChecked()
    assert panel.tool_controls._markup_visibility_button.isChecked()
    panel.tool_controls._markup_visibility_button.click()
    assert panel.tool_controls._move_tool_button.isChecked()
    assert tools[-1] == "move"
    panel.deleteLater()


def test_move_tool_is_adjacent_to_select_and_requires_registration(qt_app) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()

    assert panel.tool_controls._move_tool_button.isEnabled() is False
    toolbar_layout = panel.tool_controls.toolbar_widget.layout()
    assert toolbar_layout.itemAt(0).widget() is panel.tool_controls._select_tool_button
    assert toolbar_layout.itemAt(1).widget() is panel.tool_controls._move_tool_button

    panel.set_design_registration_active(True)
    assert panel.tool_controls._move_tool_button.isEnabled() is True

    panel.tool_controls._move_tool_button.click()
    assert panel.tool_controls._move_tool_button.isChecked()
    panel.set_design_registration_active(False)
    assert panel.tool_controls._select_tool_button.isChecked()
    panel.deleteLater()


def test_route_run_disables_move_tool(qt_app) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    panel.set_design_registration_active(True)
    panel.tool_controls._move_tool_button.click()
    panel.set_route_measurement_running(True)
    assert panel.tool_controls._move_tool_button.isEnabled() is False
    assert panel.tool_controls._select_tool_button.isChecked()
    panel.deleteLater()


def test_design_load_disables_move_tool_and_returns_to_select(qt_app) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    panel.set_design_registration_active(True)
    panel.tool_controls._move_tool_button.click()

    panel.set_design_load_pending(True)

    assert panel.tool_controls._move_tool_button.isEnabled() is False
    assert panel.tool_controls._select_tool_button.isChecked()
    panel.deleteLater()


def test_move_tool_is_exclusive_and_escape_returns_select(qt_app) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_design_registration_active(True)
    tools: list[str] = []
    panel.active_design_tool_changed.connect(tools.append)

    panel.tool_controls._move_tool_button.click()
    assert panel.tool_controls._move_tool_button.isChecked()
    assert not panel.tool_controls._select_tool_button.isChecked()

    panel.cancel_active_tool()
    assert panel.tool_controls._select_tool_button.isChecked()
    assert not panel.tool_controls._move_tool_button.isChecked()
    assert tools[-2:] == ["move", "select"]
    panel.deleteLater()


def test_align_tool_builds_numbered_draft_with_undo_clear_and_done(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    accepted: list[object] = []
    changed: list[object] = []
    panel.alignment_draft_accepted.connect(accepted.append)
    panel.alignment_draft_changed.connect(changed.append)

    panel.tool_controls._align_tool_button.click()
    panel.append_alignment_point(1.0, 2.0)
    panel.append_alignment_point(5.0, 6.0)
    panel.append_alignment_point(9.0, 3.0)

    assert changed[-1] == (
        (1.0, 2.0),
        (5.0, 6.0),
        (9.0, 3.0),
    )
    assert "D1" in panel.tool_controls._alignment_points_label.text()
    assert "D3" in panel.tool_controls._alignment_points_label.text()
    assert panel.tool_controls._alignment_done_button.isEnabled()

    panel.tool_controls._alignment_undo_button.click()
    assert changed[-1] == ((1.0, 2.0), (5.0, 6.0))
    panel.tool_controls._alignment_done_button.click()

    assert accepted == [((1.0, 2.0), (5.0, 6.0))]
    assert panel.tool_controls._select_tool_button.isChecked()
    assert changed
    panel.deleteLater()


def test_align_done_requires_two_distinct_points_and_clear_keeps_tool_active(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    panel.tool_controls._align_tool_button.click()
    panel.append_alignment_point(1.0, 2.0)
    panel.append_alignment_point(1.0, 2.0)

    assert not panel.tool_controls._alignment_done_button.isEnabled()
    panel.tool_controls._alignment_clear_button.click()
    assert panel.tool_controls._alignment_points_label.text() == "Click geometry to add D1, D2, and more."
    assert panel.tool_controls._align_tool_button.isChecked()
    panel.deleteLater()


@pytest.mark.parametrize("with_point", [False, True])
def test_escape_discards_align_draft_and_returns_to_select(
    qt_app: QApplication,
    with_point: bool,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    discarded: list[bool] = []
    panel.alignment_draft_discarded.connect(lambda: discarded.append(True))
    panel.tool_controls._align_tool_button.click()
    if with_point:
        panel.append_alignment_point(1.0, 2.0)

    panel.cancel_active_tool()

    assert panel.tool_controls._alignment_points_label.text() == "Click geometry to add D1, D2, and more."
    assert panel.tool_controls._select_tool_button.isChecked()
    assert discarded == [True]
    panel.deleteLater()


def test_align_discard_observers_see_align_until_next_tool_commits(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    panel.tool_controls._align_tool_button.click()
    panel.append_alignment_point(1.0, 2.0)
    observed: list[tuple[object, ...]] = []
    panel.alignment_draft_changed.connect(
        lambda points: observed.append(
            (
                "changed",
                points,
                panel.tool_controls._align_tool_button.isChecked(),
                panel.tool_controls._guide_tool_button.isChecked(),
            )
        )
    )
    panel.alignment_draft_discarded.connect(
        lambda: observed.append(
            (
                "discarded",
                panel.tool_controls._align_tool_button.isChecked(),
                panel.tool_controls._guide_tool_button.isChecked(),
            )
        )
    )
    panel.active_design_tool_changed.connect(
        lambda tool: observed.append(
            (
                "active",
                tool,
                panel.tool_controls._align_tool_button.isChecked(),
                panel.tool_controls._guide_tool_button.isChecked(),
            )
        )
    )

    panel.tool_controls._guide_tool_button.click()

    assert observed == [
        ("changed", (), True, False),
        ("discarded", True, False),
        ("active", "guide", False, True),
    ]
    panel.deleteLater()


def test_design_ruler_uses_canonical_label_and_token(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    tools: list[str] = []
    panel.active_design_tool_changed.connect(tools.append)

    panel.tool_controls._ruler_tool_button.click()

    assert panel.tool_controls._ruler_tool_button.text() == "Ruler"
    assert tools[-1] == "ruler"
    assert panel.route_run_controls.measure_button.text() == "Measure"
    panel.deleteLater()


def test_ruler_preview_and_commit_share_shift_constraint(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    panel.tool_controls._ruler_tool_button.click()
    previews: list[object] = []
    completed: list[object] = []
    panel.tool_measure_preview_changed.connect(previews.append)
    panel.tool_measurements_changed.connect(completed.append)

    panel.apply_route_pick("ruler", 1.0, 2.0, False, False)
    panel.set_tool_hover_snap(
        SnapResult((6.0, 4.0), "vertex", 0.1),
        True,
        False,
    )

    assert previews[-1] == [(1.0, 2.0), (6.0, 2.0)]
    panel.apply_route_pick("ruler", 6.0, 4.0, True, False)
    assert completed[-1] == [((1.0, 2.0), (6.0, 2.0))]
    panel.deleteLater()


def test_array_directions_use_ctrl_diagonal_for_commit_and_preview(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    previews: list[object] = []
    panel.tool_measure_preview_changed.connect(previews.append)

    panel.tool_controls._route_array_pick_dir1_button.click()
    panel.apply_route_pick("array_dir1", 0.0, 0.0, False, False)
    panel.apply_route_pick("array_dir1", 4.0, 3.0, False, True)

    assert panel.tool_controls._route_array_dir1_step_x_spin.value() == pytest.approx(
        3.5 * 2**0.5,
        abs=0.001,
    )
    assert panel.tool_controls._route_array_dir1_step_y_spin.value() == pytest.approx(45.0)

    panel.tool_controls._route_array_pick_dir2_button.click()
    panel.apply_route_pick("array_dir2", 0.0, 0.0, False, False)
    panel.set_tool_hover_snap(
        SnapResult((4.0, -3.0), "vertex", 0.1),
        False,
        True,
    )

    assert previews[-1] == [(0.0, 0.0), (3.5, -3.5)]
    panel.deleteLater()


def test_escape_exits_ruler_but_preserves_completed_segments(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    panel.tool_controls._ruler_tool_button.click()
    completed: list[object] = []
    panel.tool_measurements_changed.connect(completed.append)
    panel.apply_route_pick("ruler", 0.0, 0.0)
    panel.apply_route_pick("ruler", 2.0, 0.0)
    panel.apply_route_pick("ruler", 5.0, 5.0)

    panel.cancel_active_tool()

    assert panel.tool_controls._select_tool_button.isChecked()
    assert completed == [[((0.0, 0.0), (2.0, 0.0))]]
    assert panel.tool_controls._ruler_length_label.text() == "1 measurements"
    panel.deleteLater()


@pytest.mark.parametrize("tool", ["point", "guide", "array"])
def test_escape_returns_drawing_tools_to_select(
    qt_app: QApplication,
    tool: str,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    {
        "point": panel.tool_controls._point_tool_button,
        "guide": panel.tool_controls._guide_tool_button,
        "array": panel.tool_controls._array_tool_button,
    }[tool].click()

    panel.cancel_active_tool()

    assert panel.tool_controls._select_tool_button.isChecked()
    panel.deleteLater()


def test_point_guide_and_mutations_disable_while_route_is_running(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_route(_route(point_count=1), selected_route_point_index=0)
    panel.set_selectable_entities(
        [
            SelectableDesignEntity(
                route_entity_id("p001"),
                EntityOwner.ROUTE,
                "p001",
                PointGeometry((0.0, 0.0)),
                route_index=0,
            ),
            SelectableDesignEntity(
                markup_entity_id("g1"),
                EntityOwner.MARKUP,
                "g1",
                SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
            ),
        ]
    )
    panel.set_selection(SelectionModel(frozenset({route_entity_id("p001")})))

    assert panel.tool_controls._point_tool_button.isEnabled()
    assert panel.tool_controls._guide_tool_button.isEnabled()
    assert panel.tool_controls._selection_delete_button.isEnabled()
    assert panel.tool_controls._route_array_create_button.isEnabled()

    panel.set_route_measurement_running(True)

    assert not panel.tool_controls._point_tool_button.isEnabled()
    assert not panel.tool_controls._guide_tool_button.isEnabled()
    assert not panel.tool_controls._selection_delete_button.isEnabled()
    assert not panel.tool_controls._guide_clear_button.isEnabled()
    assert not panel.tool_controls._guide_undo_button.isEnabled()
    assert not panel.tool_controls._route_array_create_button.isEnabled()
    panel.deleteLater()


def test_array_page_is_selection_driven_without_origin_extent_or_replace(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()

    assert not hasattr(panel, "_route_array_origin_x_spin")
    assert not hasattr(panel, "_route_array_origin_y_spin")
    assert not hasattr(panel, "_route_array_pick_origin_button")
    assert not hasattr(panel, "_route_array_pick_extent1_button")
    assert not hasattr(panel, "_route_array_pick_extent2_button")
    assert not hasattr(panel, "_route_array_replace_checkbox")
    panel.deleteLater()


def test_array_create_requires_one_selected_visible_entity(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    guide = SelectableDesignEntity(
        markup_entity_id("g1"),
        EntityOwner.MARKUP,
        "g1",
        SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
    )
    panel.set_selectable_entities([guide])
    panel._update_enabled_state()

    assert not panel.tool_controls._route_array_create_button.isEnabled()
    panel.set_selection(SelectionModel(frozenset({guide.id})))
    assert panel.tool_controls._route_array_create_button.isEnabled()
    panel.set_markup_visible(False)
    assert not panel.tool_controls._route_array_create_button.isEnabled()
    panel.deleteLater()


def test_programmatic_markup_hide_clears_owned_tool_session_preview(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    guide = SelectableDesignEntity(
        markup_entity_id("g1"),
        EntityOwner.MARKUP,
        "g1",
        SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
    )
    panel.set_selectable_entities([guide])
    panel.set_selection(SelectionModel(frozenset({guide.id})))
    previews: list[tuple[list[object], list[object]]] = []
    panel.mixed_array_preview_changed.connect(
        lambda route_points, guide_segments: previews.append(
            (list(route_points), list(guide_segments))
        )
    )
    panel.tool_controls._array_tool_button.click()
    assert panel.tool_controls._session.mixed_array_preview.guide_segments
    previews.clear()

    panel.set_markup_visible(False)

    assert not panel.tool_controls._session.context.selection_ids
    assert panel.tool_controls._session.mixed_array_preview.is_empty
    assert previews == [([], [])]
    panel.deleteLater()


def test_user_markup_hide_clears_preview_before_visibility_signal(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    guide = SelectableDesignEntity(
        markup_entity_id("g1"),
        EntityOwner.MARKUP,
        "g1",
        SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
    )
    panel.set_selectable_entities([guide])
    panel.set_selection(SelectionModel(frozenset({guide.id})))
    panel.tool_controls._array_tool_button.click()
    observed: list[tuple[object, ...]] = []
    panel.mixed_array_preview_changed.connect(
        lambda route_points, guide_segments: observed.append(
            ("preview", list(route_points), list(guide_segments))
        )
    )
    panel.markup_visibility_changed.connect(
        lambda visible: observed.append(
            (
                "visibility",
                visible,
                panel.tool_controls._session.context.selection_ids,
                panel.tool_controls._session.mixed_array_preview.is_empty,
            )
        )
    )

    panel.tool_controls._markup_visibility_button.click()

    assert observed == [
        ("preview", [], []),
        ("visibility", False, frozenset(), True),
    ]
    panel.deleteLater()


def test_array_request_observer_sees_array_until_select_effect(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    guide = SelectableDesignEntity(
        markup_entity_id("g1"),
        EntityOwner.MARKUP,
        "g1",
        SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
    )
    panel.set_selectable_entities([guide])
    panel.set_selection(SelectionModel(frozenset({guide.id})))
    panel.tool_controls._array_tool_button.click()
    observed: list[tuple[object, ...]] = []

    def observe_request(_request: object) -> None:
        observed.append(
            (
                "request",
                panel.tool_controls._array_tool_button.isChecked(),
                panel.tool_controls._select_tool_button.isChecked(),
            )
        )
        panel.set_selection(SelectionModel())

    panel.mixed_array_requested.connect(observe_request)
    panel.active_design_tool_changed.connect(
        lambda tool: observed.append(
            (
                "active",
                tool,
                panel.tool_controls._array_tool_button.isChecked(),
                panel.tool_controls._select_tool_button.isChecked(),
            )
        )
    )

    panel.tool_controls._route_array_create_button.click()

    assert observed == [
        ("request", True, False),
        ("active", "select", False, True),
    ]
    assert not panel.tool_controls._session.context.selection_ids
    panel.deleteLater()


def test_selection_refresh_reuses_the_route_snapshot(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_calls: list[MeasurementRoute] = []
    original_capture = tool_contexts._RouteSnapshot.capture.__func__

    def counted_capture(
        cls: type[tool_contexts._RouteSnapshot],
        route: MeasurementRoute,
    ) -> tool_contexts._RouteSnapshot:
        capture_calls.append(route)
        return original_capture(cls, route)

    monkeypatch.setattr(
        tool_contexts._RouteSnapshot,
        "capture",
        classmethod(counted_capture),
    )
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    route = _route(point_count=2)
    panel.set_route(route, selected_route_point_index=0)
    entities = [
        SelectableDesignEntity(
            route_entity_id(point.id),
            EntityOwner.ROUTE,
            point.id,
            PointGeometry(point.camera_center),
            route_index=index,
        )
        for index, point in enumerate(route.points)
    ]
    captures_after_route = len(capture_calls)

    panel.set_selectable_entities(entities)
    panel.set_selection(SelectionModel(frozenset({entities[0].id})))
    panel.set_selection(SelectionModel(frozenset({entities[1].id})))

    assert captures_after_route == 1
    assert len(capture_calls) == captures_after_route
    panel.deleteLater()


def test_hidden_markup_can_still_be_cleared_when_guides_exist(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)

    panel.set_markup_state(visible=False, guide_count=2)

    assert panel.tool_controls._guide_clear_button.isEnabled()
    assert not panel.tool_controls._route_array_create_button.isEnabled()
    panel.deleteLater()


def test_route_table_selection_and_shared_selection_are_bidirectional(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    route = _route(point_count=2)
    _set_document_available(panel)
    panel.set_route(route, selected_route_point_index=0)
    entities = [
        SelectableDesignEntity(
            route_entity_id(point.id),
            EntityOwner.ROUTE,
            point.id,
            PointGeometry(point.camera_center),
            route_index=index,
        )
        for index, point in enumerate(route.points)
    ]
    panel.set_selectable_entities(entities)
    panel.set_selection(
        SelectionModel(frozenset(entity.id for entity in entities))
    )

    assert panel.route_controls.selected_rows() == [0, 1]
    requests: list[tuple[set[str], str]] = []
    panel.selection_requested.connect(
        lambda ids, mode: requests.append((set(ids), mode))
    )
    panel.route_controls.route_table.clearSelection()
    selection_model = panel.route_controls.route_table.selectionModel()
    selection_model.select(
        panel.route_controls.route_table.model().index(1, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )

    assert requests[-1] == ({route_entity_id("p002")}, "replace")
    panel.deleteLater()


def test_route_table_shift_add_and_ctrl_invert_preserve_guide_selection(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    route = _route(point_count=2)
    _set_document_available(panel)
    panel.set_route(route, selected_route_point_index=0)
    entities = [
        SelectableDesignEntity(
            route_entity_id(point.id),
            EntityOwner.ROUTE,
            point.id,
            PointGeometry(point.camera_center),
            route_index=index,
        )
        for index, point in enumerate(route.points)
    ]
    guide_id = markup_entity_id("guide")
    entities.append(
        SelectableDesignEntity(
            guide_id,
            EntityOwner.MARKUP,
            "guide",
            SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
        )
    )
    panel.set_selectable_entities(entities)
    panel.set_selection(
        SelectionModel(frozenset({guide_id, route_entity_id("p001")}))
    )
    requests: list[tuple[set[str], str]] = []
    panel.selection_requested.connect(
        lambda ids, mode: requests.append((set(ids), mode))
    )
    selection_model = panel.route_controls.route_table.selectionModel()

    panel.route_controls.selection_modifiers = lambda: Qt.ShiftModifier
    selection_model.select(
        panel.route_controls.route_table.model().index(1, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )
    assert requests[-1] == (
        {route_entity_id("p001"), route_entity_id("p002")},
        "add",
    )

    panel.set_selection(
        SelectionModel(
            frozenset(
                {
                    guide_id,
                    route_entity_id("p001"),
                    route_entity_id("p002"),
                }
            )
        )
    )
    panel.route_controls.selection_modifiers = lambda: Qt.ControlModifier
    selection_model.select(
        panel.route_controls.route_table.model().index(0, 0),
        QItemSelectionModel.Deselect | QItemSelectionModel.Rows,
    )
    assert requests[-1] == ({route_entity_id("p001")}, "invert")
    panel.deleteLater()


def test_select_and_guide_actions_emit_laconic_commands(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.tool_controls._guide_undo_available = True
    panel.set_selectable_entities(
        [
            SelectableDesignEntity(
                route_entity_id("p001"),
                EntityOwner.ROUTE,
                "p001",
                PointGeometry((0.0, 0.0)),
                route_index=0,
            ),
            SelectableDesignEntity(
                markup_entity_id("g1"),
                EntityOwner.MARKUP,
                "g1",
                SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
            ),
        ]
    )
    panel.set_selection(SelectionModel(frozenset({"route:p001"})))
    panel.set_markup_state(visible=True, guide_count=1)
    panel._update_enabled_state()
    commands: list[str] = []
    panel.delete_selection_requested.connect(lambda: commands.append("delete"))
    panel.guide_undo_requested.connect(lambda: commands.append("undo"))
    panel.guide_clear_requested.connect(lambda: commands.append("clear"))

    panel.tool_controls._selection_delete_button.click()
    panel.tool_controls._guide_undo_button.click()
    panel.tool_controls._guide_clear_button.click()

    assert commands == ["delete", "undo", "clear"]
    panel.deleteLater()


def test_design_navigator_running_route_controls_preserve_confirmation_states(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_route(_route(path="route.json"), selected_route_point_index=0)
    confirmations: list[str] = []
    panel.route_measurement_confirmation_requested.connect(confirmations.append)

    panel.set_route_measurement_running(True)

    assert not panel.route_controls.new_button.isEnabled()
    assert not panel.route_controls.open_button.isEnabled()
    assert not panel.route_controls.save_button.isEnabled()
    assert not panel.route_controls.save_as_button.isEnabled()
    assert panel.route_controls.route_table.isEnabled()
    assert panel.route_run_controls.measure_button.isEnabled()
    assert panel.route_run_controls.stop_button.isEnabled()
    assert panel.route_run_controls.pause_button.text() == "Pause"
    assert panel.route_run_controls.pause_button.isEnabled()
    assert not panel.route_run_controls.move_selected_button.isEnabled()

    panel.set_route_measurement_waiting(True, "contact_confirmation")

    assert panel.route_run_controls.pause_button.text() == "Resume"
    assert panel.route_run_controls.pause_button.isEnabled()
    assert panel.route_run_controls.save_shift_button.isEnabled()
    assert panel.route_run_controls.remeasure_button.isEnabled()
    assert panel.route_run_controls.skip_button.isEnabled()
    assert panel.route_run_controls.next_button.isEnabled()
    assert panel.route_run_controls.move_selected_button.isEnabled()
    assert panel.route_run_controls.jump_selected_button.isEnabled()
    panel.route_run_controls.measure_button.click()
    assert confirmations == ["measure"]

    panel.set_route_measurement_waiting(True, "external_measurement")

    assert panel.route_run_controls.pause_button.text() == "Interrupt"
    assert panel.route_run_controls.pause_button.isEnabled()
    assert not panel.route_run_controls.save_shift_button.isEnabled()
    assert not panel.route_run_controls.remeasure_button.isEnabled()
    assert not panel.route_run_controls.skip_button.isEnabled()
    assert not panel.route_run_controls.next_button.isEnabled()
    assert not panel.route_run_controls.move_selected_button.isEnabled()
    assert not panel.route_run_controls.jump_selected_button.isEnabled()

    panel.deleteLater()


def test_route_pause_button_preserves_pause_interrupt_resume_flow(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    calls: list[object] = []
    panel.route_measurement_pause_requested.connect(lambda: calls.append("pause"))
    panel.route_measurement_interrupt_requested.connect(lambda: calls.append("interrupt"))
    panel.route_measurement_confirmation_requested.connect(calls.append)

    panel.set_route_measurement_running(True)
    panel.route_run_controls.pause_button.click()

    assert calls == ["pause"]
    assert panel.route_run_controls.pause_button.text() == "Interrupt"
    assert panel.route_run_controls.pause_button.isEnabled()

    panel.route_run_controls.pause_button.click()

    assert calls == ["pause", "interrupt"]
    assert panel.route_run_controls.pause_button.text() == "Interrupt"
    assert not panel.route_run_controls.pause_button.isEnabled()

    panel.set_route_measurement_interrupt_request_pending(False)
    panel.set_route_measurement_pause_request_pending(False)
    panel.set_route_measurement_waiting(True, "paused")
    panel.route_run_controls.pause_button.click()

    assert calls == ["pause", "interrupt", "next"]

    panel.deleteLater()
