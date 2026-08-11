import json
import subprocess
import sys
import textwrap
import unittest

from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera.imaging import (
    MicroscopeScaleCalibration,
    MicroscopeScanPlan,
    MicroscopeScanTile,
)
from probe_station_gui.camera.scan_stitching import stitch_scan_tiles


def test_stitching_keeps_stage_coordinate_placement_without_registration() -> None:
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

    bright_columns = [
        x
        for x in range(mosaic.width())
        if max(mosaic.pixelColor(x, 5).red(), mosaic.pixelColor(x, 5).blue()) > 200
    ]
    assert bright_columns == [6]


class ScanStitchingCharacterizationTest(unittest.TestCase):
    def test_stitch_scan_tiles_places_frames_in_mosaic(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                build_design_scan_plan,
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles
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
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles
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
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles
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
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles
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
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles
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
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles
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
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles
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
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles
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
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles

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

    def test_stitch_scan_tiles_keeps_coordinate_placement_without_registration(
        self,
    ) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
                MicroscopeScanPlan,
                MicroscopeScanTile,
            )
            from probe_station_gui.camera.scan_stitching import stitch_scan_tiles
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
