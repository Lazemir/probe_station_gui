import unittest
from unittest.mock import patch

import numpy as np
import pytest
from PySide6.QtGui import QImage

from probe_station_gui.camera.imaging import (
    MicroscopeScaleCalibration,
    MicroscopeScanTile,
)
from probe_station_gui.camera.scan_scale_refinement import (
    _phase_overlap_shift,
    _refine_scan_tile_pixel_placements,
    refine_scan_scale_from_tile_overlaps,
)


def test_overlap_refinement_solves_the_full_pixel_to_stage_matrix() -> None:
    image = QImage(200, 200, QImage.Format_RGB888)
    image.fill(0)
    scale = MicroscopeScaleCalibration(
        pixel_size_x_um=0.118,
        pixel_size_y_um=0.111,
        source="test",
        pixels_to_mm=((-0.000118, 0.0), (0.0, -0.000111)),
    )
    tiles = (
        (MicroscopeScanTile(1, 0, 0, (0.0, 0.0)), image),
        (MicroscopeScanTile(2, 0, 1, (0.0116, 0.0)), image),
        (MicroscopeScanTile(3, 1, 0, (0.0, -0.0116)), image),
    )
    shifts = iter(((-1.6949152542, 0.0, 0.5), (0.0, 4.5045045045, 0.5)))

    with patch(
        "probe_station_gui.camera.scan_scale_refinement._phase_overlap_shift",
        lambda *_args: next(shifts),
    ):
        refined = refine_scan_scale_from_tile_overlaps(tiles, scale)

    assert refined is not scale
    assert refined.pixels_to_mm is not None
    assert refined.pixels_to_mm[0][0] == pytest.approx(-0.000116, abs=1e-9)
    assert refined.pixels_to_mm[1][1] == pytest.approx(-0.000116, abs=1e-9)
    assert "scan-overlap" in refined.source


def test_registration_does_not_consider_diagonal_tiles() -> None:
    import numpy as np

    first = MicroscopeScanTile(1, 0, 0, (0.0, 0.0))
    diagonal = MicroscopeScanTile(2, 1, 1, (1.0, 1.0))
    placements = [
        (first, 0.0, 0.0, np.zeros((20, 20, 3), dtype=np.uint8)),
        (diagonal, 18.0, 18.0, np.zeros((20, 20, 3), dtype=np.uint8)),
    ]

    with patch(
        "probe_station_gui.camera.scan_scale_refinement._estimate_overlap_registration",
        return_value=(8.0, 8.0, 1.0),
    ) as estimate:
        refined = _refine_scan_tile_pixel_placements(placements)

    estimate.assert_not_called()
    assert refined[1][1:3] == (18.0, 18.0)


class ScanScaleRefinementCharacterizationTest(unittest.TestCase):
    def test_refine_scan_scale_from_tile_overlaps_solves_global_matrix(self) -> None:
        from PySide6.QtGui import QImage

        image = QImage(200, 200, QImage.Format_RGB888)
        image.fill(0)
        scale = MicroscopeScaleCalibration(
            pixel_size_x_um=0.118,
            pixel_size_y_um=0.111,
            source="test",
            pixels_to_mm=(
                (-0.000118, 0.0),
                (0.0, -0.000111),
            ),
        )
        tiles = (
            (
                MicroscopeScanTile(1, 0, 0, (0.0, 0.0)),
                image,
            ),
            (
                MicroscopeScanTile(2, 0, 1, (0.0116, 0.0)),
                image,
            ),
            (
                MicroscopeScanTile(3, 1, 0, (0.0, -0.0116)),
                image,
            ),
        )
        shifts = iter(
            [
                (-1.6949152542, 0.0, 0.5),
                (0.0, 4.5045045045, 0.5),
            ]
        )
        with patch(
            "probe_station_gui.camera.scan_scale_refinement._phase_overlap_shift",
            lambda *_args: next(shifts),
        ):
            refined = refine_scan_scale_from_tile_overlaps(tiles, scale)

        assert refined is not scale
        assert refined.pixels_to_mm is not None
        self.assertAlmostEqual(refined.pixels_to_mm[0][0], -0.000116, places=9)
        self.assertAlmostEqual(refined.pixels_to_mm[1][1], -0.000116, places=9)
        self.assertIn("scan-overlap", refined.source)

    def test_refine_scan_scale_from_tile_overlaps_rejects_periodic_outlier(
        self,
    ) -> None:
        from PySide6.QtGui import QImage

        image = QImage(200, 200, QImage.Format_RGB888)
        image.fill(0)
        scale = MicroscopeScaleCalibration(
            pixel_size_x_um=0.118,
            pixel_size_y_um=0.111,
            source="test",
            pixels_to_mm=(
                (-0.000118, 0.0),
                (0.0, -0.000111),
            ),
        )
        tiles = (
            (
                MicroscopeScanTile(1, 0, 0, (0.0, 0.0)),
                image,
            ),
            (
                MicroscopeScanTile(2, 0, 1, (0.0116, 0.0)),
                image,
            ),
            (
                MicroscopeScanTile(3, 1, 0, (0.0, -0.0116)),
                image,
            ),
            (
                MicroscopeScanTile(4, 1, 1, (0.0116, -0.0116)),
                image,
            ),
        )
        shifts = iter(
            [
                (-1.6949152542, 0.0, 0.5),
                (0.0, 4.5045045045, 0.5),
                (0.0, -45.4954954955, 0.5),
                (-1.6949152542, 0.0, 0.5),
            ]
        )
        with patch(
            "probe_station_gui.camera.scan_scale_refinement._phase_overlap_shift",
            lambda *_args: next(shifts),
        ):
            refined = refine_scan_scale_from_tile_overlaps(tiles, scale)

        assert refined is not scale
        assert refined.pixels_to_mm is not None
        self.assertAlmostEqual(refined.pixels_to_mm[0][0], -0.000116, places=9)
        self.assertAlmostEqual(refined.pixels_to_mm[1][1], -0.000116, places=9)

    def test_phase_overlap_shift_preserves_subpixel_translation(self) -> None:
        import cv2

        height = 160
        width = 160
        rng = np.random.default_rng(3)
        gray = np.zeros((height, width), dtype=np.float32)
        for _index in range(60):
            x = int(rng.integers(20, width - 20))
            y = int(rng.integers(20, height - 20))
            radius = int(round(float(rng.uniform(1.5, 4.0))))
            value = float(rng.uniform(80.0, 220.0))
            cv2.circle(gray, (x, y), radius, value, -1)
        gray = cv2.GaussianBlur(gray, (0, 0), sigmaX=1.0, sigmaY=1.0)
        existing = np.dstack((gray, gray, gray)).clip(0, 255).astype(np.uint8)
        transform = np.float32([[1.0, 0.0, 0.45], [0.0, 1.0, -0.65]])
        current = cv2.warpAffine(
            existing,
            transform,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

        shift = _phase_overlap_shift(existing, current)

        assert shift is not None
        shift_x, shift_y, response = shift
        self.assertGreater(response, 0.5)
        self.assertAlmostEqual(shift_x, 0.45, delta=0.25)
        self.assertAlmostEqual(shift_y, -0.65, delta=0.25)

    def test_scan_tile_registration_rejects_implausible_shift(self) -> None:
        first = MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.0, 0.0))
        second = MicroscopeScanTile(index=2, row=1, column=0, stage_xy=(0.0, -1.0))
        image = np.zeros((20, 20, 3), dtype=np.uint8)
        placements = [
            (first, 0.0, 0.0, image),
            (second, 0.0, 18.0, image),
        ]

        with patch(
            "probe_station_gui.camera.scan_scale_refinement._estimate_overlap_registration",
            return_value=(120.0, 0.0, 1.0),
        ):
            refined = _refine_scan_tile_pixel_placements(placements)

        self.assertEqual(refined[1][1], 0.0)
        self.assertEqual(refined[1][2], 18.0)

    def test_scan_tile_registration_ignores_diagonal_neighbors(self) -> None:
        first = MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.0, 0.0))
        diagonal = MicroscopeScanTile(index=2, row=1, column=1, stage_xy=(1.0, -1.0))
        image = np.zeros((20, 20, 3), dtype=np.uint8)
        placements = [
            (first, 0.0, 0.0, image),
            (diagonal, 18.0, 18.0, image),
        ]

        with patch(
            "probe_station_gui.camera.scan_scale_refinement._estimate_overlap_registration",
            return_value=(8.0, 8.0, 1.0),
        ) as estimate:
            refined = _refine_scan_tile_pixel_placements(placements)

        estimate.assert_not_called()
        self.assertEqual(refined[1][1], 18.0)
        self.assertEqual(refined[1][2], 18.0)
