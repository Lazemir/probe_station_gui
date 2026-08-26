from __future__ import annotations

import importlib
import sys
import types

from tests.app.import_reset import restore_real_imports_for_main


def _loaded_pyside_modules() -> dict[str, object]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "PySide6" or name.startswith("PySide6.")
    }


def _restore_pyside_modules(original_modules: dict[str, object]) -> None:
    for name in tuple(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            del sys.modules[name]
    sys.modules.update(original_modules)


def test_restore_real_imports_preserves_loaded_real_pyside_modules() -> None:
    original_modules = _loaded_pyside_modules()
    try:
        current_package = importlib.import_module("PySide6")
        current_qtcore = importlib.import_module("PySide6.QtCore")
        assert getattr(current_package, "__file__", None)
        assert hasattr(current_package, "__path__")

        restore_real_imports_for_main()

        assert sys.modules.get("PySide6") is current_package
        assert sys.modules.get("PySide6.QtCore") is current_qtcore
        assert importlib.import_module("PySide6.QtCore") is current_qtcore
    finally:
        _restore_pyside_modules(original_modules)


def test_restore_real_imports_clears_pyside_stubs() -> None:
    original_modules = _loaded_pyside_modules()
    try:
        _restore_pyside_modules({})
        sys.modules["PySide6"] = types.ModuleType("PySide6")
        sys.modules["PySide6.QtCore"] = types.ModuleType("PySide6.QtCore")

        restore_real_imports_for_main()

        assert "PySide6" not in sys.modules
        assert "PySide6.QtCore" not in sys.modules
    finally:
        _restore_pyside_modules(original_modules)
