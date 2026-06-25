import unittest

from probe_station_gui.settings.oscillation_config import (
    OscillationSettings,
    OscillationSettingsDefaults,
    parse_oscillation_settings,
)


def _defaults() -> OscillationSettingsDefaults:
    return OscillationSettingsDefaults(
        mode="X",
        amplitude_mm=0.5,
        feedrate_mm_min=120.0,
        turns_per_sweep=3.0,
    )


class OscillationConfigTest(unittest.TestCase):
    def test_settings_round_trip_and_clone(self) -> None:
        settings = OscillationSettings(
            mode="SPIRAL",
            amplitude_mm=1.25,
            feedrate_mm_min=250.0,
            turns_per_sweep=4.0,
        )

        restored = OscillationSettings(**settings.to_dict())
        clone = settings.clone()

        self.assertEqual(restored, settings)
        self.assertEqual(clone, settings)
        self.assertIsNot(clone, settings)

    def test_normalises_mode_and_numeric_values(self) -> None:
        parsed = parse_oscillation_settings(
            {
                "mode": " spiral ",
                "amplitude_mm": "0.75",
                "feedrate_mm_min": "240",
                "turns_per_sweep": "4.5",
            },
            _defaults(),
        )

        self.assertEqual(parsed.mode, "SPIRAL")
        self.assertEqual(parsed.amplitude_mm, 0.75)
        self.assertEqual(parsed.feedrate_mm_min, 240.0)
        self.assertEqual(parsed.turns_per_sweep, 4.5)

    def test_invalid_mode_returns_to_default(self) -> None:
        parsed = parse_oscillation_settings({"mode": "Z"}, _defaults())

        self.assertEqual(parsed.mode, "X")

    def test_non_positive_numeric_values_return_to_defaults(self) -> None:
        parsed = parse_oscillation_settings(
            {
                "amplitude_mm": 0,
                "feedrate_mm_min": -1,
                "turns_per_sweep": 0,
            },
            _defaults(),
        )

        self.assertEqual(parsed.amplitude_mm, 0.5)
        self.assertEqual(parsed.feedrate_mm_min, 120.0)
        self.assertEqual(parsed.turns_per_sweep, 3.0)


if __name__ == "__main__":
    unittest.main()
