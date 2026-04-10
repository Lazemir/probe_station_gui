import importlib.util
import sys
import unittest
from pathlib import Path


def _load_driver_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "probe_station_gui"
        / "gwinstek_lcr_76200.py"
    )
    spec = importlib.util.spec_from_file_location("gwinstek_lcr_76200_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver_module = _load_driver_module()


class GWInstekLCR76200ParsingTest(unittest.TestCase):
    def test_normalize_resource_name(self) -> None:
        self.assertEqual(
            driver_module.normalize_resource_name("COM4"),
            "ASRL4::INSTR",
        )
        self.assertEqual(
            driver_module.normalize_resource_name("ASRL9::INSTR"),
            "ASRL9::INSTR",
        )

    def test_parse_fetch_response_with_comparator_tail(self) -> None:
        reading = driver_module.parse_fetch_response(
            "+8.94983e-12,+1.11930e-01,OUT ,AUX-OK,NG"
        )

        self.assertAlmostEqual(reading.primary, 8.94983e-12)
        self.assertAlmostEqual(reading.secondary, 1.11930e-01)
        self.assertEqual(reading.monitor1, None)
        self.assertEqual(reading.comparator_tokens, ("OUT", "AUX-OK", "NG"))

    def test_parse_fetch_response_for_dcr_main(self) -> None:
        reading = driver_module.parse_fetch_response("+1.23434e+05")

        self.assertAlmostEqual(reading.resistance_ohm, 1.23434e5)
        self.assertEqual(reading.secondary, None)
        self.assertEqual(reading.comparator_tokens, ())

    def test_parse_bias_response(self) -> None:
        self.assertIsNone(driver_module._parse_bias_response("OFF"))
        self.assertAlmostEqual(driver_module._parse_bias_response("+2.50V"), 2.5)

    def test_parse_idn_response(self) -> None:
        idn = driver_module.parse_idn_response(
            "LCR-76200,REV E8.13,GEY894701,Good Will Instrument Co., Ltd."
        )

        self.assertEqual(idn["model"], "LCR-76200")
        self.assertEqual(idn["firmware"], "REV E8.13")
        self.assertEqual(idn["serial"], "GEY894701")
        self.assertEqual(idn["vendor"], "Good Will Instrument Co., Ltd.")


if __name__ == "__main__":
    unittest.main()
