import importlib.util
import json
import logging
import platform
import sys
import tempfile
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
JogSettings = settings_manager.JogSettings
NeedleCalibrationSettings = settings_manager.NeedleCalibrationSettings
AxisACalibrationSettings = settings_manager.AxisACalibrationSettings
AxisZCalibrationSettings = settings_manager.AxisZCalibrationSettings
OscillationSettings = settings_manager.OscillationSettings
SavedStagePositionSettings = settings_manager.SavedStagePositionSettings
SettingsManager = settings_manager.SettingsManager
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


class NeedleCalibrationBookmarkTest(unittest.TestCase):
    def test_clone_preserves_chip_and_stone_positions(self) -> None:
        settings = NeedleCalibrationSettings(
            chip_position=SavedStagePositionSettings(
                x_mm=1.0, y_mm=2.0, z_mm=3.0, configured=True
            ),
            stone_position=SavedStagePositionSettings(
                x_mm=4.0, y_mm=5.0, z_mm=6.0, configured=True
            ),
        )

        restored = settings.clone()

        self.assertEqual(restored.chip_position, settings.chip_position)
        self.assertEqual(restored.stone_position, settings.stone_position)

    def test_lcr_settings_round_trip(self) -> None:
        settings = NeedleCalibrationSettings(
            measurement_function="Cp-Rp",
            range_mode="AUTO",
            auto_range_enabled=True,
            impedance_range=2,
            dcr_range=5,
            frequency_hz=1234.0,
            level_mode="CURRENT",
            voltage_level_v=0.05,
            current_level_a=0.001,
            source_resistance_ohm=100,
            aperture_rate="SLOW",
            aperture_averages=16,
            trigger_source="BUS",
            trigger_delay_s=0.25,
            bias_enabled=True,
            bias_level_v=1.5,
            monitor1="R",
            monitor2="X",
            alc_enabled=True,
        )

        restored = settings.to_dict()

        self.assertEqual(restored["measurement_function"], settings.measurement_function)
        self.assertEqual(restored["range_mode"], settings.range_mode)
        self.assertEqual(restored["impedance_range"], settings.impedance_range)
        self.assertEqual(restored["dcr_range"], settings.dcr_range)
        self.assertEqual(restored["level_mode"], settings.level_mode)
        self.assertEqual(restored["source_resistance_ohm"], settings.source_resistance_ohm)
        self.assertEqual(restored["monitor1"], settings.monitor1)
        self.assertEqual(restored["monitor2"], settings.monitor2)

    def test_oscillation_settings_round_trip(self) -> None:
        settings = OscillationSettings(
            mode="SPIRAL",
            amplitude_mm=0.75,
            feedrate_mm_min=240.0,
            turns_per_sweep=4.5,
        )

        restored = OscillationSettings(**settings.to_dict())

        self.assertEqual(restored, settings)


class JogSettingsTest(unittest.TestCase):
    def test_jog_settings_round_trip_preserves_manual_axis_controls(self) -> None:
        settings = JogSettings(
            linear_distance_mm=12.5,
            rotary_distance_deg=7.5,
            motion_safety_disabled=True,
            show_axis_a_controls=True,
            show_axis_b_controls=True,
            manual_axis_controls_enabled=True,
            manual_axis="B",
            manual_axis_distance_mm=0.25,
            manual_axis_mode="G90",
            manual_axis_feedrate_mm_min=123.4,
        )

        restored = JogSettings(**settings.to_dict())

        self.assertEqual(restored, settings)

    def test_parse_jog_normalizes_manual_axis_controls(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_jog(
            {
                "linear_distance_mm": "2.5",
                "rotary_distance_deg": "3.5",
                "unsafe_motion_enabled": "true",
                "manual_axis": "b",
                "manual_axis_distance_mm": "0.125",
                "manual_axis_feedrate_mm_min": "123.4",
            }
        )

        self.assertEqual(parsed.linear_distance_mm, 2.5)
        self.assertEqual(parsed.rotary_distance_deg, 3.5)
        self.assertTrue(parsed.motion_safety_disabled)
        self.assertTrue(parsed.show_axis_a_controls)
        self.assertTrue(parsed.show_axis_b_controls)
        self.assertTrue(parsed.manual_axis_controls_enabled)
        self.assertEqual(parsed.manual_axis, "B")
        self.assertEqual(parsed.manual_axis_distance_mm, 0.125)
        self.assertEqual(parsed.manual_axis_mode, "G91")
        self.assertEqual(parsed.manual_axis_feedrate_mm_min, 123.4)


class AxisACalibrationSettingsTest(unittest.TestCase):
    def test_axis_a_calibration_defaults_to_disabled(self) -> None:
        self.assertFalse(AxisACalibrationSettings().configured)

    def test_axis_a_calibration_round_trip_preserves_sine_model(self) -> None:
        settings = AxisACalibrationSettings(
            configured=True,
            steps_per_mm=2600.0,
            commanded_lowering_min_mm=0.0,
            commanded_lowering_max_mm=5.5,
            offset_mm=-0.18025492860701603,
            amplitude_mm=-4.256281153779931,
            angular_frequency_rad_per_mm=0.2560331555269034,
            phase_rad=0.9304927419233507,
        )

        restored = AxisACalibrationSettings(**settings.to_dict())

        self.assertEqual(restored, settings)

    def test_parse_axis_a_calibration_disables_invalid_model(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_axis_a_calibration(
            {
                "configured": True,
                "steps_per_mm": 0,
            }
        )

        self.assertFalse(parsed.configured)

    def test_parse_axis_a_calibration_migrates_positive_parameters_to_signed_model(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_axis_a_calibration(
            {
                "configured": True,
                "offset_mm": 0.18025492860701603,
                "amplitude_mm": 4.256281153779931,
            }
        )

        self.assertLess(parsed.offset_mm, 0.0)
        self.assertLess(parsed.amplitude_mm, 0.0)


class AxisZCalibrationSettingsTest(unittest.TestCase):
    def test_axis_z_calibration_defaults_to_disabled(self) -> None:
        self.assertFalse(AxisZCalibrationSettings().configured)

    def test_axis_z_calibration_round_trip_preserves_polynomial_model(self) -> None:
        settings = AxisZCalibrationSettings(
            configured=True,
            steps_per_mm=6335.0,
            gcode_min_mm=0.02,
            gcode_max_mm=23.4,
            coefficients_mm=[1, 2, 3, 4, 5, 6],
        )

        restored = AxisZCalibrationSettings(**settings.to_dict())

        self.assertEqual(restored, settings)

    def test_parse_axis_z_calibration_disables_invalid_range(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_axis_z_calibration(
            {
                "configured": True,
                "gcode_min_mm": 5,
                "gcode_max_mm": 5,
            }
        )

        self.assertFalse(parsed.configured)


class SerialConnectionStateTest(unittest.TestCase):
    def _manager_for_temp_dir(self, directory: Path) -> SettingsManager:
        manager = object.__new__(SettingsManager)
        manager._config_dir = directory
        manager._logger = logging.getLogger("settings_manager_test")
        return manager

    def test_serial_auto_connect_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager_for_temp_dir(Path(temp_dir))

            self.assertFalse(manager.serial_auto_connect_enabled())

    def test_serial_auto_connect_follows_last_saved_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager_for_temp_dir(Path(temp_dir))

            manager.save_serial_connection_state(
                True,
                port="COM3",
                baud_rate=115200,
            )

            self.assertTrue(manager.serial_auto_connect_enabled())
            state_path = (
                Path(temp_dir) / SettingsManager.SERIAL_CONNECTION_STATE_FILENAME
            )
            with state_path.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["status"], "connected")
            self.assertEqual(saved["port"], "COM3")
            self.assertEqual(saved["baud_rate"], 115200)

            manager.save_serial_connection_state(False)

            self.assertFalse(manager.serial_auto_connect_enabled())


class SettingsLoadTest(unittest.TestCase):
    def test_load_accepts_utf8_bom_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / SettingsManager.CONFIG_FILENAME
            config_path.write_text("\ufeff{}", encoding="utf-8")
            manager = object.__new__(SettingsManager)
            manager._config_path = config_path
            manager._logger = logging.getLogger("settings_manager_test")

            loaded = manager._load()

            self.assertIsInstance(loaded, settings_manager.Settings)


if __name__ == "__main__":
    unittest.main()
