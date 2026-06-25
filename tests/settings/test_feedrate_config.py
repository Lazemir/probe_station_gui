import unittest

from probe_station_gui.settings.feedrate_config import (
    FeedrateGroup,
    FeedrateSettings,
    parse_feedrate_groups,
)


def test_feedrate_settings_clone_copies_groups() -> None:
    settings = FeedrateSettings(
        linear=FeedrateGroup(presets=[10.0, 20.0], default=10.0),
        rotary=FeedrateGroup(presets=[5.0], default=5.0),
    )

    clone = settings.clone()
    clone.linear.presets.append(30.0)

    assert settings.linear.presets == [10.0, 20.0]
    assert clone.linear.presets == [10.0, 20.0, 30.0]


class FeedrateConfigTest(unittest.TestCase):
    def test_legacy_presets_seed_rotary_group_when_current_layout_is_missing(self) -> None:
        linear, rotary = parse_feedrate_groups(
            None,
            [12.0, "24.0"],
            linear_group="linear",
            rotary_group="rotary",
            linear_defaults=(1.0, 2.0),
            rotary_defaults=(90.0,),
            default_feedrate=1.0,
            min_feedrate=1.0,
        )

        self.assertEqual(linear.presets, [12.0, 24.0])
        self.assertEqual(rotary.presets, [12.0, 24.0])
        self.assertEqual(linear.default, 1.0)
        self.assertEqual(rotary.default, 1.0)

    def test_preserves_continuous_default_that_is_not_a_preset(self) -> None:
        linear, rotary = parse_feedrate_groups(
            {
                "linear": {"presets": [1.0, 10.0], "default": 4.5},
                "rotary": {"presets": [30.0, 90.0], "default": 45.0},
            },
            [],
            linear_group="linear",
            rotary_group="rotary",
            linear_defaults=(1.0,),
            rotary_defaults=(90.0,),
            default_feedrate=1.0,
            min_feedrate=1.0,
        )

        self.assertEqual(linear.presets, [1.0, 10.0])
        self.assertEqual(linear.default, 4.5)
        self.assertEqual(rotary.presets, [30.0, 90.0])
        self.assertEqual(rotary.default, 45.0)

    def test_clamps_legacy_sub_one_presets_and_defaults(self) -> None:
        linear, rotary = parse_feedrate_groups(
            {
                "linear": {"presets": [0.1, 1.0, 3.0], "default": 0.1},
                "rotary": {"presets": [0.1, 1.0, 90.0], "default": 0.1},
            },
            [],
            linear_group="linear",
            rotary_group="rotary",
            linear_defaults=(1.0,),
            rotary_defaults=(90.0,),
            default_feedrate=1.0,
            min_feedrate=1.0,
        )

        self.assertEqual(linear.presets, [1.0, 3.0])
        self.assertEqual(linear.default, 1.0)
        self.assertEqual(rotary.presets, [1.0, 90.0])
        self.assertEqual(rotary.default, 1.0)


if __name__ == "__main__":
    unittest.main()
