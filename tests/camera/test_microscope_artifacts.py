import json
import subprocess
import sys
import textwrap
import unittest

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from probe_station_gui.camera.imaging import (
    MicroscopeScaleCalibration,
    MicroscopeScanTile,
)
from probe_station_gui.camera.microscope_artifacts import (
    MicroscopeImageMetadata,
    route_photo_filename,
    save_microscope_image,
    scan_tile_filename,
)


def test_artifact_save_preserves_schema_encoding_overlay_and_dedup(tmp_path) -> None:
    _app = QApplication.instance() or QApplication([])
    frame = QImage(320, 240, QImage.Format_RGB32)
    frame.fill(QColor(80, 90, 100))
    scale = MicroscopeScaleCalibration(
        pixel_size_x_um=0.5,
        pixel_size_y_um=0.6,
        source="test",
    )
    metadata = MicroscopeImageMetadata(
        title="Probe \N{GREEK CAPITAL LETTER OMEGA}",
        mode="route photo",
        captured_at="2026-05-29T12:00:00+03:00",
        objective_name="X10",
        route_point_index=3,
        route_point_label="P003",
        stage_position=(1.0, 2.0, 3.0),
        design_xy=(10.0, 20.0),
    )

    first = save_microscope_image(
        frame=frame,
        output_dir=tmp_path,
        filename_stem="\N{GREEK CAPITAL LETTER OMEGA} /",
        metadata=metadata,
        scale=scale,
        save_raw=True,
    )
    second = save_microscope_image(
        frame=frame,
        output_dir=tmp_path,
        filename_stem="\N{GREEK CAPITAL LETTER OMEGA} /",
        metadata=metadata,
        scale=scale,
    )

    assert first.image_path.name == "microscope_image.png"
    assert first.raw_image_path is not None
    assert first.raw_image_path.name == "microscope_image_raw.png"
    assert first.raw_image.format() == QImage.Format_RGB888
    assert second.image_path.name == "microscope_image_002.png"
    sidecar_bytes = first.metadata_path.read_bytes()
    assert "Probe \N{GREEK CAPITAL LETTER OMEGA}".encode() in sidecar_bytes
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    assert tuple(sidecar) == ("FileName", "MicroscopeImage", "Microscopy")
    assert sidecar["FileName"] == first.image_path.name
    assert sidecar["MicroscopeImage"]["image_size_px"] == [320, 240]
    assert sidecar["MicroscopeImage"]["fov_um"] == [160.0, 144.0]
    assert sidecar["Microscopy"]["PixelSize"] == [0.5, 0.6]
    embedded = json.loads(
        QImage(str(first.image_path)).text("ProbeStationGUI.Metadata")
    )
    assert embedded == sidecar
    assert first.raw_image.pixelColor(10, 230) == QColor(80, 90, 100)
    assert QImage(str(first.image_path)).pixelColor(10, 230) != QColor(80, 90, 100)


def test_artifact_filenames_preserve_route_and_scan_payload_conventions() -> None:
    captured_at = "2026-05-29T12:00:00+03:00"
    tile = MicroscopeScanTile(
        index=7,
        row=1,
        column=2,
        stage_xy=(1.0, 2.0),
        label="corner / right",
    )

    assert (
        route_photo_filename(
            route_name="Route \N{GREEK CAPITAL LETTER OMEGA}",
            point_index=3,
            point_label="",
            captured_at=captured_at,
        )
        == "Route_point_003_P003_20260529_120000_0300"
    )
    assert (
        scan_tile_filename(
            scan_name="Scan \N{GREEK CAPITAL LETTER OMEGA}",
            tile=tile,
            captured_at=captured_at,
        )
        == "Scan_tile_0007_r002_c003_corner___right_20260529_120000_0300"
    )


class MicroscopeArtifactsCharacterizationTest(unittest.TestCase):
    def test_save_microscope_image_writes_png_and_sidecar_metadata(self) -> None:
        script = textwrap.dedent(
            """
            import json
            import tempfile
            from PySide6.QtGui import QColor, QImage
            from PySide6.QtWidgets import QApplication
            from probe_station_gui.camera.imaging import (
                MicroscopeScaleCalibration,
            )
            from probe_station_gui.camera.microscope_artifacts import (
                MicroscopeImageMetadata,
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
                MicroscopeScaleCalibration,
            )
            from probe_station_gui.camera.microscope_artifacts import (
                MicroscopeImageMetadata,
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
