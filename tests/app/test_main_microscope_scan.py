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


def test_microscope_scan_capture_uses_raw_space_for_scan_flat_field() -> None:
    window = Main.__new__(Main)
    raw = QImage(8, 6, QImage.Format_RGB32)
    raw.fill(QColor("red"))
    events: list[tuple[str, object]] = []
    window._latest_raw_camera_counter = lambda: 12
    window._wait_for_raw_camera_frame = (
        lambda **kwargs: events.append(("raw", kwargs)) or (raw, 13)
    )
    window._latest_camera_counter = lambda: pytest.fail("corrected counter used")
    window._wait_for_camera_frame = lambda **_kwargs: pytest.fail(
        "corrected frame used"
    )

    captured = Main._capture_microscope_scan_frame(window, raw=True)

    assert captured.pixelColor(0, 0) == QColor("red")
    assert events == [("raw", {"after_counter": 12, "timeout_s": 2.0})]


def test_microscope_scan_applies_flat_field_before_lens_correction() -> None:
    window = Main.__new__(Main)
    events: list[str] = []
    source = QImage(8, 6, QImage.Format_RGB32)
    source.setText("space", "raw")

    def apply_flat(frame, _options, *, flat_field_profile):
        assert frame.text("space") == "raw"
        assert flat_field_profile == "profile"
        events.append("flat")
        result = frame.copy()
        result.setText("space", "flat")
        return result

    def apply_lens(frame):
        assert frame.text("space") == "flat"
        events.append("lens")
        result = frame.copy()
        result.setText("space", "corrected")
        return result

    window._flat_field_microscope_scan_frame = apply_flat
    window._correct_camera_frame_for_active_objective = apply_lens

    corrected = Main._correct_microscope_scan_frame(
        window,
        source,
        object(),
        flat_field_profile="profile",
    )

    assert corrected.text("space") == "corrected"
    assert events == ["flat", "lens"]


def test_microscope_scan_runs_auto_exposure_before_stage_and_motion(tmp_path) -> None:
    events: list[object] = []
    window = _scan_runner_window(events)
    window._run_camera_auto_exposure = (
        lambda: events.append("auto_exposure")
        or {
            "accepted": True,
            "converged": True,
            "final_exposure_us": 2500.0,
        }
    )
    configuration = _scan_runner_configuration(tmp_path, auto_exposure=True)
    plan = types.SimpleNamespace(
        tiles=(types.SimpleNamespace(index=1, stage_xy=(1.0, 2.0)),)
    )

    Main._run_microscope_scan(window, configuration, plan)

    assert events[:5] == [
        "auto_exposure",
        "begin",
        "camera_lock",
        "raise",
        ("move", 1.0, 2.0),
    ]
    assert window.finished[-1][0] is False


def test_microscope_scan_auto_exposure_rejection_never_acquires_stage(
    tmp_path,
) -> None:
    events: list[object] = []
    window = _scan_runner_window(events)
    window._run_camera_auto_exposure = lambda: {
        "accepted": False,
        "message": "Camera auto exposure did not converge.",
    }
    configuration = _scan_runner_configuration(tmp_path, auto_exposure=True)
    plan = types.SimpleNamespace(tiles=())

    Main._run_microscope_scan(window, configuration, plan)

    assert events == []
    assert window.finished == [
        (False, "Microscope scan failed: Camera auto exposure did not converge.")
    ]


def test_microscope_scan_disabled_auto_exposure_starts_with_camera_lock(
    tmp_path,
) -> None:
    events: list[object] = []
    window = _scan_runner_window(events)
    window._run_camera_auto_exposure = lambda: pytest.fail(
        "auto exposure should be disabled"
    )
    configuration = _scan_runner_configuration(tmp_path, auto_exposure=False)
    plan = types.SimpleNamespace(
        tiles=(types.SimpleNamespace(index=1, stage_xy=(1.0, 2.0)),)
    )

    Main._run_microscope_scan(window, configuration, plan)

    assert events[:4] == [
        "begin",
        "camera_lock",
        "raise",
        ("move", 1.0, 2.0),
    ]


def test_scan_corrections_metadata_serializes_auto_exposure_result() -> None:
    result = {
        "accepted": True,
        "converged": True,
        "final_exposure_us": 2400.0,
    }

    corrections = Main._microscope_scan_corrections_metadata(
        flat_field_options=microscope_scan.FlatFieldScanOptions(enabled=False),
        camera_lock_settings=microscope_scan.CameraLockSettings(enabled=False),
        auto_exposure_options=microscope_scan.AutoExposureScanOptions(enabled=True),
        auto_exposure_result=result,
    )

    assert corrections["auto_exposure"] == {
        "enabled": True,
        **result,
    }


def test_camera_auto_exposure_frame_reader_uses_fresh_raw_frame() -> None:
    window = Main.__new__(Main)
    image = QImage(8, 6, QImage.Format_RGB32)
    image.fill(QColor(12, 34, 56))
    calls: list[dict[str, object]] = []
    window._wait_for_raw_camera_frame = (
        lambda **kwargs: calls.append(kwargs) or (image, 18)
    )

    frame = Main._read_camera_auto_exposure_frame(window, 17, 1.25)

    assert calls == [{"after_counter": 17, "timeout_s": 1.25}]
    assert frame.counter == 18
    assert frame.rgb.shape == (6, 8, 3)
    assert frame.rgb.flags.owndata is True
    assert frame.rgb[0, 0].tolist() == [12, 34, 56]


def _scan_runner_window(events: list[object]):
    window = Main.__new__(Main)
    window._active_microscope_scale = lambda: object()
    window._microscope_scan_stop_requested = types.SimpleNamespace(
        is_set=lambda: False
    )
    window._apply_microscope_scan_camera_lock = (
        lambda _settings: events.append("camera_lock") or None
    )
    window._current_needle_feedrate = lambda: 10.0
    window._sleep_microscope_scan_settle = lambda _settle: False
    window._microscope_scan_actual_position = lambda: (1.0, 2.0)
    window.microscope_scan_status = types.SimpleNamespace(emit=lambda _message: None)
    window.finished = []
    window.microscope_scan_finished = types.SimpleNamespace(
        emit=lambda success, message: window.finished.append((success, message))
    )
    window.stage_controller = types.SimpleNamespace(
        begin_external_task=lambda _name: events.append("begin"),
        run_external_needles_action=lambda *_args: events.append("raise"),
        run_external_move_to_xy=lambda x, y: events.append(("move", x, y)),
        finish_external_task=lambda: events.append("finish"),
    )
    return window


def _scan_runner_configuration(tmp_path, *, auto_exposure: bool):
    return types.SimpleNamespace(
        output_dir=str(tmp_path / "scan"),
        overlap_fraction=0.25,
        settle_s=0.0,
        tile_approach_mm=0.0,
        flat_field_options=microscope_scan.FlatFieldScanOptions(enabled=False),
        camera_lock_settings=microscope_scan.CameraLockSettings(enabled=True),
        auto_exposure_options=microscope_scan.AutoExposureScanOptions(
            enabled=auto_exposure
        ),
    )


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
    assert configuration.auto_exposure_options.enabled is True
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
            "auto_exposure": False,
        },
    )

    assert response["accepted"] is True
    configuration, plan = created_threads[0].args
    assert configuration.overlap_fraction == pytest.approx(0.5)
    assert configuration.auto_exposure_options.enabled is False
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
