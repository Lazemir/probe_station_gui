from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
    normalize_objective_name,
    ordered_objective_names,
    parse_objectives_settings,
    parse_pixels_to_mm_matrix,
)


def test_objective_profile_clone_copies_pixels_to_mm_matrix() -> None:
    profile = ObjectiveCalibrationSettings(
        name="X20",
        pixels_to_mm=[[1.0, 0.0], [0.0, 2.0]],
        xy_calibration_configured=True,
    )

    restored = profile.clone()
    restored.pixels_to_mm[0][0] = 3.0

    assert profile.pixels_to_mm == [[1.0, 0.0], [0.0, 2.0]]
    assert restored.pixels_to_mm == [[3.0, 0.0], [0.0, 2.0]]


def test_objective_profile_clone_copies_distortion_payload() -> None:
    profile = ObjectiveCalibrationSettings(
        name="X50",
        distortion_correction={
            "model_version": 1,
            "frame_size": [640, 480],
            "source_points": [[10.0, 20.0], [30.0, 40.0]],
        },
        distortion_correction_configured=True,
    )

    restored = profile.clone()
    restored.distortion_correction["frame_size"][0] = 320
    restored.distortion_correction["source_points"][0][0] = 99.0

    assert profile.distortion_correction["frame_size"] == [640, 480]
    assert profile.distortion_correction["source_points"][0] == [10.0, 20.0]
    assert restored.distortion_correction["frame_size"] == [320, 480]
    assert restored.distortion_correction["source_points"][0] == [99.0, 20.0]


def test_objectives_settings_orders_builtin_profiles_before_custom_profiles() -> None:
    settings = ObjectivesSettings(
        objectives={
            "X20": ObjectiveCalibrationSettings(name="X20"),
            "CUSTOM": ObjectiveCalibrationSettings(name="CUSTOM"),
            "X5": ObjectiveCalibrationSettings(name="X5"),
        }
    )

    assert ordered_objective_names(settings.objectives) == ["X5", "X20", "CUSTOM"]


def test_normalize_objective_name_strips_spaces_and_rejects_bad_names() -> None:
    assert normalize_objective_name(" x 20 ") == "X20"
    assert normalize_objective_name("x/20") == ""


def test_parse_pixels_to_mm_matrix_accepts_valid_2x2_matrix() -> None:
    assert parse_pixels_to_mm_matrix([[0.001, 0.0], [0.0, 0.0012]]) == [
        [0.001, 0.0],
        [0.0, 0.0012],
    ]


def test_parse_pixels_to_mm_matrix_rejects_singular_matrix() -> None:
    assert parse_pixels_to_mm_matrix([[1.0, 2.0], [2.0, 4.0]]) == []


def test_parse_pixels_to_mm_matrix_rejects_nonfinite_values() -> None:
    assert parse_pixels_to_mm_matrix([[1.0, "nan"], [0.0, 1.0]]) == []


def test_parse_pixels_to_mm_matrix_preserves_tiny_valid_determinant() -> None:
    assert parse_pixels_to_mm_matrix([[1e-8, 0.0], [0.0, 1e-8]]) == [
        [1e-08, 0.0],
        [0.0, 1e-08],
    ]


def test_parse_objectives_rejects_invalid_matrix() -> None:
    parsed = parse_objectives_settings(
        {
            "active_name": "x10",
            "objectives": {
                "X10": {
                    "pixels_to_mm": [[1.0, 2.0], [2.0, 4.0]],
                    "xy_calibration_configured": True,
                }
            },
        }
    )

    assert parsed.active_name == "X10"
    assert not parsed.objectives["X10"].xy_calibration_configured
    assert parsed.objectives["X10"].pixels_to_mm == []


def test_parse_objectives_preserves_custom_profile() -> None:
    parsed = parse_objectives_settings(
        {
            "active_name": "x100",
            "objectives": {
                "x100": {
                    "pixels_to_mm": [[0.0001, 0.0], [0.0, 0.00011]],
                    "xy_calibration_configured": True,
                }
            },
        }
    )

    assert parsed.active_name == "X100"
    assert "X100" in parsed.objectives
    assert parsed.objectives["X100"].xy_calibration_configured
    assert parsed.objectives["X100"].pixels_to_mm == [
        [0.0001, 0.0],
        [0.0, 0.00011],
    ]


def test_parse_objectives_preserves_valid_distortion_payload() -> None:
    parsed = parse_objectives_settings(
        {
            "active_name": "x50",
            "objectives": {
                "X50": {
                    "distortion_correction_configured": True,
                    "distortion_correction": {
                        "model_version": 1,
                        "frame_size": [640, 480],
                        "source_points": [[10.0, 20.0], [30.0, 40.0]],
                        "target_points": [[11.0, 21.0], [31.0, 41.0]],
                        "residual_mean_px": 0.2,
                        "residual_max_px": 0.4,
                    },
                }
            },
        }
    )

    profile = parsed.objectives["X50"]
    assert profile.distortion_correction_configured
    assert profile.distortion_correction["frame_size"] == [640, 480]
    assert profile.distortion_correction["source_points"] == [
        [10.0, 20.0],
        [30.0, 40.0],
    ]


def test_parse_objectives_rejects_invalid_distortion_payload() -> None:
    parsed = parse_objectives_settings(
        {
            "active_name": "x50",
            "objectives": {
                "X50": {
                    "distortion_correction_configured": True,
                    "distortion_correction": {
                        "model_version": 1,
                        "frame_size": [640],
                    },
                }
            },
        }
    )

    profile = parsed.objectives["X50"]
    assert not profile.distortion_correction_configured
    assert profile.distortion_correction == {}


def test_parse_objectives_uses_remaining_profile_when_active_was_deleted() -> None:
    parsed = parse_objectives_settings(
        {
            "active_name": "X50",
            "objectives": {
                "X10": {
                    "xy_calibration_configured": False,
                }
            },
        }
    )

    assert parsed.active_name == "X10"
    assert list(parsed.objectives) == ["X10"]
