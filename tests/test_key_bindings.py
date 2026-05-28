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
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "probe_station_gui")]
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
ApiSettings = settings_manager.ApiSettings
JogSettings = settings_manager.JogSettings
ClickToMoveSettings = settings_manager.ClickToMoveSettings
NeedleCalibrationSettings = settings_manager.NeedleCalibrationSettings
LCR_METER_TYPE_KEITHLEY = settings_manager.LCR_METER_TYPE_KEITHLEY
AxisACalibrationSettings = settings_manager.AxisACalibrationSettings
AxisZCalibrationSettings = settings_manager.AxisZCalibrationSettings
OscillationSettings = settings_manager.OscillationSettings
ObjectiveCalibrationSettings = settings_manager.ObjectiveCalibrationSettings
ObjectivesSettings = settings_manager.ObjectivesSettings
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
            meter_type=LCR_METER_TYPE_KEITHLEY,
            visa_resource="COM5",
            keithley_source_resource="GPIB2::7::INSTR",
            keithley_voltmeter_resource="GPIB2::8::INSTR",
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
            contact_zone_mm=0.125,
            raise_position_mm=0.25,
            raise_position_configured=True,
            down_position_mm=1.25,
            down_position_configured=True,
        )

        restored = settings.to_dict()

        self.assertEqual(restored["meter_type"], settings.meter_type)
        self.assertEqual(restored["visa_resource"], settings.visa_resource)
        self.assertEqual(
            restored["keithley_source_resource"],
            settings.keithley_source_resource,
        )
        self.assertEqual(
            restored["keithley_voltmeter_resource"],
            settings.keithley_voltmeter_resource,
        )
        self.assertEqual(restored["measurement_function"], settings.measurement_function)
        self.assertEqual(restored["range_mode"], settings.range_mode)
        self.assertEqual(restored["impedance_range"], settings.impedance_range)
        self.assertEqual(restored["dcr_range"], settings.dcr_range)
        self.assertEqual(restored["level_mode"], settings.level_mode)
        self.assertEqual(restored["source_resistance_ohm"], settings.source_resistance_ohm)
        self.assertEqual(restored["monitor1"], settings.monitor1)
        self.assertEqual(restored["monitor2"], settings.monitor2)
        self.assertEqual(restored["contact_zone_mm"], settings.contact_zone_mm)
        self.assertEqual(restored["raise_position_mm"], settings.raise_position_mm)
        self.assertEqual(
            restored["raise_position_configured"],
            settings.raise_position_configured,
        )
        self.assertEqual(restored["down_position_mm"], settings.down_position_mm)
        self.assertEqual(
            restored["down_position_configured"],
            settings.down_position_configured,
        )

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
    def test_jog_settings_round_trip_preserves_machine_control_settings(self) -> None:
        settings = JogSettings(
            mode="step",
            linear_distance_mm=12.5,
            rotary_distance_deg=7.5,
            motion_safety_disabled=True,
            manual_axis="B",
            manual_axis_distance_mm=0.25,
            manual_axis_mode="G90",
            manual_axis_feedrate_mm_min=123.4,
            focus_feedrate_mm_min=55.0,
            focus_step_feedrate_mm_min=44.0,
            needles_step_feedrate_mm_min=33.0,
            turntable_feedrate_mm_min=321.0,
            turntable_step_feedrate_mm_min=222.0,
        )

        restored = JogSettings(**settings.to_dict())

        self.assertEqual(restored, settings)

    def test_parse_jog_normalizes_machine_control_settings(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_jog(
            {
                "mode": "step",
                "linear_distance_mm": "2.5",
                "rotary_distance_deg": "3.5",
                "unsafe_motion_enabled": "true",
                "manual_axis": "b",
                "manual_axis_distance_mm": "0.125",
                "manual_axis_feedrate_mm_min": "123.4",
                "focus_feedrate_mm_min": "44.5",
                "focus_step_feedrate_mm_min": "22.5",
                "needles_step_feedrate_mm_min": "11.5",
                "turntable_feedrate_mm_min": "222.0",
                "turntable_step_feedrate_mm_min": "111.0",
            }
        )

        self.assertEqual(parsed.mode, "step")
        self.assertEqual(parsed.linear_distance_mm, 2.5)
        self.assertEqual(parsed.rotary_distance_deg, 3.5)
        self.assertTrue(parsed.motion_safety_disabled)
        self.assertEqual(parsed.manual_axis, "B")
        self.assertEqual(parsed.manual_axis_distance_mm, 0.125)
        self.assertEqual(parsed.manual_axis_mode, "G91")
        self.assertEqual(parsed.manual_axis_feedrate_mm_min, 123.4)
        self.assertEqual(parsed.focus_feedrate_mm_min, 44.5)
        self.assertEqual(parsed.focus_step_feedrate_mm_min, 22.5)
        self.assertEqual(parsed.needles_step_feedrate_mm_min, 11.5)
        self.assertEqual(parsed.turntable_feedrate_mm_min, 222.0)
        self.assertEqual(parsed.turntable_step_feedrate_mm_min, 111.0)

    def test_parse_jog_clamps_legacy_sub_one_feedrates(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_jog(
            {
                "manual_axis_feedrate_mm_min": "0.1",
                "focus_feedrate_mm_min": "0.1",
                "focus_step_feedrate_mm_min": "0.1",
                "needles_step_feedrate_mm_min": "0.1",
                "turntable_feedrate_mm_min": "0.1",
                "turntable_step_feedrate_mm_min": "0.1",
            }
        )

        self.assertEqual(parsed.manual_axis_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.focus_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.focus_step_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.needles_step_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.turntable_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.turntable_step_feedrate_mm_min, 1.0)

    def test_parse_feedrates_clamps_legacy_sub_one_values(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_feedrates(
            {
                "linear": {"presets": [0.1, 1.0, 3.0], "default": 0.1},
                "rotary": {"presets": [0.1, 1.0, 90.0], "default": 0.1},
            },
            legacy_presets=[],
        )

        self.assertEqual(parsed.linear.presets, [1.0, 3.0])
        self.assertEqual(parsed.linear.default, 1.0)
        self.assertEqual(parsed.rotary.presets, [1.0, 90.0])
        self.assertEqual(parsed.rotary.default, 1.0)

    def test_parse_fluidnc_axis_max_feedrates(self) -> None:
        rates = settings_manager.parse_fluidnc_axis_max_feedrates(
            [
                "axes:",
                "  x:",
                "    max_rate_mm_per_min: 500",
                "  z:",
                "    max_rate_mm_per_min: 100",
                "a:",
                "max_rate_mm_per_min: 80",
                "  b:",
                "    max_rate_mm_per_min: 360",
            ]
        )

        self.assertEqual(
            rates,
            {"X": 500.0, "Z": 100.0, "A": 80.0, "B": 360.0},
        )


class ClickToMoveSettingsTest(unittest.TestCase):
    def test_click_to_move_settings_round_trip(self) -> None:
        settings = ClickToMoveSettings(pending_timeout_s=12.5)

        restored = ClickToMoveSettings(**settings.to_dict())

        self.assertEqual(restored.pending_timeout_s, 12.5)

    def test_parse_click_to_move_clamps_invalid_timeout(self) -> None:
        manager = object.__new__(SettingsManager)

        low = manager._parse_click_to_move({"pending_timeout_s": -1})
        high = manager._parse_click_to_move({"pending_timeout_s": 999})
        fallback = manager._parse_click_to_move({"pending_timeout_s": "nan"})

        self.assertEqual(
            low.pending_timeout_s,
            SettingsManager.MIN_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
        )
        self.assertEqual(
            high.pending_timeout_s,
            SettingsManager.MAX_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
        )
        self.assertEqual(
            fallback.pending_timeout_s,
            SettingsManager.DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
        )


class ObjectiveSettingsTest(unittest.TestCase):
    def test_objective_settings_round_trip_preserves_matrix_and_offsets(self) -> None:
        profile = ObjectiveCalibrationSettings(
            name="X20",
            xy_offset_x_mm=0.12,
            xy_offset_y_mm=-0.34,
            xy_offset_configured=True,
            z_offset_mm=0.056,
            z_offset_configured=True,
            pixels_to_mm=[[0.001, 0.0], [0.0, 0.0012]],
            xy_calibration_configured=True,
        )
        settings = ObjectivesSettings(
            active_name="X20",
            apply_offsets_on_change=True,
            objectives={"X20": profile},
        )

        restored = ObjectivesSettings(
            active_name=settings.to_dict()["active_name"],
            apply_offsets_on_change=settings.to_dict()["apply_offsets_on_change"],
            objectives={
                "X20": ObjectiveCalibrationSettings(
                    **settings.to_dict()["objectives"]["X20"]
                )
            },
        )

        self.assertEqual(restored.active_name, "X20")
        self.assertEqual(restored.objectives["X20"].pixels_to_mm, profile.pixels_to_mm)
        self.assertTrue(restored.objectives["X20"].xy_offset_configured)

    def test_parse_objectives_rejects_invalid_matrix(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_objectives(
            {
                "active_name": "x10",
                "objectives": {
                    "X10": {
                        "pixels_to_mm": [[1.0, 2.0], [2.0, 4.0]],
                        "xy_calibration_configured": True,
                    }
                },
            }
        )

        self.assertEqual(parsed.active_name, "X10")
        self.assertFalse(parsed.objectives["X10"].xy_calibration_configured)
        self.assertEqual(parsed.objectives["X10"].pixels_to_mm, [])

    def test_parse_objectives_preserves_custom_profile(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_objectives(
            {
                "active_name": "x100",
                "objectives": {
                    "x100": {
                        "pixels_to_mm": [[0.0001, 0.0], [0.0, 0.00011]],
                        "xy_calibration_configured": True,
                    }
                },
            }
        )

        self.assertEqual(parsed.active_name, "X100")
        self.assertIn("X100", parsed.objectives)
        self.assertTrue(parsed.objectives["X100"].xy_calibration_configured)
        self.assertEqual(
            parsed.objectives["X100"].pixels_to_mm,
            [[0.0001, 0.0], [0.0, 0.00011]],
        )

    def test_parse_objectives_uses_remaining_profile_when_active_was_deleted(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_objectives(
            {
                "active_name": "X50",
                "objectives": {
                    "X10": {
                        "xy_calibration_configured": False,
                    }
                },
            }
        )

        self.assertEqual(parsed.active_name, "X10")
        self.assertEqual(list(parsed.objectives), ["X10"])


class ApiSettingsTest(unittest.TestCase):
    def test_api_settings_round_trip(self) -> None:
        settings = ApiSettings(
            enabled=False,
            host="0.0.0.0",
            port=9876,
        )

        restored = ApiSettings(**settings.to_dict())

        self.assertEqual(restored, settings)

    def test_parse_api_normalizes_values(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_api(
            {
                "enabled": True,
                "host": " 127.0.0.1 ",
                "port": "8766",
            }
        )

        self.assertTrue(parsed.enabled)
        self.assertEqual(parsed.host, "127.0.0.1")
        self.assertEqual(parsed.port, 8766)


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
            bindings = loaded.controls.get("toggle_jog_step", [])
            self.assertEqual(len(bindings), 1)
            self.assertEqual(bindings[0].qt_key, 74)
            self.assertEqual(bindings[0].text, "j")


if __name__ == "__main__":
    unittest.main()
