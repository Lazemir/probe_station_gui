import unittest

import pytest

from probe_station_gui.camera.imaging import (
    MicroscopeScaleCalibration,
    build_design_scan_plan,
)


class ScanPlanningCharacterizationTest(unittest.TestCase):
    def test_design_scan_plan_covers_bounds_with_overlap(self) -> None:
        plan = build_design_scan_plan(
            stage_bounds=(0.0, 0.0, 3.0, 2.0),
            fov_size_mm=(1.0, 1.0),
            overlap_fraction=0.2,
        )

        self.assertEqual(plan.column_count, 4)
        self.assertEqual(plan.row_count, 3)
        self.assertEqual(len(plan.tiles), 12)
        self.assertLessEqual(plan.covered_stage_bounds[0], 0.0)
        self.assertLessEqual(plan.covered_stage_bounds[1], 0.0)
        self.assertGreaterEqual(plan.covered_stage_bounds[2], 3.0)
        self.assertGreaterEqual(plan.covered_stage_bounds[3], 2.0)


def test_scale_fallback_preserves_signed_y_and_full_matrix_round_trip() -> None:
    fallback = MicroscopeScaleCalibration(0.5, 0.6)
    assert fallback.pixel_delta_to_stage_mm(10.0, 20.0) == pytest.approx(
        (0.005, -0.012)
    )
    assert fallback.stage_delta_to_pixel(0.005, -0.012) == pytest.approx((10.0, 20.0))

    calibrated = MicroscopeScaleCalibration(
        100.0,
        100.0,
        pixels_to_mm=((0.1, 0.02), (-0.03, -0.11)),
    )
    stage_delta = calibrated.pixel_delta_to_stage_mm(7.25, -3.5)
    assert calibrated.stage_delta_to_pixel(*stage_delta) == pytest.approx((7.25, -3.5))


def test_scale_rejects_a_singular_full_matrix() -> None:
    calibrated = MicroscopeScaleCalibration(
        100.0,
        100.0,
        pixels_to_mm=((0.1, 0.2), (0.2, 0.4)),
    )

    with pytest.raises(ValueError, match="Pixel-to-stage matrix is singular"):
        calibrated.stage_delta_to_pixel(1.0, 2.0)
