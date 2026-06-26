import math
import unittest

from probe_station_gui.views.joystick.feedrate_targets import (
    bounded_feedrate_setting,
    clean_axis_feedrate_limits,
    feedrate_from_slider_value,
    feedrate_key,
    feedrate_limit_known_for_target,
    feedrate_max_for_target,
    feedrate_target_for_axis,
    linear_feedrate_min_max,
    slider_value_from_feedrate,
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

    def test_feedrate_max_for_target_uses_axis_limits_or_fallback(self) -> None:
        kwargs = {
            "common_feedrate_max": None,
            "linear_feedrate_value": 42.0,
            "linear_default": 30.0,
            "linear_presets": [1.0, 100.0, 300.0],
            "min_linear_feedrate": 1.0,
            "max_linear_feedrate": 600.0,
            "xy_target": "xy",
            "focus_target": "focus",
            "needles_target": "needles",
            "turntable_target": "turntable",
            "common_target": "common",
        }

        self.assertEqual(
            feedrate_max_for_target(
                "xy",
                axis_limits={"X": 500.0, "Y": 400.0},
                **kwargs,
            ),
            500.0,
        )
        self.assertEqual(
            feedrate_max_for_target(
                "focus",
                axis_limits={"Z": 120.0},
                **kwargs,
            ),
            120.0,
        )
        self.assertEqual(
            feedrate_max_for_target(
                "needles",
                axis_limits={},
                **kwargs,
            ),
            300.0,
        )

    def test_feedrate_max_for_common_target_uses_common_limit_when_set(self) -> None:
        self.assertEqual(
            feedrate_max_for_target(
                "common",
                axis_limits={},
                common_feedrate_max="250",
                linear_feedrate_value=42.0,
                linear_default=30.0,
                linear_presets=[1.0, 100.0, 300.0],
                min_linear_feedrate=1.0,
                max_linear_feedrate=600.0,
                xy_target="xy",
                focus_target="focus",
                needles_target="needles",
                turntable_target="turntable",
                common_target="common",
            ),
            250.0,
        )

    def test_feedrate_limit_known_for_target_checks_matching_axis(self) -> None:
        kwargs = {
            "common_feedrate_max": None,
            "xy_target": "xy",
            "focus_target": "focus",
            "needles_target": "needles",
            "turntable_target": "turntable",
            "common_target": "common",
        }

        self.assertTrue(
            feedrate_limit_known_for_target(
                "xy",
                axis_limits={"Y": 400.0},
                **kwargs,
            )
        )
        self.assertFalse(
            feedrate_limit_known_for_target(
                "focus",
                axis_limits={"Y": 400.0},
                **kwargs,
            )
        )
        self.assertTrue(
            feedrate_limit_known_for_target(
                "common",
                axis_limits={},
                **{**kwargs, "common_feedrate_max": 300.0},
            )
        )

    def test_bounded_and_linear_range_helpers_clamp_to_known_limits(self) -> None:
        self.assertEqual(
            bounded_feedrate_setting(
                "xy",
                500.0,
                limit_known=True,
                target_max=400.0,
                min_linear_feedrate=1.0,
            ),
            400.0,
        )
        self.assertEqual(
            bounded_feedrate_setting(
                "xy",
                0.5,
                limit_known=False,
                target_max=400.0,
                min_linear_feedrate=1.0,
            ),
            1.0,
        )
        self.assertEqual(
            linear_feedrate_min_max(
                target_max=300.0,
                bounds=(10.0, 500.0),
                min_linear_feedrate=1.0,
            ),
            (10.0, 300.0),
        )
        self.assertEqual(
            linear_feedrate_min_max(
                target_max=300.0,
                bounds=(500.0, 100.0),
                min_linear_feedrate=1.0,
            ),
            (500.0, 500.0),
        )

    def test_slider_conversion_clamps_to_visible_range(self) -> None:
        self.assertEqual(
            slider_value_from_feedrate(
                0.5,
                min_value=1.0,
                max_value=100.0,
                scale=10,
            ),
            10,
        )
        self.assertEqual(
            slider_value_from_feedrate(
                12.34,
                min_value=1.0,
                max_value=100.0,
                scale=10,
            ),
            123,
        )
        self.assertEqual(feedrate_from_slider_value(123, scale=10), 12.3)


if __name__ == "__main__":
    unittest.main()
