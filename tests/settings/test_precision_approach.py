from __future__ import annotations

import pytest

from probe_station_gui.settings.precision_approach import (
    PRECISION_APPROACH_AXES,
    PrecisionApproachProfile,
    PrecisionApproachSettings,
    parse_precision_approach_settings,
)
from probe_station_gui.settings.manager import Settings


def test_precision_approach_defaults_cover_every_stage_axis() -> None:
    settings = PrecisionApproachSettings()

    assert PRECISION_APPROACH_AXES == ("X", "Y", "Z", "A", "B", "C")
    assert tuple(settings.profiles) == PRECISION_APPROACH_AXES
    assert settings.profiles["A"] == PrecisionApproachProfile(
        enabled=False,
        backlash=0.0,
        final_direction=-1,
    )
    assert settings.profiles["Z"] == PrecisionApproachProfile(
        enabled=True,
        backlash=0.03,
        final_direction=1,
    )
    for axis in ("X", "Y", "B", "C"):
        assert settings.profiles[axis] == PrecisionApproachProfile()


@pytest.mark.parametrize("backlash", [-0.001, float("inf"), float("nan")])
def test_precision_approach_profile_rejects_invalid_backlash(backlash: float) -> None:
    with pytest.raises(ValueError, match="backlash"):
        PrecisionApproachProfile(backlash=backlash)


def test_precision_approach_profile_rejects_invalid_final_direction() -> None:
    with pytest.raises(ValueError, match="final_direction"):
        PrecisionApproachProfile(final_direction=0)


def test_precision_approach_parser_merges_partial_and_invalid_values_with_defaults() -> None:
    parsed = parse_precision_approach_settings(
        {
            "A": {
                "enabled": "yes",
                "backlash": "0.125",
                "final_direction": "+",
            },
            "Z": {
                "enabled": "false",
                "backlash": -2.0,
                "final_direction": "sideways",
            },
            "Q": {
                "enabled": True,
                "backlash": 9.0,
                "final_direction": -1,
            },
        }
    )

    assert parsed.profiles["A"] == PrecisionApproachProfile(True, 0.125, 1)
    assert parsed.profiles["Z"] == PrecisionApproachProfile(False, 0.03, 1)
    assert "Q" not in parsed.profiles
    assert parsed.profiles["X"] == PrecisionApproachProfile()


def test_precision_approach_settings_clone_and_json_are_independent() -> None:
    settings = PrecisionApproachSettings()
    clone = settings.clone()

    clone.profiles["X"] = PrecisionApproachProfile(True, 0.2, -1)

    assert settings.profiles["X"] == PrecisionApproachProfile()
    assert clone.to_dict()["X"] == {
        "enabled": True,
        "backlash": 0.2,
        "final_direction": -1,
    }
    assert clone.fingerprint_payload() == (
        ("X", True, 0.2, -1),
        ("Y", False, 0.0, 1),
        ("Z", True, 0.03, 1),
        ("A", False, 0.0, -1),
        ("B", False, 0.0, 1),
        ("C", False, 0.0, 1),
    )


def test_application_settings_clone_and_serialize_precision_profiles() -> None:
    settings = Settings()
    clone = settings.clone()

    clone.precision_approach.profiles["B"] = PrecisionApproachProfile(
        True,
        0.75,
        -1,
    )

    assert settings.precision_approach.profiles["B"] == PrecisionApproachProfile()
    assert clone.to_dict()["precision_approach"]["B"] == {
        "enabled": True,
        "backlash": 0.75,
        "final_direction": -1,
    }
