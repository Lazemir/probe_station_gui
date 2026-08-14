from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "main.py"
SLOT = ROOT / "probe_station_gui" / "application" / "route_run_execution.py"
PRODUCTION_CONSUMERS = (
    MAIN,
    ROOT / "probe_station_gui" / "application" / "route_launch_setup.py",
    ROOT / "probe_station_gui" / "application" / "route_capture_run.py",
    ROOT / "probe_station_gui" / "application" / "route_control.py",
    ROOT / "probe_station_gui" / "application" / "route_results.py",
    ROOT / "probe_station_gui" / "application" / "api_stage_contact.py",
    ROOT / "probe_station_gui" / "application" / "api_route_scan.py",
    ROOT / "probe_station_gui" / "application" / "api_meter_visa.py",
    ROOT / "probe_station_gui" / "application" / "bootstrap_api.py",
    ROOT / "probe_station_gui" / "application" / "design_edit_dialog.py",
    ROOT / "probe_station_gui" / "application" / "design_markup.py",
    ROOT / "probe_station_gui" / "application" / "registration_focus.py",
    ROOT / "probe_station_gui" / "route" / "dialog_adapter.py",
    ROOT / "probe_station_gui" / "stage" / "move_lifecycle.py",
    ROOT / "probe_station_gui" / "views" / "main_window_shutdown.py",
    ROOT / "probe_station_gui" / "views" / "main_window_needle_calibration.py",
)
RAW_EXECUTION_FIELDS = (
    "_route_measurement_runner",
    "_route_measurement_thread",
    "_route_measurement_waiting",
    "_route_measurement_waiting_reason",
)
OBSOLETE_HELPERS = (
    "_current_route_measurement_waiting_reason",
    "_join_finished_route_measurement_thread",
    "_clear_waiting_route_measurement_state",
    "restart_waiting_route_measurement",
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _main_class() -> ast.ClassDef:
    module = ast.parse(_source(MAIN))
    return next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "Main"
    )


def test_main_composes_one_canonical_route_run_execution_slot() -> None:
    main_class = _main_class()
    init = next(
        node
        for node in main_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__init__"
    )
    assignments = [
        node
        for node in ast.walk(init)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == "_route_run_execution"
            for target in (
                [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            )
        )
    ]

    assert len(assignments) == 1
    value = assignments[0].value
    assert isinstance(value, ast.Call)
    assert isinstance(value.func, ast.Name)
    assert value.func.id == "_RouteRunExecutionSlot"


def test_raw_execution_fields_and_obsolete_helpers_are_deleted_from_production() -> (
    None
):
    findings: set[tuple[str, str]] = set()
    for path in PRODUCTION_CONSUMERS:
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(_source(path))):
            if isinstance(node, ast.Attribute) and node.attr in RAW_EXECUTION_FIELDS:
                findings.add((relative, node.attr))
            if isinstance(node, ast.Name) and node.id in OBSOLETE_HELPERS:
                findings.add((relative, node.id))
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in OBSOLETE_HELPERS
            ):
                findings.add((relative, node.name))

    assert findings == set()


def test_main_has_no_route_execution_compatibility_surface() -> None:
    main_class = _main_class()
    forbidden_names = set(RAW_EXECUTION_FIELDS) | set(OBSOLETE_HELPERS)
    definitions = {
        node.name
        for node in main_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assigned_names = {
        target.attr
        for node in ast.walk(main_class)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        )
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }

    assert definitions.isdisjoint(forbidden_names)
    assert assigned_names.isdisjoint(RAW_EXECUTION_FIELDS)
    assert "__getattr__" not in definitions


def test_run_kind_is_explicit_without_capability_inference() -> None:
    consumer_source = "\n".join(_source(path) for path in PRODUCTION_CONSUMERS)

    assert "kind=RouteRunKind.GUI" in consumer_source
    assert "kind=RouteRunKind.EXTERNAL_RESULT_SESSION" in consumer_source
    assert "isinstance(" not in "\n".join(
        line
        for line in consumer_source.splitlines()
        if "route_measurement" in line or "route_run" in line
    )
    assert 'hasattr(runner, "submit_external_result")' not in consumer_source


def test_slot_has_no_reverse_import_or_package_reexport() -> None:
    slot_tree = ast.parse(_source(SLOT))
    imported = {
        alias.name
        for node in ast.walk(slot_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(slot_tree)
        if isinstance(node, ast.ImportFrom)
    }
    package_init = _source(ROOT / "probe_station_gui" / "application" / "__init__.py")

    assert not any(
        name == "main"
        or name.startswith("probe_station_gui.route")
        or name.startswith("probe_station_gui.dialogs")
        or name.startswith("probe_station_gui.views")
        for name in imported
    )
    assert "route_run_execution" not in package_init
    assert "_RouteRunExecutionSlot" not in package_init


def test_leaf_import_linter_contract_remains_exact() -> None:
    pyproject = _source(ROOT / "pyproject.toml")

    assert 'id = "route-run-execution-slot-leaf"' in pyproject
    assert (
        'source_modules = ["probe_station_gui.application.route_run_execution"]'
        in pyproject
    )
    assert "ignore_imports" not in pyproject
