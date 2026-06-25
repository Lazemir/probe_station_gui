import math
import unittest

from probe_station_gui.stage_needle_targets import (
    needle_target_lowering_for_action,
    normalise_needle_contact_zone,
    normalise_needle_lowering_target,
)


class StageNeedleTargetsTest(unittest.TestCase):
    def test_normalise_lowering_keeps_none_and_non_negative_values(self) -> None:
        self.assertIsNone(
            normalise_needle_lowering_target(
                None,
                lowering_for_gcode_coordinate=lambda value: abs(value),
            )
        )
        self.assertEqual(
            normalise_needle_lowering_target(
                1.25,
                lowering_for_gcode_coordinate=lambda value: abs(value),
            ),
            1.25,
        )

    def test_normalise_lowering_converts_legacy_negative_coordinate(self) -> None:
        self.assertEqual(
            normalise_needle_lowering_target(
                -1.25,
                lowering_for_gcode_coordinate=lambda value: abs(value) + 0.5,
            ),
            1.75,
        )

    def test_normalise_contact_zone_defaults_invalid_values(self) -> None:
        self.assertEqual(normalise_needle_contact_zone(None, default=0.2), 0.2)
        self.assertEqual(normalise_needle_contact_zone("bad", default=0.2), 0.2)
        self.assertEqual(normalise_needle_contact_zone(math.inf, default=0.2), 0.2)
        self.assertEqual(normalise_needle_contact_zone(-0.1, default=0.2), 0.2)
        self.assertEqual(normalise_needle_contact_zone(0.0, default=0.2), 0.0)
        self.assertEqual(normalise_needle_contact_zone("0.125", default=0.2), 0.125)

    def test_needle_target_lowering_for_action_returns_existing_targets(self) -> None:
        self.assertEqual(
            needle_target_lowering_for_action(
                "raise",
                down_lowering_mm=None,
                boundary_lowering=None,
            ),
            0.0,
        )
        self.assertEqual(
            needle_target_lowering_for_action(
                "lift",
                down_lowering_mm=1.0,
                boundary_lowering=0.9,
            ),
            0.9,
        )
        self.assertEqual(
            needle_target_lowering_for_action(
                "lower",
                down_lowering_mm=-0.1,
                boundary_lowering=None,
            ),
            0.0,
        )

    def test_needle_target_lowering_for_action_preserves_error_messages(self) -> None:
        cases = (
            (
                "unknown",
                1.0,
                0.9,
                "Unknown needle action: unknown.",
            ),
            (
                "lift",
                None,
                None,
                "Needle down calibration missing; cannot lift.",
            ),
            (
                "lift",
                1.0,
                None,
                "Needle contact zone is zero; cannot lift.",
            ),
            (
                "lower",
                None,
                None,
                "Needle down calibration missing; cannot lower.",
            ),
        )
        for action, down_lowering, boundary, message in cases:
            with self.subTest(action=action, message=message):
                with self.assertRaisesRegex(ValueError, message):
                    needle_target_lowering_for_action(
                        action,
                        down_lowering_mm=down_lowering,
                        boundary_lowering=boundary,
                    )


if __name__ == "__main__":
    unittest.main()
