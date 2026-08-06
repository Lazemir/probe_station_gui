import json
import logging
from importlib import resources

import pytest

from probe_station_gui.settings.software_coordinates import (
    CustomFrameSettings,
    SoftwareCoordinateSettings,
    parse_software_coordinate_settings,
)
from probe_station_gui.settings.manager import Settings, SettingsManager


def _custom(*, z_zero_mm: float = 6.0, a_zero_mm: float = 7.0) -> CustomFrameSettings:
    return CustomFrameSettings(
        frame_id="14c838bd-a9a5-47bd-9d22-6326ca63c469",
        name="fixture",
        origin_x_mm=1.0,
        origin_y_mm=2.0,
        reference_b_deg=3.0,
        xy_angle_deg=4.0,
        b_zero_deg=5.0,
        z_zero_mm=z_zero_mm,
        a_zero_mm=a_zero_mm,
    )


def test_software_coordinate_defaults_use_assumed_machine_zero_pivot() -> None:
    parsed = parse_software_coordinate_settings({})

    assert parsed.pivot.x_mm == 0.0
    assert parsed.pivot.y_mm == 0.0
    assert parsed.pivot.source == "assumed"
    assert parsed.last_selected_frame_id == "machine"
    assert parsed.selection_generation == 0


def test_bundled_software_coordinate_defaults_match_current_schema() -> None:
    resource = resources.files("probe_station_gui").joinpath("default_settings.json")
    with resource.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)["software_coordinates"]

    assert raw == SoftwareCoordinateSettings().to_dict()


def test_software_coordinate_selection_generation_round_trips() -> None:
    settings = SoftwareCoordinateSettings(
        last_selected_frame_id="14c838bd-a9a5-47bd-9d22-6326ca63c469",
        selection_generation=17,
    )

    restored = parse_software_coordinate_settings(settings.to_dict())

    assert restored.selection_generation == 17


def test_invalid_or_legacy_selection_generation_migrates_to_zero() -> None:
    for raw_generation in (None, -1, True, "7"):
        raw = {"last_selected_frame_id": "machine"}
        if raw_generation is not None:
            raw["selection_generation"] = raw_generation

        parsed = parse_software_coordinate_settings(raw)

        assert parsed.selection_generation == 0


def test_custom_frame_round_trip_keeps_stable_id_and_physical_values() -> None:
    frame = _custom()
    settings = SoftwareCoordinateSettings(custom_frames=(frame,))

    restored = parse_software_coordinate_settings(settings.to_dict())

    assert restored == settings


def test_changing_custom_z_clears_a_even_when_new_a_is_supplied() -> None:
    original = _custom(z_zero_mm=1.0, a_zero_mm=2.0)

    changed = original.apply_geometry_edit(z_zero_mm=3.0, a_zero_mm=4.0)

    assert changed.z_zero_mm == 3.0
    assert changed.a_zero_mm is None


def test_parser_skips_one_invalid_custom_record_without_losing_valid_record() -> None:
    raw = SoftwareCoordinateSettings(custom_frames=(_custom(),)).to_dict()
    raw["custom_frames"].append({"frame_id": "bad", "name": "broken"})

    parsed = parse_software_coordinate_settings(raw)

    assert len(parsed.custom_frames) == 1
    assert len(parsed.diagnostics) == 1


def test_future_settings_schema_is_not_materialized_and_round_trips_losslessly() -> None:
    raw = SoftwareCoordinateSettings(custom_frames=(_custom(),)).to_dict()
    raw["version"] = 99
    raw["future_geometry"] = {"mode": "curved"}

    parsed = parse_software_coordinate_settings(raw)

    assert parsed.custom_frames == ()
    assert parsed.pivot is None
    assert parsed.degraded
    assert "version" in parsed.diagnostics[0].lower()
    assert parsed.to_dict() == raw


def test_settings_model_rejects_unsupported_schema_version() -> None:
    with pytest.raises(ValueError, match="supported version"):
        SoftwareCoordinateSettings(version=2)


def test_missing_legacy_pivot_defaults_but_present_malformed_pivot_is_unavailable() -> None:
    legacy = parse_software_coordinate_settings({})
    raw = SoftwareCoordinateSettings().to_dict()
    raw["pivot"]["x_mm"] = "not-a-number"

    malformed = parse_software_coordinate_settings(raw)

    assert (legacy.pivot.x_mm, legacy.pivot.y_mm) == (0.0, 0.0)
    assert malformed.pivot is None
    assert malformed.degraded
    assert "pivot" in malformed.diagnostics[0].lower()
    assert malformed.to_dict()["pivot"] == raw["pivot"]


def test_numeric_strings_are_not_accepted_as_current_pivot_schema() -> None:
    raw = SoftwareCoordinateSettings().to_dict()
    raw["pivot"]["x_mm"] = "1.25"

    parsed = parse_software_coordinate_settings(raw)

    assert parsed.pivot is None
    assert parsed.to_dict()["pivot"] == raw["pivot"]


def test_malformed_custom_numeric_fields_are_preserved_not_coerced() -> None:
    raw = SoftwareCoordinateSettings(custom_frames=(_custom(),)).to_dict()
    raw_frame = raw["custom_frames"][0]
    raw_frame["origin_x_mm"] = "1.0"

    parsed = parse_software_coordinate_settings(raw)

    assert parsed.custom_frames == ()
    assert parsed.to_dict()["custom_frames"] == [raw_frame]


def test_malformed_custom_frame_collection_preserves_the_whole_section() -> None:
    raw = SoftwareCoordinateSettings().to_dict()
    raw["custom_frames"] = {"future_collection": []}

    parsed = parse_software_coordinate_settings(raw)

    assert parsed.custom_frames == ()
    assert parsed.pivot is None
    assert parsed.degraded
    assert parsed.to_dict() == raw


def test_unknown_current_root_fields_degrade_and_preserve_the_whole_section() -> None:
    raw = SoftwareCoordinateSettings().to_dict()
    raw["future_selection_policy"] = {"scope": "operator"}

    parsed = parse_software_coordinate_settings(raw)

    assert parsed.custom_frames == ()
    assert parsed.pivot is None
    assert parsed.degraded
    assert parsed.to_dict() == raw


def test_current_root_numeric_strings_are_preserved_not_coerced() -> None:
    raw = SoftwareCoordinateSettings().to_dict()
    raw["max_rotation_segment_deg"] = "0.5"

    parsed = parse_software_coordinate_settings(raw)

    assert parsed.pivot is None
    assert parsed.degraded
    assert parsed.to_dict() == raw


def test_parser_rejects_and_preserves_a_origin_without_z_origin() -> None:
    raw = SoftwareCoordinateSettings(custom_frames=(_custom(),)).to_dict()
    raw_frame = raw["custom_frames"][0]
    raw_frame["z_zero_mm"] = None
    raw_frame["a_zero_mm"] = 7.0

    parsed = parse_software_coordinate_settings(raw)

    assert parsed.custom_frames == ()
    assert parsed.degraded
    assert "requires" in parsed.diagnostics[0].lower()
    assert parsed.to_dict()["custom_frames"] == [raw_frame]


def test_custom_frame_model_rejects_a_origin_without_z_origin() -> None:
    with pytest.raises(ValueError, match="A origin requires a Z origin"):
        _custom(z_zero_mm=None, a_zero_mm=7.0)


def test_application_settings_clone_and_parse_software_coordinate_settings() -> None:
    coordinates = SoftwareCoordinateSettings(custom_frames=(_custom(),))
    settings = Settings(software_coordinates=coordinates)
    manager = SettingsManager.__new__(SettingsManager)
    manager._logger = logging.getLogger(__name__)

    clone = settings.clone()
    restored = manager._settings_from_raw(settings.to_dict())

    assert clone.software_coordinates == coordinates
    assert restored.software_coordinates == coordinates
