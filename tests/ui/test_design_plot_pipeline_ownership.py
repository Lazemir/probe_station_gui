from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_plot_pipeline_modules_exist_at_owned_seams() -> None:
    expected = (
        ROOT / "probe_station_gui" / "design" / "plot_interaction.py",
        ROOT / "probe_station_gui" / "design" / "snap_coordinator.py",
        ROOT / "probe_station_gui" / "design" / "snap_protocol.py",
        ROOT / "probe_station_gui" / "views" / "design_snap_runtime.py",
        ROOT / "probe_station_gui" / "design" / "plot_presentation.py",
        ROOT / "probe_station_gui" / "views" / "design_plot_viewport.py",
        ROOT / "probe_station_gui" / "views" / "design_plot_route_rendering.py",
        ROOT / "probe_station_gui" / "views" / "design_plot_rendering.py",
        ROOT / "probe_station_gui" / "design" / "klayout_render_worker.py",
        ROOT / "probe_station_gui" / "design" / "klayout_snap_worker.py",
        ROOT
        / "probe_station_gui"
        / "design"
        / "klayout_structure_bounds_worker.py",
        ROOT / "probe_station_gui" / "design" / "klayout_worker_runtime.py",
    )
    replaced_worker_owner = (
        ROOT / "probe_station_gui" / "design" / ("klayout_" + "workers.py")
    )

    assert all(path.is_file() for path in expected)
    assert not replaced_worker_owner.exists()
    assert all(
        "probe_station_gui.design." + "klayout_" + "workers"
        not in path.read_text(encoding="utf-8")
        for path in expected
    )


def test_plot_pane_has_no_legacy_snap_queue_or_worker_ownership() -> None:
    path = ROOT / "probe_station_gui" / "views" / "design_plot_pane.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_names = {
        "_snap_enabled",
        "_klayout_config",
        "_snap_request_id",
        "_latest_hover_request_id",
        "_pending_hover_markup",
        "_pending_clicks",
        "_click_order",
        "_click_completions",
        "_snap_worker",
        "_snap_worker_source_key",
        "_retired_snap_workers",
        "_snap_retirement_callbacks",
        "_markup_generation",
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    forbidden_imports = {
        "deque",
        "Callable",
        "KLayoutSnapWorker",
        "PendingClick",
        "SnapRequest",
        "SnapResponse",
        "SnapFailure",
        "plan_snap_search",
    }

    assert not forbidden_names.intersection(names | attributes)
    assert not forbidden_imports.intersection(names)


def test_plot_pane_has_no_legacy_click_action_facades() -> None:
    path = ROOT / "probe_station_gui" / "views" / "design_plot_pane.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_methods = {
        "_submit_file_backed_click",
        "_execute_click_action",
        "_accept_guide_point",
    }
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert not forbidden_methods.intersection(methods)


def test_pure_plot_modules_do_not_import_qt_views_route_or_main() -> None:
    forbidden = (
        "PySide6",
        "probe_station_gui.views",
        "probe_station_gui.route",
        "main",
    )
    for relative in (
        Path("probe_station_gui/design/plot_interaction.py"),
        Path("probe_station_gui/design/snap_coordinator.py"),
        Path("probe_station_gui/design/snap_protocol.py"),
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert all(token not in source for token in forbidden)


def test_plot_presentation_is_qt_free() -> None:
    source = (
        ROOT / "probe_station_gui" / "design" / "plot_presentation.py"
    ).read_text(encoding="utf-8")

    assert "PySide6" not in source
    assert "pyqtgraph" not in source
    assert "probe_station_gui.views" not in source
    assert "main" not in source


def test_plot_pane_retains_only_raster_scene_item_ownership() -> None:
    path = ROOT / "probe_station_gui" / "views" / "design_plot_pane.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    self_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    forbidden_attributes = {
        "_navigation_content_bounds",
        "_navigation_frame",
        "_navigation_ignored_coordinate_count",
        "_last_navigation_invalid_coordinate_count",
        "_viewport_size",
        "_document",
        "_targets",
        "_selected_target_id",
        "_probe_route",
        "_selected_route_point_index",
        "_probe_route_preview_points",
        "_probe_route_preview_offsets",
        "_tool_measure_points",
        "_tool_measure_segments",
        "_tool_sketch_points",
        "_tool_sketch_segments",
        "_alignment_draft_points",
        "_selectable_entities",
        "_selection",
        "_mixed_preview_points",
        "_mixed_preview_segments",
        "_source_design_marks",
        "_current_design_position",
        "_fov_design_size",
        "_check_design_marks",
        "_focus_candidate",
        "_selected_focus_point",
        "_hover_snap",
        "_layer_items",
        "_route_number_items",
        "_tool_measure_label_items",
        "_alignment_draft_label_items",
        "_route_render_timer",
        "_route_render_deferred",
    }
    forbidden_methods = {
        "_current_view_bounds",
        "_set_view_bounds",
        "_clear_navigation_limits",
        "_update_navigation_limits",
        "_render_selection_preview",
        "_schedule_route_geometry_redraw",
        "_clear_route_arrows",
    }
    owned_items = {name for name in self_attributes if name.endswith("_item")}

    assert owned_items == {"_raster_item"}
    assert not forbidden_attributes.intersection(self_attributes)
    assert not forbidden_methods.intersection(methods)
    assert not any(name.startswith("_redraw_") for name in methods)
    assert all(
        token not in source
        for token in ("pg.mkPen", "pg.mkBrush", "pg.TextItem", "pg.ScatterPlotItem", "removeItem")
    )


def test_deleted_plot_pane_facades_are_not_reintroduced() -> None:
    path = ROOT / "probe_station_gui" / "views" / "design_plot_pane.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert not {
        "focus_bounds",
        "set_source_design_marks",
        "set_tool_sketch_points",
        "set_tool_sketch_segments",
        "_first_segment_length",
        "_route_arrow_tip_fractions",
    }.intersection(methods)
