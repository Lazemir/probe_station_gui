import unittest

from probe_station_gui.settings.axis_calibration_config import (
    AxisACalibrationConfig,
    AxisACalibrationSettings,
    AxisZCalibrationConfig,
    AxisZCalibrationSettings,
    parse_axis_a_calibration,
    parse_axis_z_calibration,
)
from probe_station_gui.settings.axis_calibration_npz import (
    LINEAR_INTERPOLATION_MODEL,
)


class AxisCalibrationConfigTest(unittest.TestCase):
    def test_new_axis_defaults_clear_legacy_source_metadata(self) -> None:
        self.assertEqual(AxisACalibrationSettings().source, "")
        self.assertEqual(AxisZCalibrationSettings().source, "")

    def test_axis_a_settings_round_trip_preserves_model(self) -> None:
        settings = AxisACalibrationSettings(
            configured=True,
            steps_per_mm=1234.0,
            commanded_lowering_min_mm=0.1,
            commanded_lowering_max_mm=4.0,
        )

        restored = AxisACalibrationSettings(**settings.to_dict())

        self.assertEqual(restored, settings)

    def test_axis_a_settings_clone_copies_interpolation_points(self) -> None:
        settings = AxisACalibrationSettings(
            interpolation_gcode_mm=[-2.0, 0.0],
            interpolation_display_mm=[-3.0, 0.0],
        )

        restored = settings.clone()
        restored.interpolation_gcode_mm.append(1.0)
        restored.interpolation_display_mm.append(2.0)

        self.assertEqual(settings.interpolation_gcode_mm, [-2.0, 0.0])
        self.assertEqual(settings.interpolation_display_mm, [-3.0, 0.0])

    def test_axis_z_settings_clone_copies_coefficients(self) -> None:
        settings = AxisZCalibrationSettings(
            configured=True,
            coefficients_mm=[1, 2, 3, 4, 5, 6],
            interpolation_gcode_mm=[0.0, 1.0],
            interpolation_display_mm=[0.0, 2.0],
        )

        restored = settings.clone()
        restored.coefficients_mm.append(7)
        restored.interpolation_gcode_mm.append(3.0)
        restored.interpolation_display_mm.append(4.0)

        self.assertEqual(settings.coefficients_mm, [1, 2, 3, 4, 5, 6])
        self.assertEqual(restored.coefficients_mm, [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(settings.interpolation_gcode_mm, [0.0, 1.0])
        self.assertEqual(settings.interpolation_display_mm, [0.0, 2.0])

    def test_legacy_models_still_round_trip_through_parsers(self) -> None:
        axis_a = AxisACalibrationSettings(configured=True, source="")
        axis_z = AxisZCalibrationSettings(configured=True, source="")

        parsed_a = parse_axis_a_calibration(
            axis_a.to_dict(),
            AxisACalibrationConfig(),
            expected_model="cosine_displacement",
        )
        parsed_z = parse_axis_z_calibration(
            axis_z.to_dict(),
            AxisZCalibrationConfig(),
        )

        self.assertEqual(parsed_a, AxisACalibrationConfig(**axis_a.to_dict()))
        self.assertEqual(parsed_z, AxisZCalibrationConfig(**axis_z.to_dict()))

    def test_axis_a_parser_clones_default_interpolation_points(self) -> None:
        defaults = AxisACalibrationConfig(
            interpolation_gcode_mm=[-1.0, 0.0],
            interpolation_display_mm=[-2.0, 0.0],
        )

        parsed = parse_axis_a_calibration(
            None,
            defaults,
            expected_model="cosine_displacement",
        )
        parsed.interpolation_gcode_mm.append(1.0)
        parsed.interpolation_display_mm.append(1.0)

        self.assertEqual(defaults.interpolation_gcode_mm, [-1.0, 0.0])
        self.assertEqual(defaults.interpolation_display_mm, [-2.0, 0.0])

    def test_axis_z_parser_clones_default_interpolation_points(self) -> None:
        defaults = AxisZCalibrationConfig(
            interpolation_gcode_mm=[0.0, 1.0],
            interpolation_display_mm=[0.0, 2.0],
        )

        parsed = parse_axis_z_calibration(None, defaults)
        parsed.interpolation_gcode_mm.append(3.0)
        parsed.interpolation_display_mm.append(4.0)

        self.assertEqual(defaults.interpolation_gcode_mm, [0.0, 1.0])
        self.assertEqual(defaults.interpolation_display_mm, [0.0, 2.0])

    def test_axis_a_migrates_positive_offset_and_amplitude_to_signed_model(
        self,
    ) -> None:
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

    def test_axis_a_preserves_valid_linear_interpolation_model(self) -> None:
        parsed = parse_axis_a_calibration(
            {
                "configured": True,
                "model": LINEAR_INTERPOLATION_MODEL,
                "calibration_file": "axis-a.npz",
                "interpolation_gcode_mm": [-2.0, -1.0, 0.0],
                "interpolation_display_mm": [-3.0, -1.5, 0.0],
                "interpolation_direction": -1,
            },
            AxisACalibrationConfig(),
            expected_model="cosine_displacement",
        )

        self.assertTrue(parsed.configured)
        self.assertEqual(parsed.model, LINEAR_INTERPOLATION_MODEL)
        self.assertEqual(parsed.calibration_file, "axis-a.npz")
        self.assertEqual(parsed.interpolation_gcode_mm, [-2.0, -1.0, 0.0])
        self.assertEqual(parsed.interpolation_display_mm, [-3.0, -1.5, 0.0])
        self.assertEqual(parsed.interpolation_direction, -1)

    def test_axis_a_disables_invalid_linear_interpolation_snapshot(self) -> None:
        parsed = parse_axis_a_calibration(
            {
                "configured": True,
                "model": LINEAR_INTERPOLATION_MODEL,
                "interpolation_gcode_mm": [-1.0, 0.0],
                "interpolation_display_mm": [-2.0],
            },
            AxisACalibrationConfig(),
            expected_model="cosine_displacement",
        )

        self.assertFalse(parsed.configured)
        self.assertEqual(parsed.interpolation_gcode_mm, [])
        self.assertEqual(parsed.interpolation_display_mm, [])

    def test_axis_a_clears_legacy_png_source(self) -> None:
        parsed = parse_axis_a_calibration(
            {"source": "calibrations/legacy-plot.PNG"},
            AxisACalibrationConfig(),
            expected_model="cosine_displacement",
        )

        self.assertEqual(parsed.source, "")

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

    def test_axis_z_preserves_valid_linear_interpolation_model(self) -> None:
        parsed = parse_axis_z_calibration(
            {
                "configured": True,
                "model": LINEAR_INTERPOLATION_MODEL,
                "calibration_file": "axis-z.npz",
                "interpolation_gcode_mm": [0.0, 1.0, 2.0],
                "interpolation_display_mm": [0.1, 1.1, 2.1],
                "interpolation_direction": 1,
            },
            AxisZCalibrationConfig(),
        )

        self.assertTrue(parsed.configured)
        self.assertEqual(parsed.model, LINEAR_INTERPOLATION_MODEL)
        self.assertEqual(parsed.calibration_file, "axis-z.npz")
        self.assertEqual(parsed.interpolation_gcode_mm, [0.0, 1.0, 2.0])
        self.assertEqual(parsed.interpolation_display_mm, [0.1, 1.1, 2.1])
        self.assertEqual(parsed.interpolation_direction, 1)

    def test_axis_z_disables_non_monotonic_interpolation_snapshot(self) -> None:
        parsed = parse_axis_z_calibration(
            {
                "configured": True,
                "model": LINEAR_INTERPOLATION_MODEL,
                "interpolation_gcode_mm": [0.0, 2.0, 1.0],
                "interpolation_display_mm": [0.0, 1.0, 2.0],
            },
            AxisZCalibrationConfig(),
        )

        self.assertFalse(parsed.configured)
        self.assertEqual(parsed.interpolation_gcode_mm, [])
        self.assertEqual(parsed.interpolation_display_mm, [])

    def test_axis_z_clears_legacy_png_source(self) -> None:
        parsed = parse_axis_z_calibration(
            {"source": "calibrations/legacy-plot.png"},
            AxisZCalibrationConfig(),
        )

        self.assertEqual(parsed.source, "")

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
