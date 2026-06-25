from probe_station_gui.objective_config import parse_pixels_to_mm_matrix


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
