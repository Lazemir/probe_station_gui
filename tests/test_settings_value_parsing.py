import math
import unittest

from probe_station_gui.settings_value_parsing import (
    coerce_bool,
    coerce_float,
    coerce_int,
    finite_float,
    normalise_choice,
    positive_float,
)


class SettingsValueParsingTest(unittest.TestCase):
    def test_coerce_bool_preserves_legacy_string_truthiness(self) -> None:
        for value in ("", "0", "false", "off", "no", " FALSE "):
            self.assertFalse(coerce_bool(value, default=True))
        for value in ("1", "true", "yes", "on", "anything"):
            self.assertTrue(coerce_bool(value, default=False))
        self.assertTrue(coerce_bool(None, default=True))
        self.assertFalse(coerce_bool(None, default=False))

    def test_coerce_float_accepts_only_numbers_and_strings(self) -> None:
        self.assertEqual(coerce_float(" 1.25 ", default=9.0), 1.25)
        self.assertEqual(coerce_float(2, default=9.0), 2.0)
        self.assertEqual(coerce_float(object(), default=9.0), 9.0)
        self.assertEqual(coerce_float("bad", default=9.0), 9.0)

    def test_coerce_int_truncates_float_strings(self) -> None:
        self.assertEqual(coerce_int("2.9", default=7), 2)
        self.assertEqual(coerce_int(3.8, default=7), 3)
        self.assertEqual(coerce_int("bad", default=7), 7)

    def test_float_guards_reject_non_finite_or_non_positive_values(self) -> None:
        self.assertEqual(finite_float("inf", default=4.0), 4.0)
        self.assertEqual(finite_float(math.nan, default=4.0), 4.0)
        self.assertEqual(positive_float("0", default=4.0), 4.0)
        self.assertEqual(positive_float("-1", default=4.0), 4.0)
        self.assertEqual(positive_float("1.5", default=4.0), 1.5)

    def test_normalise_choice_returns_canonical_choice_text(self) -> None:
        self.assertEqual(
            normalise_choice(" slow ", choices=("FAST", "SLOW"), default="FAST"),
            "SLOW",
        )
        self.assertEqual(
            normalise_choice("50", choices=(10, 50, 100), default="10"),
            "50",
        )
        self.assertEqual(
            normalise_choice(50, choices=(10, 50, 100), default="10"),
            "10",
        )


if __name__ == "__main__":
    unittest.main()
