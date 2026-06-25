from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

from setuptools import find_packages


ROOT = Path(__file__).resolve().parents[2]


PUBLIC_IMPORT_PATHS = [
    "probe_station_gui.settings_manager",
    "probe_station_gui.feedrate_config",
    "probe_station_gui.jog_config",
    "probe_station_gui.controls_config",
    "probe_station_gui.settings.manager",
    "probe_station_gui.settings.feedrate_config",
    "probe_station_gui.views.joystick_window",
    "probe_station_gui.dialogs.settings_dialog",
    "probe_station_gui.stage.joystick_feedrate_targets",
    "probe_station_gui.views.joystick.feedrate_targets",
]


def _configured_packages() -> set[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools_config = pyproject["tool"]["setuptools"]
    packages = setuptools_config["packages"]
    if isinstance(packages, list):
        return set(packages)

    find_config = packages["find"]
    where = find_config.get("where", ["."])
    include = find_config.get("include", ["*"])
    exclude = find_config.get("exclude", [])

    discovered: set[str] = set()
    for search_root in where:
        discovered.update(
            find_packages(
                where=str(ROOT / search_root),
                include=include,
                exclude=exclude,
            )
        )
    return discovered


def test_public_import_paths_remain_available() -> None:
    for module_name in PUBLIC_IMPORT_PATHS:
        importlib.import_module(module_name)

    from probe_station_gui import Grabber, JoystickWindow, StageController
    from probe_station_gui.dialogs.settings_dialog import (
        ControlsSettingsWidget,
        FeedrateGroupEditor,
        FeedrateSettingsWidget,
        JogSettingsWidget,
        KeyBindingListEditor,
        KeyCaptureDialog,
    )

    assert Grabber.__name__ == "Grabber"
    assert JoystickWindow.__name__ == "JoystickWindow"
    assert StageController.__name__ == "StageController"
    assert ControlsSettingsWidget.__name__ == "ControlsSettingsWidget"
    assert FeedrateGroupEditor.__name__ == "FeedrateGroupEditor"
    assert FeedrateSettingsWidget.__name__ == "FeedrateSettingsWidget"
    assert JogSettingsWidget.__name__ == "JogSettingsWidget"
    assert KeyBindingListEditor.__name__ == "KeyBindingListEditor"
    assert KeyCaptureDialog.__name__ == "KeyCaptureDialog"


def test_packaging_includes_all_source_packages() -> None:
    configured_packages = _configured_packages()
    source_packages = set(
        find_packages(
            where=str(ROOT),
            include=[
                "probe_station_client*",
                "probe_station_gui*",
                "probe_station_measure*",
            ],
            exclude=["tests*"],
        )
    )

    missing_packages = sorted(source_packages - configured_packages)

    assert not missing_packages
