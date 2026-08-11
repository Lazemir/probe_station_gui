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
    )

    assert all(path.is_file() for path in expected)


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
