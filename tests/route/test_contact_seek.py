import math
import unittest

from probe_station_gui.route.contact_seek import (
    contact_seek_attempt_number,
    contact_seek_depths,
    normalize_contact_seek_limit,
    normalize_contact_seek_step,
)


class RouteContactSeekTest(unittest.TestCase):
    def test_normalize_contact_seek_step_is_always_negative_and_nonzero(self) -> None:
        self.assertEqual(
            normalize_contact_seek_step(0.001, default_step_mm=0.002),
            -0.001,
        )
        self.assertEqual(
            normalize_contact_seek_step(-0.001, default_step_mm=0.002),
            -0.001,
        )
        self.assertEqual(
            normalize_contact_seek_step(0.0, default_step_mm=0.002),
            -0.002,
        )
        self.assertEqual(
            normalize_contact_seek_step("bad", default_step_mm=0.002),
            -0.002,
        )
        self.assertEqual(
            normalize_contact_seek_step(math.nan, default_step_mm=0.002),
            -0.002,
        )

    def test_normalize_contact_seek_limit_clamps_invalid_values_to_zero(self) -> None:
        self.assertEqual(
            normalize_contact_seek_limit("0.003", default_limit_mm=0.002),
            0.003,
        )
        self.assertEqual(
            normalize_contact_seek_limit("bad", default_limit_mm=0.002),
            0.002,
        )
        self.assertEqual(
            normalize_contact_seek_limit(-1.0, default_limit_mm=0.002),
            0.0,
        )
        self.assertEqual(
            normalize_contact_seek_limit(math.inf, default_limit_mm=0.002),
            0.0,
        )

    def test_contact_seek_depths_step_to_limit_with_final_clamp(self) -> None:
        self.assertEqual(
            contact_seek_depths(-0.001, 0.003),
            (0.001, 0.002, 0.003),
        )
        self.assertEqual(
            contact_seek_depths(-0.001, 0.0025),
            (0.001, 0.002, 0.0025),
        )
        self.assertEqual(contact_seek_depths(-0.001, 0.0), ())
        self.assertEqual(contact_seek_depths(0.0, 0.003), ())

    def test_contact_seek_attempt_number_counts_depth_steps(self) -> None:
        self.assertEqual(contact_seek_attempt_number(0.0, -0.001), 1)
        self.assertEqual(contact_seek_attempt_number(0.001, -0.001), 1)
        self.assertEqual(contact_seek_attempt_number(0.0015, -0.001), 2)
        self.assertEqual(contact_seek_attempt_number(0.003, -0.001), 3)
        self.assertEqual(contact_seek_attempt_number(0.003, 0.0), 1)


if __name__ == "__main__":
    unittest.main()
