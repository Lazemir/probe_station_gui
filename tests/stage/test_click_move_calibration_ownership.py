from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import inspect
from pathlib import Path
import subprocess
import sys


_MOVED_METHOD_HASHES = {
    "_ensure_calibration": "bf97542ec7d11490e186a0326c9135edf971ed6db33020124a6a85d07de906e2",
    "_verify_active_objective_calibration": "f0f3be133b83d056052b9b4edcbd9559a7727d4e1ea580c64d6c8aa039a49c7c",
    "_best_objective_for_measurement": "1f0ede5564d2735170aa3b41c031f00a348adaf84a11867673c4a70e0dcaae8a",
    "_calibrate_axis_series": "76b7e8fe749b72eb4eb868f62c154bce2099dad03b8b139d94e749d8ca63f372",
    "_next_calibration_probe_step_mm": "fac33afd9b8d0756f752e6fdeb6b56fe4d4c6895725a378580c63520ada39824",
    "_calibration_verify_step_mm": "2d222c5ca11d36504733c968df1101380b33d63002a2dee84fc12dc838b9b2c9",
    "_calibration_matrix_from_observations": "8714b6eb74615484e2ff9d5d8d89fef2a5f491cd34b3b830b2f659349ceda670",
    "_axis_slope_calibration_matrix": "95146ba06b063045ef4604a628e96f22e73ac8b4f681666344e45bae7e499c2d",
    "_move_from_calibration_to_target": "3b74779d84fb013901805531581fe09ee8b7f1b62b4628bd6e166ebb8b2482e5",
    "_return_to_origin": "07c0f50304bd303213e6a74d5fccf6aa2e8f3113f7c4c45beb92b3d0c9f1f847",
    "_calibration_magnitudes": "d351aff7a374764a8605a9b8df426781f22f7f433befb5ecb2fb2d68c913b05d",
    "_estimate_shift": "4e780302ce4512fbab9e9c70232d4bb5ef3fc581b8ede72706a9073018893786",
    "_estimate_shift_with_response": "c4245476644d3b1a917facb00fca52c5fcd778031687b8d24fd8971ab3d3ad93",
}


def _class_methods(module_path: str, class_name: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)}


def _ast_hash(node: ast.AST) -> str:
    payload = ast.dump(node, include_attributes=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _method_manifest(methods: dict[str, ast.FunctionDef]) -> str:
    payload = "\n".join(
        ast.dump(node, include_attributes=False) for node in methods.values()
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def test_click_move_calibration_module_exists() -> None:
    assert (
        importlib.util.find_spec("probe_station_gui.stage.click_move_calibration")
        is not None
    )


def test_click_move_host_inherits_the_private_calibration_owner() -> None:
    owner_module = importlib.import_module(
        "probe_station_gui.stage.click_move_calibration"
    )
    host_module = importlib.import_module("probe_station_gui.stage.click_move")
    owner = getattr(owner_module, "_StageControllerClickCalibrationMixin")

    assert host_module.StageControllerClickMoveMixin.__bases__ == (owner,)


def test_calibration_methods_move_exactly_to_the_private_owner() -> None:
    from probe_station_gui.stage.click_move import StageControllerClickMoveMixin
    from probe_station_gui.stage.click_move_calibration import (
        _StageControllerClickCalibrationMixin,
    )
    from probe_station_gui.stage.controller import StageController

    owner_methods = _class_methods(
        "probe_station_gui/stage/click_move_calibration.py",
        "_StageControllerClickCalibrationMixin",
    )
    host_methods = _class_methods(
        "probe_station_gui/stage/click_move.py",
        "StageControllerClickMoveMixin",
    )

    assert set(owner_methods) == set(_MOVED_METHOD_HASHES)
    assert set(host_methods).isdisjoint(_MOVED_METHOD_HASHES)
    assert {
        name: _ast_hash(owner_methods[name]) for name in _MOVED_METHOD_HASHES
    } == _MOVED_METHOD_HASHES
    for name in _MOVED_METHOD_HASHES:
        descriptor = inspect.getattr_static(_StageControllerClickCalibrationMixin, name)
        assert inspect.getattr_static(StageControllerClickMoveMixin, name) is descriptor
        assert inspect.getattr_static(StageController, name) is descriptor


def test_residual_imports_and_retained_methods_are_exact() -> None:
    source = Path("probe_station_gui/stage/click_move.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    autofocus_names = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "probe_station_gui.stage.autofocus_math"
        for alias in node.names
    }
    retained_methods = _class_methods(
        "probe_station_gui/stage/click_move.py",
        "StageControllerClickMoveMixin",
    )

    assert imported_modules.isdisjoint({"logging", "math"})
    assert autofocus_names == {"focus_metric", "qimage_to_gray"}
    assert len(retained_methods) == 30
    assert (
        _method_manifest(retained_methods)
        == "0e6a5a3021c91ea9ec8c25d5449eef18c54ac3b242240315f53878d4eaaadd20"
    )


def test_calibration_owner_has_one_way_imports_and_preserves_logger() -> None:
    owner_module = importlib.import_module(
        "probe_station_gui.stage.click_move_calibration"
    )
    source = Path("probe_station_gui/stage/click_move_calibration.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)}
    forbidden_prefixes = (
        "probe_station_gui.stage.controller",
        "probe_station_gui.stage.click_move",
        "PySide6",
        "probe_station_gui.views",
        "probe_station_gui.route",
        "probe_station_gui.serial",
        "main",
    )

    assert not any(
        imported.startswith(prefix)
        for imported in imported_modules
        for prefix in forbidden_prefixes
    )
    assert owner_module.logger.name == "probe_station_gui.stage.click_move"
    assert "__all__" not in owner_module.__dict__

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import probe_station_gui.stage.click_move_calibration; "
                "blocked={'PySide6','rotpy','klayout'}; "
                "raise SystemExit(bool(blocked.intersection(sys.modules)))"
            ),
        ],
        check=False,
    )
    assert probe.returncode == 0
