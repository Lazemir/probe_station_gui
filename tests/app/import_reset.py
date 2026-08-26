"""Import-state helpers for tests that import the main window module."""

from __future__ import annotations

import sys


def _delete_loaded_modules(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(f"{prefix}."):
            del sys.modules[name]


def _real_pyside_package_is_loaded() -> bool:
    package = sys.modules.get("PySide6")
    return bool(
        package is not None
        and getattr(package, "__file__", None)
        and hasattr(package, "__path__")
    )


def restore_real_imports_for_main(*, clear_probe_station_gui: bool = False) -> None:
    if not _real_pyside_package_is_loaded():
        _delete_loaded_modules("PySide6")
    serial_module = sys.modules.get("serial")
    if serial_module is not None and not hasattr(serial_module, "__path__"):
        _delete_loaded_modules("serial")
    package = sys.modules.get("probe_station_gui")
    if clear_probe_station_gui or (
        package is not None and not hasattr(package, "__path__")
    ):
        _delete_loaded_modules("main")
        _delete_loaded_modules("probe_station_gui")
