import unittest

from probe_station_gui.api_server import _axis_targets_from_payload


class ApiServerPayloadTest(unittest.TestCase):
    def test_axis_targets_accept_top_level_axes(self) -> None:
        targets = _axis_targets_from_payload({"x": 1.25, "Y": 2.5})

        self.assertEqual(targets, {"X": 1.25, "Y": 2.5})

    def test_axis_targets_accept_coordinates_map(self) -> None:
        targets = _axis_targets_from_payload(
            {"coordinates": {"x": "1.25", "Z": 3.0}}
        )

        self.assertEqual(targets, {"X": 1.25, "Z": 3.0})

    def test_top_level_axis_overrides_coordinates_map(self) -> None:
        targets = _axis_targets_from_payload(
            {"coordinates": {"X": 1.25, "Y": 2.5}, "x": 9.0}
        )

        self.assertEqual(targets, {"X": 9.0, "Y": 2.5})


if __name__ == "__main__":
    unittest.main()

