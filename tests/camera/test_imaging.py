import json
import numpy as np
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

for _module_name in (
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
):
    _module = sys.modules.get(_module_name)
    if _module is not None and not hasattr(_module, "__file__"):
        del sys.modules[_module_name]

from probe_station_gui.camera.imaging import (
    MicroscopeScaleCalibration,
    MicroscopeScanPlan,
    MicroscopeScanTile,
    build_design_scan_plan,
    _phase_overlap_shift,
    _refine_scan_tile_pixel_placements,
    refine_scan_scale_from_tile_overlaps,
    stitch_scan_tiles,
)


class MicroscopeImagingTest(unittest.TestCase):
    def test_save_microscope_image_writes_png_and_sidecar_metadata(self) -> None:
        script = textwrap.dedent(
            """
            import json
            import tempfile
            from PySide6.QtGui import QColor, QImage
            from PySide6.QtWidgets import QApplication
            from probe_station_gui.camera.imaging import (
                MicroscopeImageMetadata,
                MicroscopeScaleCalibration,
                save_microscope_image,
            )
            _app = QApplication.instance() or QApplication([])
            image = QImage(320, 240, QImage.Format_RGB32)
            image.fill(QColor("darkGray"))
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=0.5,
                pixel_size_y_um=0.6,
                source="test",
            )
            metadata = MicroscopeImageMetadata(
                title="Probe Station Microscope",
                mode="route photo",
                captured_at="2026-05-29T12:00:00+03:00",
                objective_name="X10",
                magnification=10.0,
                route_name="route",
                route_point_index=3,
                route_point_label="P003",
                stage_position=(1.0, 2.0, 3.0),
                design_xy=(10.0, 20.0),
            )
            with tempfile.TemporaryDirectory() as tmpdir:
                result = save_microscope_image(
                    frame=image,
                    output_dir=tmpdir,
                    filename_stem="capture",
                    metadata=metadata,
                    scale=scale,
                )
                assert result.image_path.exists()
                assert result.metadata_path.exists()
                with result.metadata_path.open("r", encoding="utf-8") as handle:
                    sidecar = json.load(handle)
            print(json.dumps(sidecar))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        sidecar = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertEqual(sidecar["Microscopy"]["PixelSize"], [0.5, 0.6])
        self.assertEqual(sidecar["Microscopy"]["PixelSizeUnits"], "um")
        self.assertEqual(
            sidecar["MicroscopeImage"]["image_size_px"],
            [320, 240],
        )
        self.assertEqual(
            sidecar["MicroscopeImage"]["fov_um"],
            [160.0, 144.0],
        )

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
            "probe_station_gui.camera.imaging._phase_overlap_shift",
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
            "probe_station_gui.camera.imaging._phase_overlap_shift",
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

    def test_save_microscope_image_can_write_raw_png_without_overlay(self) -> None:
        script = textwrap.dedent(
            """
            import json
            import tempfile
            from PySide6.QtGui import QColor, QImage
            from PySide6.QtWidgets import QApplication
            from probe_station_gui.camera.imaging import (
                MicroscopeImageMetadata,
                MicroscopeScaleCalibration,
                save_microscope_image,
            )
            _app = QApplication.instance() or QApplication([])
            image = QImage(320, 240, QImage.Format_RGB32)
            image.fill(QColor(80, 90, 100))
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=0.5,
                pixel_size_y_um=0.6,
                source="test",
            )
            metadata = MicroscopeImageMetadata(
                title="Probe Station Microscope",
                mode="design scan tile",
                captured_at="2026-05-29T12:00:00+03:00",
                objective_name="X10",
                magnification=10.0,
            )
            with tempfile.TemporaryDirectory() as tmpdir:
                result = save_microscope_image(
                    frame=image,
                    output_dir=tmpdir,
                    filename_stem="capture",
                    metadata=metadata,
                    scale=scale,
                    save_raw=True,
                )
                assert result.raw_image_path is not None
                raw = QImage(str(result.raw_image_path))
                annotated = QImage(str(result.image_path))
                raw_bottom = raw.pixelColor(10, raw.height() - 10)
                annotated_bottom = annotated.pixelColor(10, annotated.height() - 10)
                print(json.dumps({
                    "raw_exists": result.raw_image_path.exists(),
                    "raw_red": raw_bottom.red(),
                    "annotated_red": annotated_bottom.red(),
                }))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertTrue(result["raw_exists"])
        self.assertEqual(result["raw_red"], 80)
        self.assertLess(result["annotated_red"], 80)

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

    def test_stitch_scan_tiles_places_frames_in_mosaic(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                build_design_scan_plan,
                stitch_scan_tiles,
            )
            plan = build_design_scan_plan(
                stage_bounds=(0.0, 0.0, 2.0, 1.0),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.0,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=100.0,
                pixel_size_y_um=100.0,
            )
            tile_images = []
            for tile in plan.tiles:
                image = QImage(10, 10, QImage.Format_RGB32)
                image.fill(QColor("red") if tile.column == 0 else QColor("blue"))
                tile_images.append((tile, image))
            mosaic = stitch_scan_tiles(plan=plan, tile_images=tile_images, scale=scale)
            print(json.dumps({"width": mosaic.width(), "height": mosaic.height()}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(result["width"], 20)
        self.assertEqual(result["height"], 10)

    def test_stitch_scan_tiles_uses_full_pixel_matrix_for_tile_placement(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
                stitch_scan_tiles,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=100.0,
                pixel_size_y_um=100.0,
                pixels_to_mm=((0.1, 0.0), (0.02, -0.1)),
            )
            plan = MicroscopeScanPlan(
                tiles=(
                    MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.0, 0.0)),
                    MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(-1.0, -0.2)),
                ),
                stage_bounds=(0.0, -0.5, 1.0, 0.7),
                covered_stage_bounds=(0.0, -0.5, 1.0, 0.7),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.0,
                row_count=1,
                column_count=2,
            )
            red = QImage(10, 10, QImage.Format_RGB32)
            red.fill(QColor("red"))
            blue = QImage(10, 10, QImage.Format_RGB32)
            blue.fill(QColor("blue"))
            mosaic = stitch_scan_tiles(
                plan=plan,
                tile_images=((plan.tiles[0], red), (plan.tiles[1], blue)),
                scale=scale,
            )
            print(json.dumps({"width": mosaic.width(), "height": mosaic.height()}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(result["width"], 20)
        self.assertEqual(result["height"], 10)

    def test_stitch_scan_tiles_places_negative_stage_x_on_left(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
                stitch_scan_tiles,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=100.0,
                pixel_size_y_um=100.0,
                pixels_to_mm=((-0.1, 0.0), (0.0, -0.1)),
            )
            plan = MicroscopeScanPlan(
                tiles=(
                    MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.0, 0.0)),
                    MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(-1.0, 0.0)),
                ),
                stage_bounds=(-1.5, -0.5, 0.5, 0.5),
                covered_stage_bounds=(-1.5, -0.5, 0.5, 0.5),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.0,
                row_count=1,
                column_count=2,
            )
            red = QImage(10, 10, QImage.Format_RGB32)
            red.fill(QColor("red"))
            blue = QImage(10, 10, QImage.Format_RGB32)
            blue.fill(QColor("blue"))
            mosaic = stitch_scan_tiles(
                plan=plan,
                tile_images=((plan.tiles[0], red), (plan.tiles[1], blue)),
                scale=scale,
            )
            left = mosaic.pixelColor(5, 5)
            right = mosaic.pixelColor(15, 5)
            print(json.dumps({
                "width": mosaic.width(),
                "left_blue": left.blue(),
                "right_red": right.red(),
            }))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertEqual(result["width"], 20)
        self.assertGreater(result["left_blue"], 220)
        self.assertGreater(result["right_red"], 220)

    def test_stitch_scan_tiles_blends_overlap_low_frequency(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
                stitch_scan_tiles,
            )
            plan = MicroscopeScanPlan(
                tiles=(
                    MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.5, 0.5)),
                    MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(0.0, 0.5)),
                ),
                stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                covered_stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.5,
                row_count=1,
                column_count=2,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=100.0,
                pixel_size_y_um=100.0,
            )
            red = QImage(10, 10, QImage.Format_RGB32)
            red.fill(QColor(255, 0, 0))
            blue = QImage(10, 10, QImage.Format_RGB32)
            blue.fill(QColor(0, 0, 255))
            mosaic = stitch_scan_tiles(
                plan=plan,
                tile_images=((plan.tiles[0], red), (plan.tiles[1], blue)),
                scale=scale,
            )
            overlap = mosaic.pixelColor(7, 5)
            print(json.dumps({
                "width": mosaic.width(),
                "height": mosaic.height(),
                "overlap_red": overlap.red(),
                "overlap_blue": overlap.blue(),
            }))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertEqual(result["width"], 15)
        self.assertEqual(result["height"], 10)
        self.assertGreater(result["overlap_red"], 90)
        self.assertGreater(result["overlap_blue"], 90)
        self.assertLess(result["overlap_red"], 170)
        self.assertLess(result["overlap_blue"], 170)

    def test_stitch_scan_tiles_blends_bright_low_frequency_background(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
                stitch_scan_tiles,
            )
            plan = MicroscopeScanPlan(
                tiles=(
                    MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.5, 0.5)),
                    MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(0.0, 0.5)),
                ),
                stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                covered_stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.5,
                row_count=1,
                column_count=2,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=100.0,
                pixel_size_y_um=100.0,
            )
            left = QImage(10, 10, QImage.Format_RGB32)
            left.fill(QColor(230, 230, 230))
            right = QImage(10, 10, QImage.Format_RGB32)
            right.fill(QColor(170, 170, 170))
            mosaic = stitch_scan_tiles(
                plan=plan,
                tile_images=((plan.tiles[0], left), (plan.tiles[1], right)),
                scale=scale,
            )
            overlap = mosaic.pixelColor(7, 5)
            print(json.dumps({"overlap_red": overlap.red()}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertGreater(result["overlap_red"], 180)
        self.assertLess(result["overlap_red"], 220)

    def test_stitch_scan_tiles_preserves_low_weight_bright_feature(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
                stitch_scan_tiles,
            )
            plan = MicroscopeScanPlan(
                tiles=(
                    MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.5, 0.5)),
                    MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(0.0, 0.5)),
                ),
                stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                covered_stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.5,
                row_count=1,
                column_count=2,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=100.0,
                pixel_size_y_um=100.0,
            )
            left = QImage(10, 10, QImage.Format_RGB32)
            left.fill(QColor(55, 60, 45))
            right = QImage(10, 10, QImage.Format_RGB32)
            right.fill(QColor(55, 60, 45))
            for y in range(10):
                left.setPixelColor(8, y, QColor(255, 245, 120))
            mosaic = stitch_scan_tiles(
                plan=plan,
                tile_images=((plan.tiles[0], left), (plan.tiles[1], right)),
                scale=scale,
            )
            feature = mosaic.pixelColor(8, 5)
            print(json.dumps({
                "red": feature.red(),
                "green": feature.green(),
                "blue": feature.blue(),
            }))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertGreater(result["red"], 220)
        self.assertGreater(result["green"], 210)
        self.assertGreater(result["blue"], 90)

    def test_stitch_scan_tiles_preserves_fractional_tile_placement(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
                stitch_scan_tiles,
            )
            plan = MicroscopeScanPlan(
                tiles=(
                    MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.0, 0.0)),
                    MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(-0.96, 0.0)),
                ),
                stage_bounds=(-0.5, -0.5, 1.46, 0.5),
                covered_stage_bounds=(-0.5, -0.5, 1.46, 0.5),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.0,
                row_count=1,
                column_count=2,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=100.0,
                pixel_size_y_um=100.0,
            )
            left = QImage(10, 10, QImage.Format_RGB32)
            left.fill(QColor(255, 0, 0))
            right = QImage(10, 10, QImage.Format_RGB32)
            right.fill(QColor(0, 0, 255))
            mosaic = stitch_scan_tiles(
                plan=plan,
                tile_images=((plan.tiles[0], left), (plan.tiles[1], right)),
                scale=scale,
            )
            seam = mosaic.pixelColor(9, 5)
            print(json.dumps({
                "width": mosaic.width(),
                "seam_red": seam.red(),
                "seam_blue": seam.blue(),
            }))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertEqual(result["width"], 20)
        self.assertGreater(result["seam_red"], 120)
        self.assertGreater(result["seam_blue"], 20)
        self.assertLess(result["seam_red"], 255)

    def test_stitch_scan_tiles_levels_tile_background_in_overlap(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
                stitch_scan_tiles,
            )
            plan = MicroscopeScanPlan(
                tiles=(
                    MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.5, 0.5)),
                    MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(0.0, 0.5)),
                ),
                stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                covered_stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.5,
                row_count=1,
                column_count=2,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=100.0,
                pixel_size_y_um=100.0,
            )
            left = QImage(10, 10, QImage.Format_RGB32)
            left.fill(QColor(70, 85, 100))
            right = QImage(10, 10, QImage.Format_RGB32)
            right.fill(QColor(125, 145, 165))
            mosaic = stitch_scan_tiles(
                plan=plan,
                tile_images=((plan.tiles[0], left), (plan.tiles[1], right)),
                scale=scale,
            )
            left_sample = mosaic.pixelColor(3, 5)
            right_sample = mosaic.pixelColor(12, 5)
            print(json.dumps({
                "red_delta": abs(left_sample.red() - right_sample.red()),
                "green_delta": abs(left_sample.green() - right_sample.green()),
                "blue_delta": abs(left_sample.blue() - right_sample.blue()),
            }))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertLessEqual(result["red_delta"], 4)
        self.assertLessEqual(result["green_delta"], 4)
        self.assertLessEqual(result["blue_delta"], 4)

    def test_stitch_scan_tiles_levels_linear_tile_background_drift(self) -> None:
        script = textwrap.dedent(
            """
            import json
            import numpy as np
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
                stitch_scan_tiles,
            )

            width = 40
            height = 24
            world_width = 60
            world = np.zeros((height, world_width), dtype=np.float32)
            for y in range(height):
                for x in range(world_width):
                    world[y, x] = 92 + 0.35 * x + 0.2 * y

            left = QImage(width, height, QImage.Format_RGB32)
            right = QImage(width, height, QImage.Format_RGB32)
            for y in range(height):
                for x in range(width):
                    left_value = int(round(world[y, x]))
                    drift = -18.0 + 36.0 * x / (width - 1)
                    right_value = int(round(world[y, x + 20] + drift))
                    left.setPixelColor(x, y, QColor(left_value, left_value, left_value))
                    right.setPixelColor(x, y, QColor(right_value, right_value, right_value))

            plan = MicroscopeScanPlan(
                tiles=(
                    MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.5, 0.5)),
                    MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(0.0, 0.5)),
                ),
                stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                covered_stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.5,
                row_count=1,
                column_count=2,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=25.0,
                pixel_size_y_um=25.0,
            )
            mosaic = stitch_scan_tiles(
                plan=plan,
                tile_images=((plan.tiles[0], left), (plan.tiles[1], right)),
                scale=scale,
            )
            sample_y = height // 2
            far_right = mosaic.pixelColor(55, sample_y).red()
            expected_far_right = int(round(world[sample_y, 55]))
            print(json.dumps({
                "far_right_delta": abs(far_right - expected_far_right),
                "far_right": far_right,
                "expected_far_right": expected_far_right,
            }))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertLessEqual(result["far_right_delta"], 8)

    def test_stitch_scan_tiles_keeps_coordinate_placement_without_registration(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
                stitch_scan_tiles,
            )
            plan = MicroscopeScanPlan(
                tiles=(
                    MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.5, 0.5)),
                    MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(0.0, 0.5)),
                ),
                stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                covered_stage_bounds=(-0.5, 0.0, 1.0, 1.0),
                fov_size_mm=(1.0, 1.0),
                overlap_fraction=0.5,
                row_count=1,
                column_count=2,
            )
            scale = MicroscopeScaleCalibration(
                pixel_size_x_um=100.0,
                pixel_size_y_um=100.0,
            )
            left = QImage(10, 10, QImage.Format_RGB32)
            left.fill(QColor("black"))
            right = QImage(10, 10, QImage.Format_RGB32)
            right.fill(QColor("black"))
            for y in range(10):
                left.setPixelColor(8, y, QColor("white"))
                right.setPixelColor(1, y, QColor("white"))
            mosaic = stitch_scan_tiles(
                plan=plan,
                tile_images=((plan.tiles[0], left), (plan.tiles[1], right)),
                scale=scale,
            )
            bright_columns = []
            for x in range(mosaic.width()):
                if max(mosaic.pixelColor(x, 5).red(), mosaic.pixelColor(x, 5).blue()) > 200:
                    bright_columns.append(x)
            print(json.dumps({"width": mosaic.width(), "bright_columns": bright_columns}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertEqual(result["bright_columns"], [6])

    def test_scan_tile_registration_rejects_implausible_shift(self) -> None:
        first = MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(0.0, 0.0))
        second = MicroscopeScanTile(index=2, row=1, column=0, stage_xy=(0.0, -1.0))
        image = np.zeros((20, 20, 3), dtype=np.uint8)
        placements = [
            (first, 0.0, 0.0, image),
            (second, 0.0, 18.0, image),
        ]

        with patch(
            "probe_station_gui.camera.imaging._estimate_overlap_registration",
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
            "probe_station_gui.camera.imaging._estimate_overlap_registration",
            return_value=(8.0, 8.0, 1.0),
        ) as estimate:
            refined = _refine_scan_tile_pixel_placements(placements)

        estimate.assert_not_called()
        self.assertEqual(refined[1][1], 18.0)
        self.assertEqual(refined[1][2], 18.0)

    def test_self_flat_field_correction_reduces_low_frequency_gradient(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import apply_self_flat_field_correction

            width = 81
            image = QImage(width, 21, QImage.Format_RGB32)
            for y in range(image.height()):
                for x in range(width):
                    shade = 55 + int(90 * x / (width - 1))
                    image.setPixelColor(x, y, QColor(shade, shade, shade))

            corrected = apply_self_flat_field_correction(
                image,
                blur_radius_px=17,
                max_gain=4.0,
            )
            left = corrected.pixelColor(4, 10).red()
            right = corrected.pixelColor(width - 5, 10).red()
            center = corrected.pixelColor(width // 2, 10).red()
            print(json.dumps({"left": left, "center": center, "right": right}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertLess(abs(result["left"] - result["right"]), 16)
        self.assertGreater(result["center"], 80)

    def test_flat_field_profile_rejects_frame_size_mismatch(self) -> None:
        script = textwrap.dedent(
            """
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                apply_flat_field_correction,
                build_flat_field_profile,
            )

            reference = QImage(20, 20, QImage.Format_RGB32)
            reference.fill(QColor(120, 120, 120))
            frame = QImage(21, 20, QImage.Format_RGB32)
            frame.fill(QColor(120, 120, 120))
            profile = build_flat_field_profile(reference, blur_radius_px=9)
            try:
                apply_flat_field_correction(frame, profile)
            except ValueError as exc:
                print(str(exc))
            else:
                raise AssertionError("expected ValueError")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("frame size", completed.stdout)

    def test_compiled_flat_field_matches_profile_application(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                apply_compiled_flat_field_correction,
                apply_flat_field_correction,
                build_flat_field_profile,
                compile_flat_field_correction,
            )

            reference = QImage(31, 17, QImage.Format_RGB32)
            frame = QImage(31, 17, QImage.Format_RGB32)
            for y in range(reference.height()):
                for x in range(reference.width()):
                    shade = 45 + 4 * x
                    reference.setPixelColor(x, y, QColor(shade, shade + 3, shade // 2))
                    frame.setPixelColor(x, y, QColor(100, 110, 70))

            profile = build_flat_field_profile(reference, blur_radius_px=9, max_gain=5.0)
            expected = apply_flat_field_correction(frame, profile)
            compiled = compile_flat_field_correction(profile)
            actual = apply_compiled_flat_field_correction(frame, compiled)
            same = all(
                expected.pixelColor(x, y) == actual.pixelColor(x, y)
                for y in range(frame.height())
                for x in range(frame.width())
            )
            print(json.dumps({"same": same, "size": list(compiled.image_size_px)}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(result["same"])
        self.assertEqual(result["size"], [31, 17])

    def test_median_flat_field_profile_ignores_moving_dark_features(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                apply_flat_field_correction,
                build_median_flat_field_profile,
            )

            width = 81
            frames = []
            for dark_x in (12, 40, 68):
                image = QImage(width, 21, QImage.Format_RGB32)
                for y in range(image.height()):
                    for x in range(width):
                        shade = 55 + int(90 * x / (width - 1))
                        if abs(x - dark_x) <= 2:
                            shade = max(0, shade - 45)
                        image.setPixelColor(x, y, QColor(shade, shade, shade))
                frames.append(image)

            profile = build_median_flat_field_profile(
                frames,
                blur_radius_px=17,
                max_gain=4.0,
            )
            corrected = apply_flat_field_correction(frames[0], profile)
            left = corrected.pixelColor(4, 10).red()
            right = corrected.pixelColor(width - 5, 10).red()
            dark = corrected.pixelColor(12, 10).red()
            print(json.dumps({"left": left, "right": right, "dark": dark}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertLess(abs(result["left"] - result["right"]), 18)
        self.assertLess(result["dark"], result["left"] - 20)


if __name__ == "__main__":
    unittest.main()
