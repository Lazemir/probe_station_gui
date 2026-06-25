import unittest

from probe_station_gui.settings.needle_calibration_config import (
    LCR_METER_TYPE_KEITHLEY,
    NeedleCalibrationSettings,
    SavedStagePositionSettings,
)
from probe_station_gui.settings.manager import (
    SettingsManager,
)


class NeedleCalibrationParsingTest(unittest.TestCase):
    def test_parse_normalizes_lcr_choices_ranges_and_feedrate(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_needle_calibration(
            {
                "meter_type": LCR_METER_TYPE_KEITHLEY,
                "visa_resource": " COM5 ",
                "keithley_source_resource": " GPIB2::7::INSTR ",
                "keithley_voltmeter_resource": " GPIB2::8::INSTR ",
                "measurement_function": "Cp-Rp",
                "range_mode": "HOLD",
                "auto_range_enabled": True,
                "impedance_range": "2",
                "dcr_range": "5",
                "frequency_hz": "1234",
                "level_mode": "CURRENT",
                "voltage_level_v": "0.05",
                "current_level_a": "0.001",
                "source_resistance_ohm": "50",
                "aperture_rate": "SLOW",
                "aperture_averages": "16",
                "trigger_source": "BUS",
                "trigger_delay_s": "0.25",
                "bias_enabled": "true",
                "bias_level_v": "1.5",
                "monitor1": "R",
                "monitor2": "X",
                "alc_enabled": "true",
                "short_threshold_ohm": "12.5",
                "poll_interval_ms": "500",
                "feedrate_mm_min": "0.1",
                "contact_zone_mm": "0.125",
            }
        )

        self.assertEqual(parsed.meter_type, LCR_METER_TYPE_KEITHLEY)
        self.assertEqual(parsed.visa_resource, "COM5")
        self.assertEqual(parsed.keithley_source_resource, "GPIB2::7::INSTR")
        self.assertEqual(parsed.keithley_voltmeter_resource, "GPIB2::8::INSTR")
        self.assertEqual(parsed.measurement_function, "Cp-Rp")
        self.assertEqual(parsed.range_mode, "HOLD")
        self.assertFalse(parsed.auto_range_enabled)
        self.assertEqual(parsed.impedance_range, 2)
        self.assertEqual(parsed.dcr_range, 5)
        self.assertEqual(parsed.frequency_hz, 1234.0)
        self.assertEqual(parsed.level_mode, "CURRENT")
        self.assertEqual(parsed.voltage_level_v, 0.05)
        self.assertEqual(parsed.current_level_a, 0.001)
        self.assertEqual(parsed.source_resistance_ohm, 50)
        self.assertEqual(parsed.aperture_averages, 16)
        self.assertEqual(parsed.trigger_source, "BUS")
        self.assertTrue(parsed.bias_enabled)
        self.assertEqual(parsed.bias_level_v, 1.5)
        self.assertEqual(parsed.monitor1, "R")
        self.assertEqual(parsed.monitor2, "X")
        self.assertTrue(parsed.alc_enabled)
        self.assertEqual(parsed.short_threshold_ohm, 12.5)
        self.assertEqual(parsed.poll_interval_ms, 500)
        self.assertEqual(parsed.feedrate_mm_min, 1.0)
        self.assertEqual(parsed.contact_zone_mm, 0.125)

    def test_parse_migrates_down_position_to_raise_when_raise_is_missing(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_needle_calibration(
            {
                "down_position_mm": "1.25",
                "down_position_configured": True,
            }
        )

        self.assertEqual(parsed.down_position_mm, 1.25)
        self.assertTrue(parsed.down_position_configured)
        self.assertEqual(parsed.raise_position_mm, 1.25)
        self.assertTrue(parsed.raise_position_configured)

    def test_parse_preserves_saved_chip_and_stone_positions(self) -> None:
        manager = object.__new__(SettingsManager)

        parsed = manager._parse_needle_calibration(
            {
                "chip_position": {
                    "x_mm": "1.0",
                    "y_mm": "2.0",
                    "z_mm": "3.0",
                    "configured": True,
                },
                "stone_position": {
                    "x_mm": "4.0",
                    "y_mm": "5.0",
                    "z_mm": "6.0",
                    "configured": True,
                },
            }
        )

        self.assertEqual(
            parsed.chip_position,
            SavedStagePositionSettings(1.0, 2.0, 3.0, True),
        )
        self.assertEqual(
            parsed.stone_position,
            SavedStagePositionSettings(4.0, 5.0, 6.0, True),
        )


class NeedleCalibrationRoundTripTest(unittest.TestCase):
    def test_round_trip_preserves_saved_positions(self) -> None:
        settings = NeedleCalibrationSettings(
            chip_position=SavedStagePositionSettings(
                x_mm=1.0,
                y_mm=2.0,
                z_mm=3.0,
                configured=True,
            ),
            stone_position=SavedStagePositionSettings(
                x_mm=4.0,
                y_mm=5.0,
                z_mm=6.0,
                configured=True,
            ),
        )

        restored = settings.clone()

        self.assertEqual(restored.chip_position, settings.chip_position)
        self.assertEqual(restored.stone_position, settings.stone_position)


if __name__ == "__main__":
    unittest.main()
