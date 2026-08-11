import json
import logging
import os
import platform
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from probe_station_gui.notifications.telegram_settings import (
    TELEGRAM_ALERT_TYPES,
    TelegramSettings,
)
from probe_station_gui.settings.axis_calibration_config import AxisCalibrationSettings
from probe_station_gui.settings.controls_config import KeyBinding
from probe_station_gui.settings.document import Settings, SettingsDocumentCodec
from probe_station_gui.settings.jog_config import JogSettings
from probe_station_gui.settings.manager import SettingsManager
from probe_station_gui.settings.needle_calibration_config import (
    LCR_METER_TYPE_KEITHLEY,
    NeedleCalibrationSettings,
    SavedStagePositionSettings,
)
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)
from probe_station_gui.settings.oscillation_config import OscillationSettings
from probe_station_gui.settings.runtime_documents import (
    METER_CONNECTION_STATE_FILENAME,
    SERIAL_CONNECTION_STATE_FILENAME,
    RuntimeStateDocuments,
)
from probe_station_gui.settings.sections import ApiSettings, ClickToMoveSettings
from probe_station_gui.shared.qt_compat import derive_native_scan_code_from_qt_key
from probe_station_gui.stage.fluidnc_protocol import parse_fluidnc_axis_max_feedrates


def _codec() -> SettingsDocumentCodec:
    return SettingsDocumentCodec(
        default_log_path="probe-station-gui.log",
        logger=logging.getLogger(__name__),
    )


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


class TelegramSettingsTest(unittest.TestCase):
    def test_telegram_settings_round_trip_preserves_alerts_without_token(self) -> None:
        settings = TelegramSettings(
            enabled=True,
            bot_token="123:abc",
            bot_username="probe_station_bot",
            chat_id="456",
            chat_title="Lab User",
            linked_at_utc="2026-05-28T12:00:00+00:00",
            alerts={
                "route_attention": True,
                "route_started": True,
                "route_completed": False,
                "route_failed": True,
                "contact_seek_failed": False,
                "camera_error": True,
            },
        )

        serialized = settings.to_dict()
        restored = TelegramSettings(**serialized)

        self.assertNotIn("bot_token", serialized)
        self.assertEqual(restored.bot_token, "")
        self.assertEqual(restored.enabled, settings.enabled)
        self.assertEqual(restored.bot_username, settings.bot_username)
        self.assertEqual(restored.chat_id, settings.chat_id)
        self.assertEqual(restored.chat_title, settings.chat_title)
        self.assertEqual(restored.linked_at_utc, settings.linked_at_utc)
        self.assertEqual(restored.alerts, settings.alerts)
        self.assertFalse(restored.alert_enabled("route_completed"))
        self.assertFalse(restored.alert_enabled("unknown"))

    def test_default_telegram_alerts_cover_declared_types(self) -> None:
        settings = TelegramSettings()

        self.assertEqual(
            set(settings.alerts),
            {key for key, _label in TELEGRAM_ALERT_TYPES},
        )
        self.assertTrue(all(settings.alerts.values()))


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
        parsed = _codec().decode(
            {"jog": {
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
            }}
        ).jog

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
        parsed = _codec().decode(
            {"jog": {
                "manual_axis_feedrate_mm_min": "0.1",
                "focus_feedrate_mm_min": "0.1",
                "focus_step_feedrate_mm_min": "0.1",
                "needles_step_feedrate_mm_min": "0.1",
                "turntable_feedrate_mm_min": "0.1",
                "turntable_step_feedrate_mm_min": "0.1",
            }}
        ).jog

        self.assertEqual(parsed.manual_axis_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.focus_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.focus_step_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.needles_step_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.turntable_feedrate_mm_min, 1.0)
        self.assertEqual(parsed.turntable_step_feedrate_mm_min, 1.0)

    def test_parse_feedrates_clamps_legacy_sub_one_values(self) -> None:
        parsed = _codec().decode(
            {"feedrates": {
                "linear": {"presets": [0.1, 1.0, 3.0], "default": 0.1},
                "rotary": {"presets": [0.1, 1.0, 90.0], "default": 0.1},
            }}
        ).feedrates

        self.assertEqual(parsed.linear.presets, [1.0, 3.0])
        self.assertEqual(parsed.linear.default, 1.0)
        self.assertEqual(parsed.rotary.presets, [1.0, 90.0])
        self.assertEqual(parsed.rotary.default, 1.0)

    def test_parse_fluidnc_axis_max_feedrates(self) -> None:
        rates = parse_fluidnc_axis_max_feedrates(
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
        codec = _codec()
        low = codec.decode(
            {"click_to_move": {"pending_timeout_s": -1}}
        ).click_to_move
        high = codec.decode(
            {"click_to_move": {"pending_timeout_s": 999}}
        ).click_to_move
        fallback = codec.decode(
            {"click_to_move": {"pending_timeout_s": "nan"}}
        ).click_to_move

        self.assertEqual(
            low.pending_timeout_s,
            SettingsDocumentCodec.MIN_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
        )
        self.assertEqual(
            high.pending_timeout_s,
            SettingsDocumentCodec.MAX_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
        )
        self.assertEqual(
            fallback.pending_timeout_s,
            SettingsDocumentCodec.DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
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
        parsed = _codec().decode(
            {"api": {
                "enabled": True,
                "host": " 127.0.0.1 ",
                "port": "8766",
            }}
        ).api

        self.assertTrue(parsed.enabled)
        self.assertEqual(parsed.host, "127.0.0.1")
        self.assertEqual(parsed.port, 8766)


class AxisCalibrationSettingsTest(unittest.TestCase):
    def test_axis_calibration_defaults_to_disabled(self) -> None:
        self.assertFalse(AxisCalibrationSettings().enabled)

    def test_axis_calibration_round_trip_preserves_curve_snapshot(self) -> None:
        settings = AxisCalibrationSettings(
            enabled=True,
            calibration_file="axis.npz",
            controller_points=[0.0, 1.0],
            physical_points=[2.0, 3.0],
        )

        restored = AxisCalibrationSettings(**settings.to_dict())

        self.assertEqual(restored, settings)


class SerialConnectionStateTest(unittest.TestCase):
    def _documents_for_temp_dir(self, directory: Path) -> RuntimeStateDocuments:
        return RuntimeStateDocuments(
            directory,
            logger=logging.getLogger("settings_manager_test"),
        )

    def test_serial_auto_connect_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._documents_for_temp_dir(Path(temp_dir))

            self.assertFalse(manager.serial_auto_connect_enabled())

    def test_serial_auto_connect_follows_last_saved_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._documents_for_temp_dir(Path(temp_dir))

            manager.save_serial_connection_state(
                True,
                port="COM3",
                baud_rate=115200,
            )

            self.assertTrue(manager.serial_auto_connect_enabled())
            state_path = (
                Path(temp_dir) / SERIAL_CONNECTION_STATE_FILENAME
            )
            with state_path.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["status"], "connected")
            self.assertEqual(saved["port"], "COM3")
            self.assertEqual(saved["baud_rate"], 115200)

            manager.save_serial_connection_state(False)

            self.assertFalse(manager.serial_auto_connect_enabled())

    def test_meter_auto_connect_follows_last_saved_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._documents_for_temp_dir(Path(temp_dir))

            self.assertFalse(manager.meter_auto_connect_enabled())

            manager.save_meter_connection_state(
                True,
                meter_type="keithley_2400_2182a",
                description="Keithley 2400 GPIB0::1::INSTR",
            )

            self.assertTrue(manager.meter_auto_connect_enabled())
            state_path = (
                Path(temp_dir) / METER_CONNECTION_STATE_FILENAME
            )
            with state_path.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["status"], "connected")
            self.assertEqual(saved["meter_type"], "keithley_2400_2182a")
            self.assertEqual(saved["description"], "Keithley 2400 GPIB0::1::INSTR")

            manager.save_meter_connection_state(False)

            self.assertFalse(manager.meter_auto_connect_enabled())


class SettingsLoadTest(unittest.TestCase):
    def test_load_accepts_utf8_bom_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "ProbeStationGUI"
            config_dir.mkdir()
            config_path = config_dir / SettingsManager.CONFIG_FILENAME
            config_path.write_text("\ufeff{}", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "APPDATA": temp_dir,
                    "LOCALAPPDATA": temp_dir,
                    "XDG_CONFIG_HOME": str(Path(temp_dir) / "xdg-config"),
                    "XDG_STATE_HOME": str(Path(temp_dir) / "xdg-state"),
                },
            ), patch(
                "probe_station_gui.settings.manager.platform.system",
                return_value="Windows",
            ), patch("probe_station_gui.settings.manager.configure_logging"):
                loaded = SettingsManager().settings

            self.assertIsInstance(loaded, Settings)
            bindings = loaded.controls.get("toggle_jog_step", [])
            self.assertEqual(len(bindings), 1)
            self.assertEqual(bindings[0].qt_key, 74)
            self.assertEqual(bindings[0].text, "j")


if __name__ == "__main__":
    unittest.main()
