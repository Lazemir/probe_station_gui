from __future__ import annotations

import types

import pytest
from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera import microscope_scan
from main import Main


class _FakeFrame:
    def width(self) -> int:
        return 1000

    def height(self) -> int:
        return 800


class _FakeScanThread:
    def __init__(self, *, target, args, name, daemon):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self) -> None:
        self.started = True


def _tile_signature(tiles):
    return tuple(
        (
            tile.index,
            tile.row,
            tile.column,
            tile.stage_xy,
            getattr(tile, "label", ""),
        )
        for tile in tiles
    )


def test_microscope_scan_disconnected_stage_does_not_read_scale_or_camera() -> None:
    window = Main.__new__(Main)
    statuses: list[tuple[str, int]] = []
    calls: list[str] = []
    window.serial_connection = None
    window._microscope_scan_running = lambda: False
    window._show_status = (
        lambda message, timeout_ms=0: statuses.append((message, timeout_ms))
    )
    window._active_microscope_scale = lambda: calls.append("scale")
    window._wait_for_camera_frame = lambda **_kwargs: calls.append("camera")

    Main._start_microscope_scan(
        window,
        types.SimpleNamespace(overlap_fraction=0.0),
    )

    assert statuses == [("Connect the stage controller before scanning.", 5000)]
    assert calls == []


def test_microscope_scan_missing_design_does_not_read_scale_or_camera() -> None:
    window = Main.__new__(Main)
    statuses: list[tuple[str, int]] = []
    calls: list[str] = []
    window.serial_connection = types.SimpleNamespace(is_open=True)
    window._design_session = types.SimpleNamespace(document=None, registration=None)
    window._microscope_scan_running = lambda: False
    window._show_status = (
        lambda message, timeout_ms=0: statuses.append((message, timeout_ms))
    )
    window._active_microscope_scale = lambda: calls.append("scale")
    window._wait_for_camera_frame = lambda **_kwargs: calls.append("camera")

    Main._start_microscope_scan(
        window,
        types.SimpleNamespace(overlap_fraction=0.0),
    )

    assert statuses == [("Load a design before scanning.", 5000)]
    assert calls == []


def test_stage_position_metadata_prefers_frame_stage_xy() -> None:
    window = Main.__new__(Main)
    window.stage_controller = types.SimpleNamespace(
        latest_stage_position=lambda: (1.0, 2.0, 3.0, 4.0)
    )

    position = Main._stage_position_for_image_metadata(
        window,
        stage_xy=(10.0, 20.0),
    )

    assert position == (10.0, 20.0, 3.0, 4.0)


def test_microscope_scan_actual_position_reads_latest_stage_position() -> None:
    window = Main.__new__(Main)
    window.stage_controller = types.SimpleNamespace(
        latest_stage_position=lambda: (1.25, -2.5, 3.0)
    )

    actual = Main._microscope_scan_actual_position(window)

    assert actual == (1.25, -2.5, 3.0)


def test_microscope_scan_tile_move_uses_consistent_positive_approach() -> None:
    window = Main.__new__(Main)
    moves: list[tuple[float, float]] = []
    window.stage_controller = types.SimpleNamespace(
        run_external_move_to_xy=lambda x_mm, y_mm: moves.append((x_mm, y_mm))
    )
    tile = types.SimpleNamespace(stage_xy=(1.25, -2.5))

    Main._move_to_microscope_scan_tile(window, tile, tile_approach_mm=0.01)

    assert moves == [(1.24, -2.51), (1.25, -2.5)]


def test_microscope_scan_tile_move_skips_approach_when_disabled() -> None:
    window = Main.__new__(Main)
    moves: list[tuple[float, float]] = []
    window.stage_controller = types.SimpleNamespace(
        run_external_move_to_xy=lambda x_mm, y_mm: moves.append((x_mm, y_mm))
    )
    tile = types.SimpleNamespace(stage_xy=(1.25, -2.5))

    Main._move_to_microscope_scan_tile(window, tile, tile_approach_mm=0.0)

    assert moves == [(1.25, -2.5)]


def test_microscope_scan_reference_flat_field_loads_reference_image(tmp_path) -> None:
    reference_path = tmp_path / "flat.png"
    reference = QImage(8, 6, QImage.Format_RGB32)
    reference.fill(QColor(80, 100, 120))
    assert reference.save(str(reference_path), "PNG")
    options = microscope_scan.FlatFieldScanOptions(
        enabled=True,
        mode="reference",
        blur_radius_px=9,
        reference_images=(str(reference_path),),
    )

    profile = Main._flat_field_profile_for_microscope_scan([], options)

    assert profile is not None
    assert profile.source == "reference"
    assert profile.image_size_px == (8, 6)


def test_api_microscope_area_scan_builds_stitch_debug_plan(monkeypatch) -> None:
    created_threads: list[_FakeScanThread] = []
    monkeypatch.setattr(
        "main.threading.Thread",
        lambda **kwargs: created_threads.append(_FakeScanThread(**kwargs))
        or created_threads[-1],
    )
    window = Main.__new__(Main)
    window.stage_controller = types.SimpleNamespace(
        is_busy=lambda: False,
        latest_stage_position=lambda: (10.0, 20.0, 3.0),
    )
    window._microscope_scan_running = lambda: False
    window._stage_serial_ready = lambda: True
    window._active_microscope_scale = lambda: types.SimpleNamespace(
        pixels_to_mm=((0.001, 0.0), (0.0, -0.001))
    )
    window._wait_for_camera_frame = lambda **_kwargs: (_FakeFrame(), 1)
    window._microscope_area_scan_default_output_dir = lambda: "C:/scan"
    window._microscope_scan_stop_requested = types.SimpleNamespace(clear=lambda: None)
    window._update_stage_coordinate_apply_state = lambda: None

    response = Main._api_microscope_area_scan(
        window,
        {
            "pattern": "stitch_debug",
            "structure_size_mm": 0.4,
            "flat_field": False,
        },
    )

    assert response["accepted"] is True
    assert response["rows"] == 3
    assert response["columns"] == 3
    assert response["pattern"] == "stitch_debug"
    thread = created_threads[0]
    configuration, plan = thread.args
    assert configuration.refine_scale_from_overlaps is False
    assert configuration.overlap_fraction == pytest.approx(0.25)
    assert plan.overlap_fraction == pytest.approx(0.25)
    expected_plan = microscope_scan.stitch_debug_scan_plan_from_pixel_matrix(
        center_stage_xy=(10.0, 20.0),
        frame_size_px=(1000, 800),
        pixels_to_mm=((0.001, 0.0), (0.0, -0.001)),
        structure_size_mm=0.4,
        placement_fraction=1.0,
        overlap_fraction=0.25,
    )
    assert _tile_signature(plan.tiles) == _tile_signature(expected_plan.tiles)


def test_api_microscope_area_scan_defaults_to_large_area_overlap(
    monkeypatch,
) -> None:
    created_threads: list[_FakeScanThread] = []
    monkeypatch.setattr(
        "main.threading.Thread",
        lambda **kwargs: created_threads.append(_FakeScanThread(**kwargs))
        or created_threads[-1],
    )
    window = Main.__new__(Main)
    window.stage_controller = types.SimpleNamespace(
        is_busy=lambda: False,
        latest_stage_position=lambda: (10.0, 20.0, 3.0),
    )
    window._microscope_scan_running = lambda: False
    window._stage_serial_ready = lambda: True
    window._active_microscope_scale = lambda: types.SimpleNamespace(
        pixels_to_mm=((0.001, 0.0), (0.0, -0.001))
    )
    window._wait_for_camera_frame = lambda **_kwargs: (_FakeFrame(), 1)
    window._microscope_area_scan_default_output_dir = lambda: "C:/scan"
    window._microscope_scan_stop_requested = types.SimpleNamespace(clear=lambda: None)
    window._update_stage_coordinate_apply_state = lambda: None

    response = Main._api_microscope_area_scan(window, {"flat_field": False})

    assert response["accepted"] is True
    configuration, plan = created_threads[0].args
    assert configuration.overlap_fraction == pytest.approx(0.25)
    assert plan.overlap_fraction == pytest.approx(0.25)


def test_api_microscope_stitch_debug_scan_passes_overlap(monkeypatch) -> None:
    created_threads: list[_FakeScanThread] = []
    monkeypatch.setattr(
        "main.threading.Thread",
        lambda **kwargs: created_threads.append(_FakeScanThread(**kwargs))
        or created_threads[-1],
    )
    window = Main.__new__(Main)
    window.stage_controller = types.SimpleNamespace(
        is_busy=lambda: False,
        latest_stage_position=lambda: (10.0, 20.0, 3.0),
    )
    window._microscope_scan_running = lambda: False
    window._stage_serial_ready = lambda: True
    window._active_microscope_scale = lambda: types.SimpleNamespace(
        pixels_to_mm=((0.001, 0.0), (0.0, -0.001))
    )
    window._wait_for_camera_frame = lambda **_kwargs: (_FakeFrame(), 1)
    window._microscope_area_scan_default_output_dir = lambda: "C:/scan"
    window._microscope_scan_stop_requested = types.SimpleNamespace(clear=lambda: None)
    window._update_stage_coordinate_apply_state = lambda: None

    response = Main._api_microscope_area_scan(
        window,
        {
            "pattern": "stitch_debug",
            "structure_size_mm": 0.4,
            "overlap_fraction": 0.5,
            "flat_field": False,
        },
    )

    assert response["accepted"] is True
    configuration, plan = created_threads[0].args
    assert configuration.overlap_fraction == pytest.approx(0.5)
    assert plan.overlap_fraction == pytest.approx(0.5)
    expected_plan = microscope_scan.stitch_debug_scan_plan_from_pixel_matrix(
        center_stage_xy=(10.0, 20.0),
        frame_size_px=(1000, 800),
        pixels_to_mm=((0.001, 0.0), (0.0, -0.001)),
        structure_size_mm=0.4,
        placement_fraction=1.0,
        overlap_fraction=0.5,
    )
    assert _tile_signature(plan.tiles) == _tile_signature(expected_plan.tiles)
