import logging

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


def test_application_settings_clone_and_parse_software_coordinate_settings() -> None:
    coordinates = SoftwareCoordinateSettings(custom_frames=(_custom(),))
    settings = Settings(software_coordinates=coordinates)
    manager = SettingsManager.__new__(SettingsManager)
    manager._logger = logging.getLogger(__name__)

    clone = settings.clone()
    restored = manager._settings_from_raw(settings.to_dict())

    assert clone.software_coordinates == coordinates
    assert restored.software_coordinates == coordinates
