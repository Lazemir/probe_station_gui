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
    MicroscopeScanTile,
    build_design_scan_plan,
    _refine_scan_tile_pixel_placements,
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

    def test_stitch_scan_tiles_keeps_overlap_features_from_one_tile(self) -> None:
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
        self.assertGreater(max(result["overlap_red"], result["overlap_blue"]), 220)
        self.assertLess(min(result["overlap_red"], result["overlap_blue"]), 35)

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

    def test_stitch_scan_tiles_registers_overlap_before_cutting_seam(self) -> None:
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

        self.assertEqual(result["bright_columns"], [8])

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
