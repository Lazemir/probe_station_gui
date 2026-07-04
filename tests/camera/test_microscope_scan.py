from __future__ import annotations

from pathlib import Path
import types

from PySide6.QtGui import QImage

from probe_station_gui.camera.imaging import (
    MicroscopeCaptureResult,
    MicroscopeScaleCalibration,
    MicroscopeScanPlan,
    MicroscopeScanTile,
)
from probe_station_gui.camera import microscope_scan


def _document() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        path=Path("C:/designs/sample.gds"),
        bounds=(0.0, 0.0, 2.0, 1.0),
    )


def _scale() -> MicroscopeScaleCalibration:
    return MicroscopeScaleCalibration(
        pixel_size_x_um=100.0,
        pixel_size_y_um=200.0,
        source="test",
    )


def _plan() -> MicroscopeScanPlan:
    return MicroscopeScanPlan(
        tiles=(
            MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(1.0, 2.0)),
            MicroscopeScanTile(index=2, row=0, column=1, stage_xy=(3.0, 2.0)),
        ),
        stage_bounds=(0.0, 0.0, 4.0, 3.0),
        covered_stage_bounds=(-0.5, -0.5, 4.5, 3.5),
        fov_size_mm=(1.0, 1.0),
        overlap_fraction=0.2,
        row_count=1,
        column_count=2,
    )


def _capture_result(name: str) -> MicroscopeCaptureResult:
    image = QImage(2, 2, QImage.Format_RGB32)
    return MicroscopeCaptureResult(
        image_path=Path(f"C:/scan/{name}.png"),
        metadata_path=Path(f"C:/scan/{name}.json"),
        raw_image=image,
        metadata={},
    )


def test_default_output_dir_uses_design_stem_or_cwd() -> None:
    assert microscope_scan.default_output_dir(_document()) == (
        "C:\\designs\\sample-microscope-scan"
    )
    assert microscope_scan.default_output_dir(None, cwd=Path("C:/work")) == (
        "C:\\work\\microscope-scan"
    )


def test_start_decisions_preserve_guard_order_without_camera_frame() -> None:
    running = microscope_scan.start_environment_decision(
        scan_running=True,
        serial_connected=False,
    )
    assert running.status is not None
    assert running.status.message == "Microscope scan is already running."
    assert running.status.timeout_ms == 4000

    disconnected = microscope_scan.start_environment_decision(
        scan_running=False,
        serial_connected=False,
    )
    assert disconnected.status is not None
    assert disconnected.status.message == "Connect the stage controller before scanning."

    accepted = microscope_scan.start_environment_decision(
        scan_running=False,
        serial_connected=True,
    )
    assert accepted.accepted is True
    assert accepted.status is None

    missing_design = microscope_scan.start_design_decision(
        document=None,
        registration_valid=False,
    )
    assert missing_design.status is not None
    assert missing_design.status.message == "Load a design before scanning."

    missing_scale = microscope_scan.start_scale_decision(scale=None)
    assert missing_scale.status is not None
    assert missing_scale.status.message == (
        "Calibrate click-to-move for the active objective before scanning."
    )


def test_scan_plan_decision_builds_plan_from_frame_scale_and_transform() -> None:
    decision = microscope_scan.scan_plan_decision(
        document=_document(),
        scale=_scale(),
        frame_size_px=(10, 5),
        overlap_fraction=0.0,
        design_to_stage_xy=lambda point: (point[0] + 10.0, point[1] + 20.0),
    )

    assert decision.accepted is True
    assert decision.plan is not None
    assert decision.plan.fov_size_mm == (1.0, 1.0)
    assert decision.plan.stage_bounds == (10.0, 20.0, 12.0, 21.0)


def test_scan_plan_decision_reports_camera_and_transform_errors() -> None:
    no_frame = microscope_scan.scan_plan_decision(
        document=_document(),
        scale=_scale(),
        frame_size_px=None,
        overlap_fraction=0.0,
        design_to_stage_xy=lambda point: point,
    )
    assert no_frame.status is not None
    assert no_frame.status.message == "Camera frame is unavailable; cannot scan."

    bad_transform = microscope_scan.scan_plan_decision(
        document=_document(),
        scale=_scale(),
        frame_size_px=(10, 5),
        overlap_fraction=0.0,
        design_to_stage_xy=lambda _point: None,
    )
    assert bad_transform.status is not None
    assert bad_transform.status.message == (
        "Design registration is required for microscope scan."
    )


def test_centered_area_scan_plan_builds_serpentine_grid() -> None:
    plan = microscope_scan.centered_area_scan_plan(
        center_stage_xy=(10.0, 20.0),
        fov_size_mm=(1.0, 2.0),
        row_count=3,
        column_count=3,
        overlap_fraction=0.0,
    )

    assert plan.row_count == 3
    assert plan.column_count == 3
    assert plan.covered_stage_bounds == (8.5, 17.0, 11.5, 23.0)
    assert [(tile.row, tile.column, tile.stage_xy) for tile in plan.tiles] == [
        (0, 0, (9.0, 22.0)),
        (0, 1, (10.0, 22.0)),
        (0, 2, (11.0, 22.0)),
        (1, 2, (11.0, 20.0)),
        (1, 1, (10.0, 20.0)),
        (1, 0, (9.0, 20.0)),
        (2, 0, (9.0, 18.0)),
        (2, 1, (10.0, 18.0)),
        (2, 2, (11.0, 18.0)),
    ]


def test_tile_and_mosaic_save_plans_preserve_metadata_payloads() -> None:
    plan = _plan()
    tile_plan = microscope_scan.tile_image_save_plan(
        output_dir=Path("C:/scan"),
        scan_name="sample",
        tile=plan.tiles[0],
        plan=plan,
        captured_at="2026-06-28T12:00:00+03:00",
        objective_name="10x",
        magnification=10.0,
        design_xy=(5.0, 6.0),
        stage_position=(1.0, 2.0, 3.0),
    )
    assert tile_plan.output_dir == Path("C:/scan/tiles")
    assert "sample_tile_0001" in tile_plan.filename_stem
    assert tile_plan.metadata.mode == "design scan tile"
    assert tile_plan.metadata.scan_tile_index == 1
    assert tile_plan.metadata.scan_tile_total == 2
    assert tile_plan.metadata.scan_row == 0
    assert tile_plan.metadata.scan_column == 0
    assert tile_plan.metadata.design_xy == (5.0, 6.0)
    assert tile_plan.metadata.stage_xy == (1.0, 2.0)
    assert tile_plan.metadata.notes == ("needles raised before scan",)
    assert tile_plan.metadata.extra == {
        "overlap_fraction": 0.2,
        "row_count": 1,
        "column_count": 2,
    }

    mosaic_plan = microscope_scan.mosaic_image_save_plan(
        output_dir=Path("C:/scan"),
        scan_name="sample",
        plan=plan,
        captured_at="2026-06-28T12:01:00+03:00",
        objective_name="10x",
        magnification=10.0,
    )
    assert mosaic_plan.output_dir == Path("C:/scan")
    assert mosaic_plan.filename_stem == "sample_mosaic_2026-06-28T12:01:00+03:00"
    assert mosaic_plan.metadata.mode == "design scan mosaic"
    assert mosaic_plan.metadata.scan_tile_total == 2
    assert mosaic_plan.metadata.extra["stage_bounds"] == [0.0, 0.0, 4.0, 3.0]
    assert mosaic_plan.metadata.extra["covered_stage_bounds"] == [
        -0.5,
        -0.5,
        4.5,
        3.5,
    ]


def test_manifest_payload_lists_mosaic_and_tiles_in_plan_order() -> None:
    plan = _plan()
    tile_results = [_capture_result("tile1"), _capture_result("tile2")]
    mosaic = _capture_result("mosaic")

    payload = microscope_scan.manifest_payload(
        plan=plan,
        tile_results=tile_results,
        mosaic_result=mosaic,
        created_at="2026-06-28T12:02:00+03:00",
    )

    assert payload["version"] == 1
    assert payload["created_at"] == "2026-06-28T12:02:00+03:00"
    assert payload["tile_count"] == 2
    assert payload["row_count"] == 1
    assert payload["column_count"] == 2
    assert payload["mosaic"] == {
        "image": "C:\\scan\\mosaic.png",
        "metadata": "C:\\scan\\mosaic.json",
    }
    assert payload["tiles"] == [
        {
            "index": 1,
            "row": 0,
            "column": 0,
            "stage_xy": [1.0, 2.0],
            "image": "C:\\scan\\tile1.png",
            "metadata": "C:\\scan\\tile1.json",
        },
        {
            "index": 2,
            "row": 0,
            "column": 1,
            "stage_xy": [3.0, 2.0],
            "image": "C:\\scan\\tile2.png",
            "metadata": "C:\\scan\\tile2.json",
        },
    ]
