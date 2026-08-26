from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "main.py"
SESSION = "probe_station_gui.application.stage_motion_session"
LEGACY = (
    "probe_station_gui.stage.move_lifecycle",
    "probe_station_gui.stage.position_update",
    "probe_station_gui.application.motion_prediction",
)
RAW_FIELDS = (
    "_pending_alignment_preparation",
    "_pending_quick_alignment_rotation",
    "_pending_planned_move_target_xy",
    "_pending_planned_move_source_label",
    "_planned_move_start_xy",
    "_planned_move_started_at",
    "_predicted_stage_position",
    "_last_stage_position_update",
)
FORBIDDEN = (
    "main",
    "probe_station_gui.camera",
    "probe_station_gui.dialogs",
    "probe_station_gui.route",
    "probe_station_gui.views",
    "probe_station_gui.application.alignment",
    "probe_station_gui.application.api_stage_contact",
    "probe_station_gui.application.camera_pipeline",
    "probe_station_gui.application.manual_jog",
    "probe_station_gui.application.registration_focus",
    "probe_station_gui.application.scan_sample_meter",
    "probe_station_gui.application.stage_design_position",
    "probe_station_gui.application.status_coordinate_ui",
)


@lru_cache(maxsize=None)
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8-sig"))


def _production_paths() -> list[Path]:
    return [MAIN, *(ROOT / "probe_station_gui").rglob("*.py")]


def _main_class() -> ast.ClassDef:
    return next(
        node
        for node in _tree(MAIN).body
        if isinstance(node, ast.ClassDef) and node.name == "Main"
    )


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _legacy_imports(path: Path) -> list[int]:
    offenders: list[int] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [
                node.module or "",
                *(f"{node.module}.{alias.name}" for alias in node.names),
            ]
        else:
            continue
        if any(
            name == legacy or name.startswith(f"{legacy}.")
            for name in names
            for legacy in LEGACY
        ):
            offenders.append(node.lineno)
    return offenders


@pytest.mark.parametrize("module_name", LEGACY)
def test_shallow_owner_module_is_deleted(module_name: str) -> None:
    assert not ROOT.joinpath(*module_name.split(".")).with_suffix(".py").exists()


def test_main_direct_base_order_excludes_motion_prediction_mixin() -> None:
    bases = [_dotted(base) for base in _main_class().bases]
    assert "_MainMotionPredictionMixin" not in bases


def test_main_init_assigns_exactly_one_stage_motion_session() -> None:
    init = next(
        node
        for node in _main_class().body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assignments = [
        node
        for node in ast.walk(init)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == "_stage_motion"
            for target in (
                [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            )
        )
    ]
    assert len(assignments) == 1


@pytest.mark.parametrize("field", RAW_FIELDS)
def test_migrated_raw_field_is_absent_from_production(field: str) -> None:
    offenders = []
    for path in _production_paths():
        if any(
            isinstance(node, ast.Attribute) and node.attr == field
            for node in ast.walk(_tree(path))
        ):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_no_production_module_imports_a_legacy_owner() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): lines
        for path in _production_paths()
        if (lines := _legacy_imports(path))
    }
    assert offenders == {}


def test_no_dynamic_facade_property_reexport_or_owner_self() -> None:
    offenders: list[str] = []
    legacy_names = {legacy.rsplit(".", 1)[-1] for legacy in LEGACY}
    for path in _production_paths():
        tree = _tree(path)
        for node in ast.walk(tree):
            is_facade = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                any(
                    isinstance(item, ast.Name) and item.id == "property"
                    for item in node.decorator_list
                )
                and node.name in RAW_FIELDS
            )
            owner_self = (
                isinstance(node, ast.Call)
                and any(
                    keyword.arg == "owner"
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id == "self"
                    for keyword in node.keywords
                )
                and any(name in (_dotted(node.func) or "") for name in legacy_names)
            )
            value = (
                node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
            )
            targets = (
                [node.target]
                if isinstance(node, ast.AnnAssign)
                else getattr(node, "targets", [])
            )
            reexport = (
                value is not None
                and _dotted(value) in legacy_names
                and any(_dotted(target) in legacy_names for target in targets)
            )
            if is_facade or owner_self or reexport:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert offenders == []


def test_controller_completion_has_one_direct_session_consumer() -> None:
    source = MAIN.read_text(encoding="utf-8-sig")
    assert source.count("self.stage_controller.movement_finished.connect(") == 1
    assert "self._stage_motion.on_movement_finished" in source
    registration = (
        ROOT / "probe_station_gui/application/registration_focus.py"
    ).read_text(encoding="utf-8-sig")
    assert ".on_move_finished(" not in registration


def test_operation_identity_is_not_inferred_from_status_text_outside_normalizer() -> (
    None
):
    allowed = {
        "probe_station_gui/application/stage_motion_session.py",
        "probe_station_gui/stage/coordinate_targets.py",
    }
    offenders: list[str] = []
    for path in _production_paths():
        if path.relative_to(ROOT).as_posix() in allowed:
            continue
        source = path.read_text(encoding="utf-8-sig")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Compare):
                identifiers = {
                    item.id.lower()
                    for item in ast.walk(node)
                    if isinstance(item, ast.Name)
                } | {
                    item.attr.lower()
                    for item in ast.walk(node)
                    if isinstance(item, ast.Attribute)
                }
                text = " ".join(
                    item.value.lower()
                    for item in ast.walk(node)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
                if identifiers & {"message", "status"} and any(
                    word in text for word in ("move", "align", "click")
                ):
                    offenders.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                    )
    assert offenders == []


def test_session_imports_no_forbidden_callers() -> None:
    source = (ROOT / "probe_station_gui/application/stage_motion_session.py").read_text(
        encoding="utf-8-sig"
    )
    assert [
        name
        for name in FORBIDDEN
        if f"import {name}" in source or f"from {name}" in source
    ] == []


def test_sixth_import_contract_is_exact_and_unweakened() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8-sig"))
    matching = [
        item
        for item in config["tool"]["importlinter"]["contracts"]
        if item.get("id") == "stage-motion-session-leaf"
    ]
    assert matching == [
        {
            "id": "stage-motion-session-leaf",
            "name": "Stage motion session stays independent of callers and unrelated workflows",
            "type": "forbidden",
            "source_modules": [SESSION],
            "forbidden_modules": list(FORBIDDEN),
        }
    ]
