from __future__ import annotations

import ast
from pathlib import Path

import probe_station_gui.dialogs.settings.api_access as api_access_module
import probe_station_gui.dialogs.settings.telegram as telegram_module
import probe_station_gui.dialogs.settings_dialog as settings_dialog_module


EXPECTED_PUBLIC = [
    "ControlsSettingsWidget",
    "CoordinateSystemSettingsWidget",
    "FeedrateGroupEditor",
    "FeedrateSettingsWidget",
    "JogSettingsWidget",
    "KeyBindingListEditor",
    "KeyCaptureDialog",
    "MeasurementSettingsWidget",
    "ObjectivesSettingsWidget",
    "SettingsDialog",
]


def _top_level_classes(module) -> set[str]:
    source_path = Path(module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _imported_modules(module) -> set[str]:
    source_path = Path(module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"))
    imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_settings_dialog_keeps_only_residual_class_owners() -> None:
    assert _top_level_classes(settings_dialog_module) == {
        "LoggingSettingsWidget",
        "NeedleSettingsWidget",
        "SettingsDialog",
    }
    assert _top_level_classes(api_access_module) == {"ApiSettingsWidget"}
    assert _top_level_classes(telegram_module) == {
        "TelegramLinkWorker",
        "TelegramSettingsWidget",
    }


def test_settings_dialog_public_surface_and_owner_dag_are_exact() -> None:
    assert settings_dialog_module.__all__ == EXPECTED_PUBLIC
    assert settings_dialog_module.SettingsDialog.__module__ == (
        "probe_station_gui.dialogs.settings_dialog"
    )
    assert api_access_module.__all__ == ["ApiSettingsWidget"]
    assert telegram_module.__all__ == ["TelegramSettingsWidget"]
    assert not hasattr(settings_dialog_module, "ApiSettingsWidget")
    assert not hasattr(settings_dialog_module, "TelegramSettingsWidget")
    assert not hasattr(settings_dialog_module, "TelegramLinkWorker")
    assert "probe_station_gui.dialogs.settings_dialog" not in _imported_modules(
        api_access_module
    )
    assert "probe_station_gui.dialogs.settings_dialog" not in _imported_modules(
        telegram_module
    )
