from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

import pytest
from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera import (
    microscope_scan,
    microscope_scan_runtime,
    microscope_scan_runtime_adapters as adapters,
)
from probe_station_gui.camera.imaging import (
    MicroscopeCaptureResult,
    MicroscopeScanPlan,
    MicroscopeScanTile,
)


def _tile() -> MicroscopeScanTile:
    return MicroscopeScanTile(
        index=1,
        row=0,
        column=0,
        stage_xy=(1.25, -2.5),
    )


def _plan() -> MicroscopeScanPlan:
    return MicroscopeScanPlan(
        tiles=(_tile(),),
        stage_bounds=(0.0, 1.0, 2.0, 3.0),
        covered_stage_bounds=(0.0, 1.0, 2.0, 3.0),
        fov_size_mm=(2.0, 2.0),
        overlap_fraction=0.25,
        row_count=1,
        column_count=1,
    )


class _UnusedGrabber:
    pass


def _camera_adapter(
    *,
    correct_lens=lambda frame: frame,
    latest_frame_counter=lambda: 0,
    wait_for_frame=lambda **_kwargs: (None, 0),
    latest_raw_frame_counter=lambda: 0,
    wait_for_raw_frame=lambda **_kwargs: (None, 0),
) -> adapters.MicroscopeScanCameraAdapter:
    return adapters.MicroscopeScanCameraAdapter(
        grabber=_UnusedGrabber(),
        stop_event=threading.Event(),
        latest_frame_counter=latest_frame_counter,
        wait_for_frame=wait_for_frame,
        latest_raw_frame_counter=latest_raw_frame_counter,
        wait_for_raw_frame=wait_for_raw_frame,
        correct_lens=correct_lens,
        settings_timeout_s=0.1,
    )


def test_stage_adapter_uses_a_real_positive_approach_move() -> None:
    moves: list[tuple[float, float]] = []
    adapter = adapters.MicroscopeScanStageAdapter(
        reserve_task=lambda _name: type(
            "Lease", (), {"release": lambda _self: None}
        )(),
        read_reserved_position=lambda: (0.0, 0.0),
        raise_action=lambda _action, _feedrate: None,
        needle_feedrate=lambda: 10.0,
        move_xy=lambda x_mm, y_mm: moves.append((x_mm, y_mm)),
        latest_position=lambda: (0.0, 0.0),
    )

    adapter.move_to(_tile(), approach_mm=0.01)

    assert moves == [(1.24, -2.51), (1.25, -2.5)]


def test_camera_adapter_selects_the_fresh_raw_frame_for_scan_flat_fielding() -> None:
    raw = QImage(8, 6, QImage.Format_RGB32)
    raw.fill(QColor("red"))
    events: list[tuple[str, object]] = []
    adapter = _camera_adapter(
        latest_frame_counter=lambda: pytest.fail("corrected counter used"),
        wait_for_frame=lambda **_kwargs: pytest.fail("corrected frame used"),
        latest_raw_frame_counter=lambda: 12,
        wait_for_raw_frame=lambda **kwargs: (
            events.append(("raw", kwargs)) or (raw, 13)
        ),
    )

    captured = adapter.capture(raw=True)

    assert captured.pixelColor(0, 0) == QColor("red")
    assert events == [("raw", {"after_counter": 12, "timeout_s": 2.0})]


def test_camera_adapter_loads_reference_flat_field_images(tmp_path: Path) -> None:
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

    profile = _camera_adapter().flat_field_profile([], options)

    assert profile is not None
    assert profile.source == "reference"
    assert profile.image_size_px == (8, 6)


def test_camera_adapter_applies_flat_field_before_lens_correction(
    monkeypatch,
) -> None:
    source = QImage(8, 6, QImage.Format_RGB32)
    source.setText("space", "raw")
    events: list[str] = []

    def apply_flat(frame, profile):
        assert frame.text("space") == "raw"
        assert profile == "profile"
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

    monkeypatch.setattr(adapters, "apply_flat_field_correction", apply_flat)
    options = microscope_scan.FlatFieldScanOptions(enabled=True, mode="scan")

    corrected = _camera_adapter(correct_lens=apply_lens).correct(
        source,
        options,
        flat_field_profile="profile",
    )

    assert corrected.text("space") == "corrected"
    assert events == ["flat", "lens"]


def test_camera_adapter_normalizes_uppercase_self_flat_field_mode_once(
    monkeypatch,
) -> None:
    source = QImage(8, 6, QImage.Format_RGB32)
    source.setText("space", "raw")
    events: list[str] = []

    def apply_self(frame, *, blur_radius_px, max_gain):
        assert frame.text("space") == "raw"
        assert blur_radius_px == 9
        assert max_gain == pytest.approx(3.0)
        events.append("self")
        return frame.copy()

    monkeypatch.setattr(adapters, "apply_self_flat_field_correction", apply_self)
    options = microscope_scan.FlatFieldScanOptions(
        enabled=True,
        mode="SELF",
        blur_radius_px=9,
        max_gain=3.0,
    )
    adapter = _camera_adapter(
        correct_lens=lambda frame: events.append("lens") or frame,
    )

    profile = adapter.flat_field_profile([], options)
    corrected = adapter.correct(source, options, flat_field_profile=profile)

    assert corrected.size() == source.size()
    assert events == ["self", "lens"]


def test_camera_adapter_still_rejects_an_unsupported_flat_field_mode() -> None:
    options = microscope_scan.FlatFieldScanOptions(enabled=True, mode="UNKNOWN")
    adapter = _camera_adapter(
        correct_lens=lambda _frame: pytest.fail("lens correction used"),
    )

    with pytest.raises(RuntimeError, match="Unsupported flat-field mode: unknown"):
        adapter.flat_field_profile([], options)
    with pytest.raises(RuntimeError, match="Unsupported flat-field mode: unknown"):
        adapter.correct(
            QImage(8, 6, QImage.Format_RGB32),
            options,
            flat_field_profile=None,
        )


@dataclass(frozen=True)
class _LaunchSnapshot:
    objective_name: str
    magnification: float
    scan_name: str
    scale: object
    document_identity: str
    objective_xy_offset: tuple[float, float]

    def metadata(self) -> dict[str, object]:
        return {
            "objective_name": self.objective_name,
            "magnification": self.magnification,
            "scan_name": self.scan_name,
            "document_identity": self.document_identity,
            "objective_xy_offset_mm": list(self.objective_xy_offset),
        }

    def design_to_raw_stage(
        self,
        design_xy: tuple[float, float],
    ) -> tuple[float, float]:
        return (
            design_xy[0] + self.objective_xy_offset[0],
            design_xy[1] + self.objective_xy_offset[1],
        )

    def raw_stage_to_design(
        self,
        raw_stage_xy: tuple[float, float],
    ) -> tuple[float, float]:
        return (
            raw_stage_xy[0] - self.objective_xy_offset[0],
            raw_stage_xy[1] - self.objective_xy_offset[1],
        )


def _run_request(
    output_dir: Path,
    *,
    scale: object,
    plan: MicroscopeScanPlan | None = None,
    plan_factory=None,
) -> microscope_scan_runtime.MicroscopeScanRunRequest:
    return microscope_scan_runtime.MicroscopeScanRunRequest(
        output_dir=output_dir,
        scale=scale,
        plan=plan,
        plan_factory=plan_factory,
        flat_field_options=microscope_scan.FlatFieldScanOptions(enabled=False),
        camera_lock_settings=microscope_scan.CameraLockSettings(enabled=False),
        settle_s=0.0,
        tile_approach_mm=0.0,
        refine_scale_from_overlaps=False,
    )


def test_artifact_adapter_uses_immutable_launch_data_during_saves(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mutable_source = {
        "objective_name": "X5",
        "magnification": 5.0,
        "scan_name": "design-a",
        "document_identity": str(tmp_path / "design-a.gds"),
        "objective_xy_offset": (0.5, -0.25),
    }
    launch_scale = object()
    launch = _LaunchSnapshot(scale=launch_scale, **mutable_source)
    adapter = adapters.MicroscopeScanArtifactAdapter(
        launch=launch,
        stage_position_for_metadata=lambda *, stage_xy: (*stage_xy, 3.0),
    )
    saved: list[tuple[str, object, object]] = []

    def save_image(*, frame, filename_stem, metadata, scale, **_kwargs):
        saved.append((filename_stem, metadata, scale))
        return MicroscopeCaptureResult(
            image_path=tmp_path / f"{filename_stem}.png",
            metadata_path=tmp_path / f"{filename_stem}.json",
            raw_image=frame,
            metadata=metadata.to_dict(),
        )

    monkeypatch.setattr(adapters, "save_microscope_image", save_image)
    mutable_source.update(
        objective_name="X99",
        magnification=99.0,
        scan_name="design-b",
        document_identity=str(tmp_path / "design-b.gds"),
        objective_xy_offset=(50.0, 75.0),
    )
    frame = QImage(8, 6, QImage.Format_RGB32)
    request = _run_request(tmp_path, scale=launch_scale, plan=_plan())
    captured = microscope_scan_runtime.CapturedFrame(
        tile=_tile(),
        frame=frame,
        captured_at="2026-07-21T00:00:00+00:00",
        actual_stage_position=(1.25, -2.5, 3.0),
    )

    adapter.save_tile(captured, _plan(), request, {})
    adapter.save_mosaic(frame, _plan(), request, launch_scale, {})

    assert len(saved) == 2
    tile_name, tile_metadata, tile_scale = saved[0]
    mosaic_name, mosaic_metadata, mosaic_scale = saved[1]
    assert tile_name.startswith("design-a_")
    assert mosaic_name.startswith("design-a_mosaic_")
    assert tile_scale is launch_scale
    assert mosaic_scale is launch_scale
    assert tile_metadata.objective_name == "X5"
    assert mosaic_metadata.objective_name == "X5"
    assert tile_metadata.magnification == pytest.approx(5.0)
    assert mosaic_metadata.magnification == pytest.approx(5.0)
    assert tile_metadata.design_xy == pytest.approx((0.75, -2.25))
    assert tile_metadata.extra["scan_launch"]["document_identity"].endswith(
        "design-a.gds"
    )
    assert mosaic_metadata.extra["scan_launch"] == tile_metadata.extra["scan_launch"]


class _Signal:
    def __init__(self) -> None:
        self.callback = None

    def connect(self, callback, _connection_type) -> None:
        self.callback = callback

    def disconnect(self, callback) -> None:
        assert callback is self.callback
        self.callback = None


class _Grabber:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.camera_settings_override_changed = _Signal()

    def request_temporary_camera_settings(self, settings, *, restore_key) -> None:
        self.events.append("camera:lock")
        assert settings == (("GainAuto", "Off"),)
        self.camera_settings_override_changed.callback(
            {"ok": True, "restore_key": restore_key}
        )

    def request_restore_camera_settings(self, *, restore_key) -> None:
        self.events.append("camera:restore")
        self.camera_settings_override_changed.callback(
            {"ok": True, "restore_key": restore_key}
        )


class _Lease:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def snapshot(self):
        self.events.append("session:snapshot")
        return {"operation": "microscope scan"}

    def close(self):
        self.events.append("session:close")
        return {}


class _SessionManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def open(self, operation: str, parent_token=None):
        self.events.append("session:open")
        assert operation == "microscope scan"
        assert parent_token is None
        return _Lease(self.events)


def test_production_adapters_preserve_session_stage_planning_frame_order(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    frame = QImage(8, 6, QImage.Format_RGB32)
    launch = _LaunchSnapshot(
        objective_name="X5",
        magnification=5.0,
        scan_name="ordered-scan",
        scale=object(),
        document_identity="ordered-scan.gds",
        objective_xy_offset=(0.0, 0.0),
    )
    frame_counter_calls = 0

    def latest_frame_counter() -> int:
        nonlocal frame_counter_calls
        frame_counter_calls += 1
        events.append(
            "camera:planning-counter"
            if frame_counter_calls == 1
            else "camera:capture-counter"
        )
        return frame_counter_calls

    def wait_for_frame(**_kwargs):
        events.append(
            "camera:planning-frame"
            if frame_counter_calls == 1
            else "camera:capture-frame"
        )
        return frame, frame_counter_calls + 1

    def save_image(*, frame, filename_stem, metadata, **_kwargs):
        events.append("artifacts:save")
        return MicroscopeCaptureResult(
            image_path=tmp_path / f"{filename_stem}.png",
            metadata_path=tmp_path / f"{filename_stem}.json",
            raw_image=frame,
            metadata=metadata.to_dict(),
        )

    monkeypatch.setattr(adapters, "save_microscope_image", save_image)
    monkeypatch.setattr(
        adapters,
        "stitch_scan_tiles",
        lambda **_kwargs: events.append("artifacts:stitch") or frame,
    )
    monkeypatch.setattr(
        adapters.microscope_scan,
        "write_manifest",
        lambda **_kwargs: events.append("artifacts:manifest")
        or tmp_path / "manifest.json",
    )
    runtime = microscope_scan_runtime.MicroscopeScanRuntime(
        stage=adapters.MicroscopeScanStageAdapter(
            reserve_task=lambda _name: (
                events.append("stage:begin")
                or type(
                    "Lease",
                    (),
                    {"release": lambda _self: events.append("stage:release")},
                )()
            ),
            read_reserved_position=lambda: events.append("stage:position")
            or (10.0, 20.0, 3.0),
            raise_action=lambda _action, _feedrate: events.append("stage:raise"),
            needle_feedrate=lambda: 10.0,
            move_xy=lambda _x, _y: events.append("stage:move"),
            latest_position=lambda: (1.25, -2.5, 3.0),
        ),
        camera=adapters.MicroscopeScanCameraAdapter(
            grabber=_Grabber(events),
            stop_event=threading.Event(),
            latest_frame_counter=latest_frame_counter,
            wait_for_frame=wait_for_frame,
            latest_raw_frame_counter=lambda: pytest.fail("raw counter used"),
            wait_for_raw_frame=lambda **_kwargs: pytest.fail("raw frame used"),
            correct_lens=lambda image: image,
            settings_timeout_s=0.1,
        ),
        artifacts=adapters.MicroscopeScanArtifactAdapter(
            launch=launch,
            stage_position_for_metadata=lambda *, stage_xy: (*stage_xy, 3.0),
        ),
        event_sink=adapters.MicroscopeScanEventAdapter(
            status_callback=lambda _message: None,
            finished_callback=lambda success, _message: events.append(
                f"finished:{success}"
            ),
        ),
        session=adapters.MicroscopeScanSessionAdapter(_SessionManager(events)),
    )
    request = _run_request(
        tmp_path,
        scale=launch.scale,
        plan_factory=lambda frame_size, start_xy: (
            events.append(f"plan:{frame_size}:{start_xy}") or _plan()
        ),
    )
    request = microscope_scan_runtime.MicroscopeScanRunRequest(
        **{
            **request.__dict__,
            "camera_lock_settings": microscope_scan.CameraLockSettings(
                enabled=True,
                settings=(("GainAuto", "Off"),),
            ),
        }
    )

    runtime.run(request)

    assert events[:7] == [
        "session:open",
        "session:snapshot",
        "stage:begin",
        "stage:position",
        "camera:planning-counter",
        "camera:planning-frame",
        "plan:(8, 6):(10.0, 20.0)",
    ]
    assert events.index("plan:(8, 6):(10.0, 20.0)") < events.index("camera:lock")
    assert events.index("camera:lock") < events.index("stage:raise")
    assert events.index("artifacts:save") < events.index("artifacts:stitch")
    assert events.index("artifacts:manifest") < events.index("camera:restore")
    assert events.index("camera:restore") < events.index("stage:release")
    assert events.index("stage:release") < events.index("session:close")
    assert events[-1] == "finished:True"
