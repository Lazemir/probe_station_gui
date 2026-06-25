import math
import unittest

from probe_station_gui.joystick_feedrate_targets import (
    clean_axis_feedrate_limits,
    feedrate_key,
    feedrate_target_for_axis,
)


TARGET_LABELS = {
    "xy": "XY",
    "focus": "Z Focus",
    "needles": "A Needles",
    "turntable": "B Turntable",
    "common": "Common",
}


class JoystickFeedrateTargetsTest(unittest.TestCase):
    def test_feedrate_key_normalizes_target_and_mode(self) -> None:
        self.assertEqual(
            feedrate_key(
                " Focus ",
                " STEP ",
                target_labels=TARGET_LABELS,
                default_target="xy",
                valid_modes={"jog", "step"},
                default_mode="jog",
            ),
            "step:focus",
        )
        self.assertEqual(
            feedrate_key(
                "bad",
                "bad",
                target_labels=TARGET_LABELS,
                default_target="xy",
                valid_modes={"jog", "step"},
                default_mode="jog",
            ),
            "jog:xy",
        )

    def test_feedrate_target_for_axis_maps_special_axes(self) -> None:
        kwargs = {
            "xy_target": "xy",
            "focus_target": "focus",
            "needles_target": "needles",
            "turntable_target": "turntable",
        }

        self.assertEqual(feedrate_target_for_axis("Z", **kwargs), "focus")
        self.assertEqual(feedrate_target_for_axis("a", **kwargs), "needles")
        self.assertEqual(feedrate_target_for_axis(" B ", **kwargs), "turntable")
        self.assertEqual(feedrate_target_for_axis("X", **kwargs), "xy")
        self.assertEqual(feedrate_target_for_axis("C", **kwargs), "xy")

    def test_clean_axis_feedrate_limits_filters_invalid_values(self) -> None:
        self.assertEqual(
            clean_axis_feedrate_limits(
                {
                    " x ": "500",
                    "Y": 0.0,
                    "Z": math.nan,
                    "A": "bad",
                    "B": 80,
                    "invalid": 100,
                },
                valid_axes=("X", "Y", "Z", "A", "B", "C"),
            ),
            {"X": 500.0, "B": 80.0},
        )


if __name__ == "__main__":
    unittest.main()
