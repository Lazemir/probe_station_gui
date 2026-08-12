from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN_PATH = ROOT / "main.py"
RUNTIME_PATH = ROOT / "probe_station_gui" / "api" / "stage_command_runtime.py"
SHUTDOWN_PATH = ROOT / "probe_station_gui" / "views" / "main_window_shutdown.py"
API_INIT_PATH = ROOT / "probe_station_gui" / "api" / "__init__.py"

OLD_MAIN_METHODS = {
    "_api_stage_command_worker_state",
    "_api_stage_command_worker_active",
    "_wait_for_api_stage_command_workers",
    "_release_api_stage_command_reservation",
    "_start_deferred_api_stage_command",
    "_run_deferred_api_stage_command",
}
OLD_MAIN_ATTRIBUTES = {
    "_api_stage_command_worker_lock",
    "_api_stage_command_worker_changed",
    "_api_stage_command_reservation",
}


def _named_class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _named_method(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_main_uses_only_the_canonical_stage_command_runtime_interface() -> None:
    main_source = MAIN_PATH.read_text(encoding="utf-8")
    main_tree = ast.parse(main_source)
    main_class = _named_class(main_tree, "Main")
    main_methods = {
        node.name for node in main_class.body if isinstance(node, ast.FunctionDef)
    }
    top_level_classes = {
        node.name for node in main_tree.body if isinstance(node, ast.ClassDef)
    }

    assert OLD_MAIN_METHODS.isdisjoint(main_methods)
    assert "_ApiStageCommandReservation" not in top_level_classes
    assert not {
        node.attr for node in ast.walk(main_class) if isinstance(node, ast.Attribute)
    }.intersection(OLD_MAIN_ATTRIBUTES)

    init_source = ast.unparse(_named_method(main_class, "__init__"))
    submit_source = ast.unparse(
        _named_method(main_class, "_submit_api_command_request")
    )
    busy_source = ast.unparse(_named_method(main_class, "_objective_mutation_busy"))
    assert "ApiStageCommandRuntime(" in init_source
    assert "self._dispatch_api_command_request" in init_source
    assert "apply_route_control_guard=False" in init_source
    assert (
        "self._api_stage_command_runtime.submit(command_request, action)"
        in submit_source
    )
    assert "self._api_stage_command_runtime.active()" in busy_source

    runtime_source = RUNTIME_PATH.read_text(encoding="utf-8")
    runtime_tree = ast.parse(runtime_source)
    runtime_class = _named_class(runtime_tree, "ApiStageCommandRuntime")
    public_methods = {
        node.name
        for node in runtime_class.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    public_aliases = {
        target.id
        for node in runtime_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and not target.id.startswith("_")
    }
    imported_modules = {
        alias.name
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert public_methods == {"submit", "active", "wait_until_idle"}
    assert public_aliases == set()
    assert "__getattr__" not in runtime_source
    assert "__all__" not in runtime_source
    assert "main" not in imported_modules
    assert "probe_station_gui.main" not in imported_modules
    assert "ApiStageCommandRuntime" not in API_INIT_PATH.read_text(encoding="utf-8")

    shutdown_source = SHUTDOWN_PATH.read_text(encoding="utf-8")
    shutdown_tree = ast.parse(shutdown_source)
    protocol = _named_class(shutdown_tree, "MainWindowShutdownOwner")
    protocol_source = ast.unparse(protocol)
    stop_source = ast.unparse(
        next(
            node
            for node in shutdown_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_stop_api_stage_command_workers"
        )
    )
    assert "_api_stage_command_runtime: Any" in protocol_source
    assert (
        "owner._api_stage_command_runtime.wait_until_idle(timeout_s=0.0)" in stop_source
    )
    assert "getattr" not in stop_source
