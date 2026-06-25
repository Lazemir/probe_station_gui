import unittest

from probe_station_gui.jog_config import JogSettingsDefaults, parse_jog_settings


def _defaults() -> JogSettingsDefaults:
    return JogSettingsDefaults(
        mode="jog",
        linear_distance_mm=25.0,
        rotary_distance_deg=5.0,
        motion_safety_disabled=False,
        manual_axis="A",
        manual_axis_distance_mm=1.0,
        manual_axis_mode="G91",
        manual_axis_feedrate_mm_min=1.0,
        focus_feedrate_mm_min=1.0,
        focus_step_feedrate_mm_min=1.0,
        needles_step_feedrate_mm_min=1.0,
        turntable_feedrate_mm_min=1.0,
        turntable_step_feedrate_mm_min=1.0,
        min_feedrate_mm_min=1.0,
        manual_axes=("X", "Y", "Z", "A", "B", "C"),
        manual_axis_modes=("G91", "G90"),
    )


class JogConfigTest(unittest.TestCase):
    def test_legacy_control_mode_and_unsafe_motion_are_supported(self) -> None:
        parsed = parse_jog_settings(
            {
                "control_mode": " STEP ",
                "unsafe_motion_enabled": "true",
            },
            _defaults(),
        )

        self.assertEqual(parsed.mode, "step")
        self.assertTrue(parsed.motion_safety_disabled)

    def test_explicit_motion_safety_overrides_legacy_unsafe_motion(self) -> None:
        parsed = parse_jog_settings(
            {
                "unsafe_motion_enabled": "true",
                "motion_safety_disabled": "false",
            },
            _defaults(),
        )

        self.assertFalse(parsed.motion_safety_disabled)

    def test_step_feedrates_fall_back_to_matching_jog_feedrates(self) -> None:
        parsed = parse_jog_settings(
            {
                "focus_feedrate_mm_min": "44.5",
                "turntable_feedrate_mm_min": "222.0",
            },
            _defaults(),
        )

        self.assertEqual(parsed.focus_step_feedrate_mm_min, 44.5)
        self.assertEqual(parsed.turntable_step_feedrate_mm_min, 222.0)

    def test_invalid_manual_axis_and_mode_return_to_defaults(self) -> None:
        parsed = parse_jog_settings(
            {
                "manual_axis": "q",
                "manual_axis_mode": "G93",
            },
            _defaults(),
        )

        self.assertEqual(parsed.manual_axis, "A")
        self.assertEqual(parsed.manual_axis_mode, "G91")


if __name__ == "__main__":
    unittest.main()
