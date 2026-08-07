from __future__ import annotations

import math

import pytest

from probe_station_gui.design.focus_candidate import select_central_focus_candidate


def test_candidate_is_near_design_center_and_fits_current_fov() -> None:
    candidate = select_central_focus_candidate(
        design_bounds=(0.0, 0.0, 100.0, 100.0),
        structure_bounds=(
            (48.0, 48.0, 52.0, 52.0),
            (10.0, 10.0, 30.0, 30.0),
        ),
        fov_size=(10.0, 10.0),
    )

    assert candidate is not None
    assert candidate.center == (50.0, 50.0)
    assert candidate.bounds == (48.0, 48.0, 52.0, 52.0)
    assert candidate.distance_from_design_center == 0.0


def test_candidate_skips_empty_non_finite_and_oversized_bounds() -> None:
    candidate = select_central_focus_candidate(
        design_bounds=(0.0, 0.0, 100.0, 100.0),
        structure_bounds=(
            (50.0, 50.0, 50.0, 51.0),
            (0.0, 0.0, math.inf, 1.0),
            (40.0, 40.0, 60.0, 60.0),
        ),
        fov_size=(10.0, 10.0),
    )

    assert candidate is None


def test_candidate_ties_choose_smaller_area_then_bounds() -> None:
    candidate = select_central_focus_candidate(
        design_bounds=(0.0, 0.0, 100.0, 100.0),
        structure_bounds=(
            (54.0, 49.0, 56.0, 51.0),
            (44.0, 48.0, 46.0, 52.0),
            (44.0, 49.0, 46.0, 51.0),
        ),
        fov_size=(10.0, 10.0),
    )

    assert candidate is not None
    assert candidate.bounds == (44.0, 49.0, 46.0, 51.0)


@pytest.mark.parametrize(
    ("design_bounds", "fov_size"),
    [
        ((0.0, 0.0, 0.0, 1.0), (1.0, 1.0)),
        ((0.0, 0.0, 1.0, 1.0), (0.0, 1.0)),
        ((0.0, 0.0, 1.0, math.nan), (1.0, 1.0)),
    ],
)
def test_candidate_rejects_invalid_design_or_fov(
    design_bounds: tuple[float, float, float, float],
    fov_size: tuple[float, float],
) -> None:
    with pytest.raises(ValueError):
        select_central_focus_candidate(
            design_bounds=design_bounds,
            structure_bounds=((0.0, 0.0, 1.0, 1.0),),
            fov_size=fov_size,
        )


@pytest.mark.parametrize(
    "fov_size",
    [
        (-10.0, 10.0),
        (10.0, -10.0),
        (0.0, 10.0),
        (10.0, 0.0),
    ],
)
def test_candidate_rejects_non_positive_fov_dimensions(
    fov_size: tuple[float, float],
) -> None:
    with pytest.raises(ValueError, match="Field of view"):
        select_central_focus_candidate(
            design_bounds=(0.0, 0.0, 100.0, 100.0),
            structure_bounds=((48.0, 48.0, 52.0, 52.0),),
            fov_size=fov_size,
        )
