from __future__ import annotations

from pathlib import Path
import types

import pytest
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


def _capture_result(
    name: str,
    *,
    raw_image_path: Path | None = None,
) -> MicroscopeCaptureResult:
    image = QImage(2, 2, QImage.Format_RGB32)
    return MicroscopeCaptureResult(
        image_path=Path(f"C:/scan/{name}.png"),
        metadata_path=Path(f"C:/scan/{name}.json"),
        raw_image=image,
        metadata={},
        raw_image_path=raw_image_path,
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
        (0, 0, (11.0, 22.0)),
        (0, 1, (10.0, 22.0)),
        (0, 2, (9.0, 22.0)),
        (1, 2, (9.0, 20.0)),
        (1, 1, (10.0, 20.0)),
        (1, 0, (11.0, 20.0)),
        (2, 0, (11.0, 18.0)),
        (2, 1, (10.0, 18.0)),
        (2, 2, (9.0, 18.0)),
    ]


def test_centered_area_scan_plan_from_pixel_matrix_preserves_camera_axes() -> None:
    plan = microscope_scan.centered_area_scan_plan_from_pixel_matrix(
        center_stage_xy=(0.0, 0.0),
        frame_size_px=(10, 10),
        pixels_to_mm=((0.1, 0.0), (0.02, -0.1)),
        row_count=2,
        column_count=2,
        overlap_fraction=0.0,
    )

    assert [(tile.row, tile.column, tile.stage_xy) for tile in plan.tiles] == [
        (0, 0, (0.5, 0.6)),
        (0, 1, (-0.5, 0.4)),
        (1, 1, (-0.5, -0.6)),
        (1, 0, (0.5, -0.4)),
    ]
    assert plan.fov_size_mm == pytest.approx((1.019803902718557, 1.0))


def test_stitch_debug_scan_plan_places_structure_on_seams() -> None:
    plan = microscope_scan.stitch_debug_scan_plan_from_pixel_matrix(
        center_stage_xy=(10.0, 20.0),
        frame_size_px=(1000, 800),
        pixels_to_mm=((0.001, 0.0), (0.0, -0.001)),
        structure_size_mm=0.4,
        placement_fraction=1.0,
    )

    assert plan.row_count == 3
    assert plan.column_count == 3
    assert plan.fov_size_mm == pytest.approx((1.0, 0.8))
    assert [
        (tile.row, tile.column, tile.stage_xy, tile.label)
        for tile in plan.tiles
    ] == [
        (1, 1, (10.0, 20.0), "control"),
        (1, 0, (9.5, 20.0), "vertical_left"),
        (1, 2, (10.5, 20.0), "vertical_right"),
        (0, 1, (10.0, 19.6), "horizontal_top"),
        (2, 1, (10.0, 20.4), "horizontal_bottom"),
        (0, 0, (9.5, 19.6), "corner_top_left"),
        (0, 2, (10.5, 19.6), "corner_top_right"),
        (2, 0, (9.5, 20.4), "corner_bottom_left"),
        (2, 2, (10.5, 20.4), "corner_bottom_right"),
    ]
    assert [
        (group.name, [tile.label for tile in group.tiles])
        for group in microscope_scan.stitch_debug_mosaic_groups(plan)
    ] == [
        ("vertical_seam", ["vertical_left", "vertical_right"]),
        ("horizontal_seam", ["horizontal_top", "horizontal_bottom"]),
        (
            "corner_seam",
            [
                "corner_top_left",
                "corner_top_right",
                "corner_bottom_left",
                "corner_bottom_right",
            ],
        ),
    ]

    group_plans = [
        microscope_scan.stitch_debug_mosaic_group_plan(plan, group)
        for group in microscope_scan.stitch_debug_mosaic_groups(plan)
    ]
    assert [
        (
            group_plan.row_count,
            group_plan.column_count,
            [(tile.row, tile.column, tile.label) for tile in group_plan.tiles],
        )
        for group_plan in group_plans
    ] == [
        (
            1,
            2,
            [(0, 0, "vertical_left"), (0, 1, "vertical_right")],
        ),
        (
            2,
            1,
            [(0, 0, "horizontal_top"), (1, 0, "horizontal_bottom")],
        ),
        (
            2,
            2,
            [
                (0, 0, "corner_top_left"),
                (0, 1, "corner_top_right"),
                (1, 0, "corner_bottom_left"),
                (1, 1, "corner_bottom_right"),
            ],
        ),
    ]


def test_stitch_debug_scan_plan_can_place_structure_inside_overlap() -> None:
    plan = microscope_scan.stitch_debug_scan_plan_from_pixel_matrix(
        center_stage_xy=(10.0, 20.0),
        frame_size_px=(1000, 800),
        pixels_to_mm=((0.001, 0.0), (0.0, -0.001)),
        structure_size_mm=0.4,
        placement_fraction=1.0,
        overlap_fraction=0.1,
    )

    assert plan.overlap_fraction == pytest.approx(0.1)
    assert [
        (tile.row, tile.column, tile.stage_xy, tile.label)
        for tile in plan.tiles[:5]
    ] == [
        (1, 1, (10.0, 20.0), "control"),
        (1, 0, (9.55, 20.0), "vertical_left"),
        (1, 2, (10.45, 20.0), "vertical_right"),
        (0, 1, (10.0, 19.64), "horizontal_top"),
        (2, 1, (10.0, 20.36), "horizontal_bottom"),
    ]


def test_flat_field_scan_options_accept_boolean_and_object_payloads() -> None:
    defaults = microscope_scan.flat_field_options_from_payload(
        {},
        default_enabled=True,
    )
    assert defaults.enabled is True
    assert defaults.mode == "scan"
    assert defaults.blur_radius_px == 401
    assert defaults.max_gain == 4.0

    disabled = microscope_scan.flat_field_options_from_payload(
        {"flat_field": False},
        default_enabled=True,
    )
    assert disabled.enabled is False

    configured = microscope_scan.flat_field_options_from_payload(
        {
            "flat_field": {
                "enabled": True,
                "mode": "self",
                "blur_radius_px": 120,
                "max_gain": 2.5,
            }
        },
        default_enabled=False,
    )
    assert configured.enabled is True
    assert configured.mode == "self"
    assert configured.blur_radius_px == 121
    assert configured.max_gain == 2.5


def test_flat_field_scan_options_accept_reference_images() -> None:
    configured = microscope_scan.flat_field_options_from_payload(
        {
            "flat_field": {
                "enabled": True,
                "mode": "reference",
                "reference_images": ["C:/flat/east.png", "C:/flat/west.png"],
                "blur_radius_px": 801,
            }
        },
        default_enabled=False,
    )

    assert configured.enabled is True
    assert configured.mode == "reference"
    assert configured.reference_images == ("C:/flat/east.png", "C:/flat/west.png")
    assert configured.blur_radius_px == 801
    assert configured.to_metadata()["reference_images"] == [
        "C:/flat/east.png",
        "C:/flat/west.png",
    ]


def test_flat_field_reference_mode_requires_reference_images() -> None:
    with pytest.raises(ValueError, match="reference_images is required"):
        microscope_scan.flat_field_options_from_payload(
            {"flat_field": {"enabled": True, "mode": "reference"}},
            default_enabled=False,
        )


def test_camera_lock_settings_exclude_session_owned_exposure_nodes() -> None:
    disabled = microscope_scan.camera_lock_settings_from_payload(
        {"camera_lock": False},
        default_enabled=True,
    )
    assert disabled.enabled is False
    assert disabled.settings == ()

    enabled = microscope_scan.camera_lock_settings_from_payload(
        {},
        default_enabled=True,
    )
    assert enabled.enabled is True
    assert enabled.settings == (
        ("GainAuto", "Off"),
        ("BalanceWhiteAuto", "Off"),
    )
    assert not {"ExposureAuto", "ExposureTime"}.intersection(
        name for name, _value in enabled.settings
    )

    manually_configured = microscope_scan.CameraLockSettings(
        enabled=True,
        settings=(
            ("ExposureAuto", "Off"),
            ("ExposureTime", 2000.0),
            ("GainAuto", "Off"),
        ),
    )
    assert manually_configured.settings == (("GainAuto", "Off"),)
    assert manually_configured.to_metadata()["settings"] == [
        {"node_name": "GainAuto", "value": "Off"}
    ]


def test_scan_module_has_no_scan_specific_exposure_option() -> None:
    assert not hasattr(microscope_scan, "AutoExposureScanOptions")
    assert not hasattr(microscope_scan, "auto_exposure_options_from_payload")


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

    seam_mosaic_plan = microscope_scan.mosaic_image_save_plan(
        output_dir=Path("C:/scan"),
        scan_name="sample",
        plan=plan,
        captured_at="2026-06-28T12:01:00+03:00",
        objective_name="10x",
        magnification=10.0,
        filename_suffix="vertical_seam",
    )
    assert seam_mosaic_plan.filename_stem == (
        "sample_mosaic_vertical_seam_2026-06-28T12:01:00+03:00"
    )


def test_manifest_payload_lists_mosaic_and_tiles_in_plan_order() -> None:
    plan = _plan()
    tile_results = [
        _capture_result("tile1", raw_image_path=Path("C:/scan/tile1_raw.png")),
        _capture_result("tile2"),
    ]
    mosaic = _capture_result("mosaic")

    payload = microscope_scan.manifest_payload(
        plan=plan,
        tile_results=tile_results,
        mosaic_result=mosaic,
        created_at="2026-06-28T12:02:00+03:00",
        corrections={
            "flat_field": {"enabled": True, "mode": "self"},
            "camera_lock": {"enabled": True},
        },
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
            "raw_image": "C:\\scan\\tile1_raw.png",
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
    assert payload["corrections"] == {
        "flat_field": {"enabled": True, "mode": "self"},
        "camera_lock": {"enabled": True},
    }

    diagnostic_payload = microscope_scan.manifest_payload(
        plan=plan,
        tile_results=tile_results,
        mosaic_result=mosaic,
        created_at="2026-06-28T12:02:00+03:00",
        diagnostic_mosaics={
            "vertical_seam": _capture_result("vertical"),
            "horizontal_seam": _capture_result("horizontal"),
        },
    )
    assert diagnostic_payload["diagnostic_mosaics"] == {
        "vertical_seam": {
            "image": "C:\\scan\\vertical.png",
            "metadata": "C:\\scan\\vertical.json",
        },
        "horizontal_seam": {
            "image": "C:\\scan\\horizontal.png",
            "metadata": "C:\\scan\\horizontal.json",
        },
    }
