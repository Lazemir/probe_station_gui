from probe_station_gui.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
    normalize_objective_name,
    ordered_objective_names,
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
