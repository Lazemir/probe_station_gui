import json
import subprocess
import sys
import textwrap
import unittest

for _module_name in (
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
):
    _module = sys.modules.get(_module_name)
    if _module is not None and not hasattr(_module, "__file__"):
        del sys.modules[_module_name]

from probe_station_gui.microscope_imaging import (
    build_design_scan_plan,
)


class MicroscopeImagingTest(unittest.TestCase):
    def test_save_microscope_image_writes_png_and_sidecar_metadata(self) -> None:
        script = textwrap.dedent(
            """
            import json
            import tempfile
            from PySide6.QtGui import QColor, QImage
            from PySide6.QtWidgets import QApplication
            from probe_station_gui.microscope_imaging import (
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
            from probe_station_gui.microscope_imaging import (
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


if __name__ == "__main__":
    unittest.main()
