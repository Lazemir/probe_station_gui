import importlib.util
import platform
import sys
import types
import unittest
from pathlib import Path


def _install_pyside6_stubs() -> None:
    qtcore = types.ModuleType("PySide6.QtCore")

    class Qt:  # noqa: N801 - mimic Qt namespace
        KeyboardModifier = int
        KeyboardModifiers = int
        Key_Left = 16777234
        Key_Up = 16777235
        Key_Right = 16777236
        Key_Down = 16777237
        Key_Space = 32
        Key_Tab = 16777217
        Key_Return = 16777220
        Key_Enter = 16777221
        Key_D = 68

    qtcore.Qt = Qt

    pyside6 = types.ModuleType("PySide6")
    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore


def _install_probe_station_stubs() -> None:
    package = types.ModuleType("probe_station_gui")
    logging_config = types.ModuleType("probe_station_gui.logging_config")

    def configure_logging(*_args, **_kwargs) -> None:
        return None

    logging_config.configure_logging = configure_logging
    package.logging_config = logging_config
    sys.modules["probe_station_gui"] = package
    sys.modules["probe_station_gui.logging_config"] = logging_config


def _load_module(module_name: str, relative_path: str):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_install_pyside6_stubs()
_install_probe_station_stubs()
qt_compat = _load_module("qt_compat_test", "probe_station_gui/qt_compat.py")
settings_manager = _load_module(
    "settings_manager_test", "probe_station_gui/settings_manager.py"
)
KeyBinding = settings_manager.KeyBinding
derive_native_scan_code_from_qt_key = qt_compat.derive_native_scan_code_from_qt_key


class KeyBindingRoundTripTest(unittest.TestCase):
    def test_round_trip_preserves_native_scan_code(self) -> None:
        binding = KeyBinding(
            qt_key=68,
            modifiers=0,
            native_scan_code=32,
            text="d",
        )

        restored = KeyBinding.from_dict(binding.to_dict())

        self.assertEqual(restored, binding)

    def test_windows_letter_bindings_can_derive_scan_code(self) -> None:
        scan_code = derive_native_scan_code_from_qt_key(68)

        if platform.system() == "Windows":
            self.assertGreater(scan_code, 0)
        else:
            self.assertEqual(scan_code, 0)


if __name__ == "__main__":
    unittest.main()
