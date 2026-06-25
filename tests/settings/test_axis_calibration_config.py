import unittest

from probe_station_gui.settings.axis_calibration_config import (
    AxisACalibrationConfig,
    AxisACalibrationSettings,
    AxisZCalibrationConfig,
    AxisZCalibrationSettings,
    parse_axis_a_calibration,
    parse_axis_z_calibration,
)


class AxisCalibrationConfigTest(unittest.TestCase):
    def test_axis_a_settings_round_trip_preserves_model(self) -> None:
        settings = AxisACalibrationSettings(
            configured=True,
            steps_per_mm=1234.0,
            commanded_lowering_min_mm=0.1,
            commanded_lowering_max_mm=4.0,
        )

        restored = AxisACalibrationSettings(**settings.to_dict())

        self.assertEqual(restored, settings)

    def test_axis_z_settings_clone_copies_coefficients(self) -> None:
        settings = AxisZCalibrationSettings(
            configured=True,
            coefficients_mm=[1, 2, 3, 4, 5, 6],
        )

        restored = settings.clone()
        restored.coefficients_mm.append(7)

        self.assertEqual(settings.coefficients_mm, [1, 2, 3, 4, 5, 6])
        self.assertEqual(restored.coefficients_mm, [1, 2, 3, 4, 5, 6, 7])

    def test_axis_a_migrates_positive_offset_and_amplitude_to_signed_model(self) -> None:
        parsed = parse_axis_a_calibration(
            {
                "configured": True,
                "offset_mm": 0.18,
                "amplitude_mm": 4.25,
            },
            AxisACalibrationConfig(),
            expected_model="cosine_displacement",
        )

        self.assertTrue(parsed.configured)
        self.assertLess(parsed.offset_mm, 0.0)
        self.assertLess(parsed.amplitude_mm, 0.0)

    def test_axis_a_disables_invalid_motion_range(self) -> None:
        parsed = parse_axis_a_calibration(
            {
                "configured": True,
                "commanded_lowering_min_mm": 5.0,
                "commanded_lowering_max_mm": 5.0,
            },
            AxisACalibrationConfig(),
            expected_model="cosine_displacement",
        )

        self.assertFalse(parsed.configured)

    def test_axis_z_rejects_invalid_coefficients(self) -> None:
        defaults = AxisZCalibrationConfig(coefficients_mm=[1, 2, 3, 4, 5, 6])

        parsed = parse_axis_z_calibration(
            {
                "configured": True,
                "coefficients_mm": [1, 2, "nan", 4, 5, 6],
            },
            defaults,
        )

        self.assertTrue(parsed.configured)
        self.assertEqual(parsed.coefficients_mm, [1, 2, 3, 4, 5, 6])

    def test_axis_z_disables_invalid_gcode_range(self) -> None:
        parsed = parse_axis_z_calibration(
            {
                "configured": True,
                "gcode_min_mm": 5.0,
                "gcode_max_mm": 5.0,
            },
            AxisZCalibrationConfig(),
        )

        self.assertFalse(parsed.configured)


if __name__ == "__main__":
    unittest.main()
