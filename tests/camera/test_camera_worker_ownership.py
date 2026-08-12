from __future__ import annotations

import ast
import inspect
from pathlib import Path

import probe_station_gui
from probe_station_gui.camera import worker as worker_module
from probe_station_gui.camera.genicam_nodes import GenICamNodes
from probe_station_gui.camera.settings_transactions import CameraSettingsTransactions
from probe_station_gui.camera.worker import Grabber


MOVED_METHODS = {
    "_camera_settings_snapshot",
    "_camera_settings_partial_snapshot",
    "_node_map_title",
    "_node_map",
    "_node_by_name",
    "_read_node_map",
    "_read_node_info",
    "_node_type",
    "_enum_entries",
    "_category_children",
    "_apply_camera_setting",
    "_apply_camera_settings_batch",
    "_apply_temporary_camera_settings",
    "_restore_temporary_camera_settings",
    "_restore_camera_setting_values",
    "_set_camera_setting_value",
    "_execute_camera_command",
    "_set_node_value",
    "_set_node_value_from_str",
    "_execute_command_node",
    "_coerce_bool",
    "_coerce_int",
    "_safe_node_string",
    "_safe_node_bool",
    "_safe_node_value",
    "_pause_acquisition",
    "_resume_acquisition",
}


def _imports(module: object) -> set[str]:
    source_path = Path(inspect.getsourcefile(module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
            if node.module is None:
                imports.update(alias.name for alias in node.names)
    return imports


def test_worker_delegates_to_canonical_owners_without_private_aliases() -> None:
    assert worker_module.genicam_nodes.GenICamNodes is GenICamNodes
    assert (
        worker_module.settings_transactions.CameraSettingsTransactions
        is CameraSettingsTransactions
    )
    assert MOVED_METHODS.isdisjoint(Grabber.__dict__)
    assert worker_module.__all__ == ["Grabber"]
    assert not hasattr(worker_module, "GenICamNodes")
    assert not hasattr(worker_module, "CameraSettingsTransactions")
    assert not hasattr(worker_module.settings_transactions, "GenICamNodes")


def test_camera_worker_public_identity_signatures_and_signals_are_stable() -> None:
    assert probe_station_gui.Grabber is Grabber
    assert Grabber.__module__ == "probe_station_gui.camera.worker"
    assert str(inspect.signature(Grabber)) == "() -> 'None'"
    assert str(inspect.signature(Grabber.request_camera_settings_snapshot)) == (
        "(self, map_key: 'str | None' = None, node_names: 'list[str] | None' = "
        "None, *, request_id: 'str | None' = None) -> 'None'"
    )
    assert str(inspect.signature(Grabber.request_camera_setting_update)) == (
        "(self, map_key: 'str', node_name: 'str', value: 'object', *, "
        "request_id: 'str | None' = None) -> 'None'"
    )
    assert str(inspect.signature(Grabber.request_camera_settings_batch)) == (
        "(self, settings: 'Mapping[str, object] | Iterable[tuple[str, object]]', "
        "*, request_id: 'str', map_key: 'str' = 'camera') -> 'None'"
    )
    assert str(inspect.signature(Grabber.request_camera_command_execute)) == (
        "(self, map_key: 'str', node_name: 'str') -> 'None'"
    )
    assert str(inspect.signature(Grabber.request_temporary_camera_settings)) == (
        "(self, settings: 'Mapping[str, object] | Iterable[tuple[str, object]]', "
        "*, map_key: 'str' = 'camera', restore_key: 'str' = 'default') -> 'None'"
    )
    assert str(inspect.signature(Grabber.request_restore_camera_settings)) == (
        "(self, *, restore_key: 'str' = 'default') -> 'None'"
    )
    assert str(inspect.signature(Grabber.start)) == "(self) -> 'None'"
    assert str(inspect.signature(Grabber.stop)) == "(self) -> 'None'"
    assert str(inspect.signature(Grabber.latest_frame_counter)) == "(self) -> 'int'"
    assert str(inspect.signature(Grabber.wait_for_frame)) == (
        "(self, *, after_counter: 'int | None' = None, timeout_s: 'float' = 2.0) "
        "-> 'tuple[QImage | None, int]'"
    )
    assert {
        "frame_ready",
        "frame_gap_suppressed",
        "error",
        "camera_settings_snapshot_ready",
        "camera_setting_changed",
        "camera_settings_batch_changed",
        "camera_settings_override_changed",
    } == {
        name
        for name in Grabber.__dict__
        if name
        in {
            "frame_ready",
            "frame_gap_suppressed",
            "error",
            "camera_settings_snapshot_ready",
            "camera_setting_changed",
            "camera_settings_batch_changed",
            "camera_settings_override_changed",
        }
    }


def test_new_camera_settings_owners_are_qt_free_and_acyclic() -> None:
    import probe_station_gui.camera.genicam_nodes as nodes_module
    import probe_station_gui.camera.settings_transactions as transactions_module

    nodes_imports = _imports(nodes_module)
    transaction_imports = _imports(transactions_module)
    assert not any(name.startswith(("PySide6", "rotpy")) for name in nodes_imports)
    assert not any(
        name.startswith(("PySide6", "rotpy")) for name in transaction_imports
    )
    assert not any(name.endswith("worker") for name in nodes_imports)
    assert not any(name.endswith("worker") for name in transaction_imports)
    assert "genicam_nodes" in transaction_imports
    assert "settings_transactions" not in nodes_imports
