import math
import unittest

from probe_station_gui.stage_feedrate_limits import (
    axis_max_feedrate,
    clean_axis_max_feedrates,
    max_feedrate_for_axes,
)


AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3}


class StageFeedrateLimitsTest(unittest.TestCase):
    def test_clean_axis_max_feedrates_filters_and_clamps_values(self) -> None:
        self.assertEqual(
            clean_axis_max_feedrates(
                {
                    " x ": "0.25",
                    "Y": "400",
                    "bad": 500,
                    "Z": "invalid",
                    "A": math.inf,
                },
                axis_index=AXIS_INDEX,
                min_feedrate=1.0,
            ),
            {"X": 1.0, "Y": 400.0},
        )

    def test_axis_max_feedrate_uses_default_for_missing_or_invalid_values(self) -> None:
        feedrates = {"X": 0.5, "Y": 0.0, "Z": math.nan}

        self.assertEqual(
            axis_max_feedrate(
                "x",
                feedrates,
                default_feedrate=600.0,
                min_feedrate=1.0,
            ),
            1.0,
        )
        self.assertEqual(
            axis_max_feedrate(
                "Y",
                feedrates,
                default_feedrate=600.0,
                min_feedrate=1.0,
            ),
            600.0,
        )
        self.assertEqual(
            axis_max_feedrate(
                "missing",
                feedrates,
                default_feedrate=600.0,
                min_feedrate=1.0,
            ),
            600.0,
        )

    def test_max_feedrate_for_axes_returns_limiting_axis(self) -> None:
        feedrates = {"X": 500.0, "Y": 400.0, "Z": 100.0}

        self.assertEqual(
            max_feedrate_for_axes(
                ("X", "Y"),
                feedrates,
                axis_index=AXIS_INDEX,
                default_feedrate=600.0,
                min_feedrate=1.0,
            ),
            400.0,
        )
        self.assertEqual(
            max_feedrate_for_axes(
                "Z",
                feedrates,
                axis_index=AXIS_INDEX,
                default_feedrate=600.0,
                min_feedrate=1.0,
            ),
            100.0,
        )
        self.assertEqual(
            max_feedrate_for_axes(
                ("C",),
                feedrates,
                axis_index=AXIS_INDEX,
                default_feedrate=600.0,
                min_feedrate=1.0,
            ),
            600.0,
        )


if __name__ == "__main__":
    unittest.main()
