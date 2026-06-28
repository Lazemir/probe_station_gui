import math
from types import SimpleNamespace
import unittest

from probe_station_gui.route.contact_seek import (
    contact_seek_attempts,
    contact_seek_attempt_number,
    contact_seek_confirmation_failed_status,
    contact_seek_confirming_status,
    contact_seek_current_position_status,
    contact_seek_depth_status,
    contact_seek_depths,
    contact_seek_found_detail,
    contact_seek_lowering_status,
    contact_seek_not_found_message,
    contact_seek_start_decision,
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

    def test_contact_seek_attempts_track_depth_and_incremental_adjustment(
        self,
    ) -> None:
        attempts = contact_seek_attempts(-0.001, 0.0025)

        self.assertEqual(
            [
                (
                    attempt.previous_depth_mm,
                    attempt.depth_mm,
                    attempt.attempt_number,
                    attempt.max_attempts,
                )
                for attempt in attempts
            ],
            [
                (0.0, 0.001, 1, 3),
                (0.001, 0.002, 2, 3),
                (0.002, 0.0025, 3, 3),
            ],
        )
        self.assertEqual(
            [attempt.adjust_delta_mm(-0.001) for attempt in attempts],
            [-0.001, -0.001, -0.0005],
        )
        self.assertEqual(contact_seek_attempts(-0.001, 0.0), ())

    def test_manual_contact_seek_start_decision_preserves_guard_messages(self) -> None:
        self.assertEqual(
            contact_seek_start_decision(
                contact_seek_active=True,
                route_measurement_active=False,
                instrument_connected=True,
            ).status_message,
            "Contact seek is already running.",
        )
        self.assertEqual(
            contact_seek_start_decision(
                contact_seek_active=False,
                route_measurement_active=True,
                instrument_connected=True,
            ).status_message,
            "Stop route measurement before contact seek.",
        )
        disconnected = contact_seek_start_decision(
            contact_seek_active=False,
            route_measurement_active=False,
            instrument_connected=False,
        )
        self.assertFalse(disconnected.accepted)
        self.assertEqual(
            disconnected.status_message,
            "Connect the measurement instrument before contact seek.",
        )
        self.assertEqual(
            disconnected.calibration_window_result,
            "Measurement instrument is not connected.",
        )
        self.assertTrue(
            contact_seek_start_decision(
                contact_seek_active=False,
                route_measurement_active=False,
                instrument_connected=True,
            ).accepted
        )

    def test_manual_contact_seek_status_messages_match_existing_copy(self) -> None:
        quality = SimpleNamespace(
            status="good",
            median_ohm=12.5,
            mad_sigma_ohm=0.2,
            p95_abs_step_ohm=0.7,
        )
        attempt = contact_seek_attempts(-0.001, 0.002)[1]

        self.assertEqual(
            contact_seek_current_position_status(quality),
            "Contact seek: current position good, median=12.5 Ohm.",
        )
        self.assertEqual(
            contact_seek_lowering_status(attempt),
            "Contact seek: lowering A 2/2.",
        )
        self.assertEqual(
            contact_seek_depth_status(0.002, quality),
            (
                "Contact seek: 0.0020 mm down, good, "
                "median=12.5 Ohm, MAD=200 mOhm."
            ),
        )
        self.assertEqual(
            contact_seek_confirming_status(250),
            "Contact seek: confirming stable contact with 250 readings.",
        )
        self.assertEqual(
            contact_seek_confirmation_failed_status(quality),
            "Contact seek: quick check was good, confirmation failed (good).",
        )
        self.assertEqual(
            contact_seek_found_detail("current position", 0.0, quality),
            (
                "current position; moved 0.0000 mm; median=12.5 Ohm, "
                "MAD=200 mOhm, p95 step=700 mOhm."
            ),
        )
        self.assertEqual(
            contact_seek_not_found_message(0.02),
            "Contact seek did not find a stable contact within 0.020 mm.",
        )


if __name__ == "__main__":
    unittest.main()
