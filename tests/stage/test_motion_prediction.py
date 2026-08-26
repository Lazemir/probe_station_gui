from __future__ import annotations

import pytest

from probe_station_gui.stage import motion_prediction


@pytest.mark.parametrize(
    "value",
    [None, (), (1.0,), ("bad", 2.0), (float("nan"), 2.0), (1.0, float("inf"))],
)
def test_finite_xy_coercion_rejects_invalid_values(value: object) -> None:
    assert motion_prediction.coerce_finite_xy(value) is None


def test_finite_xy_coercion_normalizes_a_valid_pair() -> None:
    assert motion_prediction.coerce_finite_xy([1, "2.5"]) == (1.0, 2.5)


def test_actual_start_match_requires_finite_values_within_tolerance() -> None:
    pending = (4.0, 6.0)

    assert motion_prediction.matching_planned_xy_start(
        4.00005,
        6.0,
        300.0,
        pending_target=pending,
        tolerance_mm=1e-4,
    ) == ((4.00005, 6.0), 300.0)
    assert (
        motion_prediction.matching_planned_xy_start(
            4.001,
            6.0,
            300.0,
            pending_target=pending,
            tolerance_mm=1e-4,
        )
        is None
    )
    assert (
        motion_prediction.matching_planned_xy_start(
            4.0,
            6.0,
            float("nan"),
            pending_target=pending,
            tolerance_mm=1e-4,
        )
        is None
    )


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ((4.00005, 6.0), True),
        ((4.001, 6.0), False),
        ((float("nan"), 6.0), False),
    ],
)
def test_planned_xy_match_uses_finite_euclidean_tolerance(
    candidate: object,
    expected: bool,
) -> None:
    assert (
        motion_prediction.planned_xy_matches(
            candidate,
            (4.0, 6.0),
            tolerance_mm=1e-4,
        )
        is expected
    )


def test_planned_xy_timing_applies_feedrate_floor_and_padding() -> None:
    timing = motion_prediction.planned_xy_timing(
        (1.0, 2.0),
        (4.0, 6.0),
        300.0,
        min_feedrate_mm_min=1.0,
        duration_padding_s=0.12,
        started_at=10.0,
    )

    assert timing == pytest.approx((10.0, 11.12))
    assert motion_prediction.planned_xy_timing(
        (1.0, 2.0),
        (2.0, 2.0),
        0.5,
        min_feedrate_mm_min=1.0,
        duration_padding_s=0.12,
        started_at=10.0,
    ) == pytest.approx((10.0, 70.12))


@pytest.mark.parametrize(
    ("origin", "target", "feedrate"),
    [((1.0, 2.0), (1.0, 2.0), 300.0), ((1.0, 2.0), (2.0, 2.0), 0.0)],
)
def test_planned_xy_timing_rejects_non_motion(
    origin: tuple[float, float],
    target: tuple[float, float],
    feedrate: float,
) -> None:
    assert (
        motion_prediction.planned_xy_timing(
            origin,
            target,
            feedrate,
            min_feedrate_mm_min=0.0,
            duration_padding_s=0.12,
            started_at=10.0,
        )
        is None
    )


@pytest.mark.parametrize(
    (
        "planned_xy",
        "prediction_active",
        "waiting_for_fresh_status",
        "status_timestamp",
        "stop_status_timestamp",
        "expected",
    ),
    [
        (None, True, True, 11.0, 10.0, None),
        ((4.0, 6.0), True, False, 11.0, 10.0, (4.0, 6.0)),
        ((4.0, 6.0), False, True, 11.0, None, (4.0, 6.0)),
        ((4.0, 6.0), False, True, 10.0, 10.0, (4.0, 6.0)),
        ((4.0, 6.0), False, True, 10.1, 10.0, None),
        ((4.0, 6.0), False, False, 10.0, 10.0, None),
    ],
)
def test_planned_xy_status_hold_requires_a_strictly_newer_report(
    planned_xy: tuple[float, float] | None,
    prediction_active: bool,
    waiting_for_fresh_status: bool,
    status_timestamp: float | None,
    stop_status_timestamp: float | None,
    expected: tuple[float, float] | None,
) -> None:
    assert (
        motion_prediction.held_planned_xy(
            planned_xy,
            prediction_active=prediction_active,
            waiting_for_fresh_status=waiting_for_fresh_status,
            status_timestamp=status_timestamp,
            stop_status_timestamp=stop_status_timestamp,
        )
        == expected
    )


def test_stage_motion_prediction_has_no_application_dependency() -> None:
    source = motion_prediction.__file__
    assert source is not None
    assert (
        "probe_station_gui.application"
        not in open(
            source,
            encoding="utf-8",
        ).read()
    )
