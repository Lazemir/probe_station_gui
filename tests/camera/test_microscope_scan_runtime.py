from __future__ import annotations

from pathlib import Path
import types
from typing import get_type_hints

from probe_station_gui.camera import microscope_scan, microscope_scan_runtime
from probe_station_gui.camera.imaging import MicroscopeScanPlan, MicroscopeScanTile
from probe_station_gui.camera import microscope_scan_runtime_adapters


class _StageAdapter:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def reserve(self) -> tuple[float, float]:
        self._events.append("stage:reserve")
        return (10.0, 20.0)

    def raise_needles(self) -> None:
        self._events.append("stage:raise")

    def move_to(self, tile: MicroscopeScanTile, *, approach_mm: float) -> None:
        self._events.append("stage:move")

    def actual_position(self) -> tuple[float, ...]:
        return (1.0, 2.0, 3.0)

    def return_to(self, start_xy: tuple[float, float]) -> None:
        self._events.append("stage:return")

    def release(self) -> None:
        self._events.append("stage:release")


class _FailingCameraAdapter:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def planning_frame_size(self) -> tuple[int, int]:
        return (8, 6)

    def apply_lock(self, settings: microscope_scan.CameraLockSettings) -> object:
        self._events.append("camera:lock")
        return "camera-lock"

    def settle(self, settle_s: float) -> bool:
        return True

    def capture(self, *, raw: bool) -> object:
        raise RuntimeError("capture failed")

    def stop_requested(self) -> bool:
        return False

    def flat_field_profile(self, captured_frames, options):
        raise AssertionError("capture failure must not build a profile")

    def correct(self, frame, options, *, flat_field_profile):
        raise AssertionError("capture failure must not correct a frame")

    def restore_lock(self, restore_key: object) -> None:
        self._events.append("camera:restore")


class _ArtifactAdapter:
    def __init__(self, events: list[str]) -> None:
        self._events = events


class _EventSink:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def status(self, message: str) -> None:
        self._events.append(f"status:{message}")

    def finished(self, success: bool, message: str) -> None:
        self._events.append(f"finished:{str(success).lower()}:{message}")


class _SessionAdapter:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def acquire(self) -> dict[str, object]:
        self._events.append("session:acquire")
        return {"operation": "microscope scan"}

    def release(self) -> None:
        self._events.append("session:release")


def _single_tile_request() -> microscope_scan_runtime.MicroscopeScanRunRequest:
    tile = MicroscopeScanTile(index=1, row=0, column=0, stage_xy=(1.0, 2.0))
    plan = MicroscopeScanPlan(
        tiles=(tile,),
        stage_bounds=(0.0, 1.0, 2.0, 3.0),
        covered_stage_bounds=(0.0, 1.0, 2.0, 3.0),
        fov_size_mm=(2.0, 2.0),
        overlap_fraction=0.25,
        row_count=1,
        column_count=1,
    )
    return microscope_scan_runtime.MicroscopeScanRunRequest(
        output_dir=Path("scan"),
        scale=object(),
        plan=plan,
        flat_field_options=microscope_scan.FlatFieldScanOptions(enabled=False),
        camera_lock_settings=microscope_scan.CameraLockSettings(
            enabled=True,
            settings=(("GainAuto", "Off"),),
        ),
        settle_s=0.0,
        tile_approach_mm=0.0,
        refine_scale_from_overlaps=False,
    )


def test_capture_failure_returns_stage_before_camera_restore_and_session_release() -> None:
    events: list[str] = []
    runtime = microscope_scan_runtime.MicroscopeScanRuntime(
        stage=_StageAdapter(events),
        camera=_FailingCameraAdapter(events),
        artifacts=_ArtifactAdapter(events),
        event_sink=_EventSink(events),
        session=_SessionAdapter(events),
    )

    runtime.run(_single_tile_request())

    assert events.index("stage:return") < events.index("camera:restore")
    assert events.index("camera:restore") < events.index("session:release")
    assert events[-1].startswith("finished:false:")


def test_runtime_ports_use_explicit_artifact_result_and_adapters_are_separate() -> None:
    hints = get_type_hints(microscope_scan_runtime.ArtifactPort.save_tile)

    assert hints["return"] is microscope_scan_runtime.MicroscopeCaptureResult
    assert not hasattr(microscope_scan_runtime, "MicroscopeScanCameraAdapter")
    assert hasattr(microscope_scan_runtime_adapters, "MicroscopeScanCameraAdapter")
    assert hasattr(
        microscope_scan_runtime_adapters.ScanLaunchPort,
        "design_to_raw_stage",
    )


class _RecordingStage(_StageAdapter):
    def __init__(
        self,
        events: list[str],
        *,
        return_error: str = "",
    ) -> None:
        super().__init__(events)
        self._return_error = return_error

    def return_to(self, start_xy: tuple[float, float]) -> None:
        self._events.append("stage:return")
        if self._return_error:
            raise RuntimeError(self._return_error)


class _RecordingCamera(_FailingCameraAdapter):
    def __init__(
        self,
        events: list[str],
        *,
        restore_error: str = "",
        stop_requested: bool = False,
        exposure_state: dict[str, object] | None = None,
    ) -> None:
        super().__init__(events)
        self._restore_error = restore_error
        self._stop_requested = stop_requested
        self._exposure_state = exposure_state

    def stop_requested(self) -> bool:
        return self._stop_requested

    def capture(self, *, raw: bool) -> object:
        exposure = self._exposure_state or {}
        self._events.append(
            f"camera:capture:{exposure.get('auto')}:{exposure.get('time')}"
        )
        return object()

    def flat_field_profile(self, captured_frames, options):
        self._events.append("camera:profile")
        return None

    def correct(self, frame, options, *, flat_field_profile):
        self._events.append("camera:correct")
        return frame

    def restore_lock(self, restore_key: object) -> None:
        super().restore_lock(restore_key)
        if self._restore_error:
            raise RuntimeError(self._restore_error)


class _RecordingArtifacts(_ArtifactAdapter):
    def save_tile(self, captured, plan, request, corrections):
        self._events.append(f"artifacts:tile:{captured.tile.index}")
        return types.SimpleNamespace(
            raw_image=captured.frame,
            image_path=request.output_dir / f"tile-{captured.tile.index}.png",
            metadata_path=request.output_dir / f"tile-{captured.tile.index}.json",
        )

    def refine_scale(self, captured_tiles, scale):
        self._events.append("artifacts:refine")
        return scale

    def stitch(self, plan, tile_images, scale):
        self._events.append("artifacts:stitch")
        return object()

    def save_mosaic(
        self,
        mosaic,
        plan,
        request,
        scale,
        corrections,
        *,
        filename_suffix="",
        extra=None,
    ):
        self._events.append(f"artifacts:mosaic:{filename_suffix}")
        return types.SimpleNamespace(
            raw_image=mosaic,
            image_path=request.output_dir / f"mosaic{filename_suffix}.png",
            metadata_path=request.output_dir / f"mosaic{filename_suffix}.json",
        )

    def write_manifest(
        self,
        request,
        plan,
        tile_results,
        mosaic_result,
        corrections,
        diagnostic_mosaics,
    ):
        self._events.append("artifacts:manifest")
        return request.output_dir / "manifest.json"


class _RecordingSession(_SessionAdapter):
    def __init__(
        self,
        events: list[str],
        *,
        acquire_error: str = "",
        exposure_state: dict[str, object] | None = None,
    ) -> None:
        super().__init__(events)
        self._acquire_error = acquire_error
        self._exposure_state = exposure_state

    def acquire(self) -> dict[str, object]:
        snapshot = super().acquire()
        if self._acquire_error:
            raise RuntimeError(self._acquire_error)
        if self._exposure_state is not None:
            self._exposure_state.update(auto="Off", time=2600.0)
        return snapshot

    def release(self) -> None:
        super().release()
        if self._exposure_state is not None:
            self._exposure_state.update(auto="Continuous", time=1800.0)


def _runtime(
    events: list[str],
    *,
    stage=None,
    camera=None,
    artifacts=None,
    session=None,
) -> microscope_scan_runtime.MicroscopeScanRuntime:
    return microscope_scan_runtime.MicroscopeScanRuntime(
        stage=stage or _RecordingStage(events),
        camera=camera or _RecordingCamera(events),
        artifacts=artifacts or _RecordingArtifacts(events),
        event_sink=_EventSink(events),
        session=session or _RecordingSession(events),
    )


def test_session_rejection_never_reserves_stage_or_locks_camera(tmp_path) -> None:
    events: list[str] = []
    runtime = _runtime(
        events,
        session=_RecordingSession(events, acquire_error="Exposure did not converge."),
    )

    runtime.run(_single_tile_request_for(tmp_path))

    assert events[0] == "status:Microscope scan: fixing exposure."
    assert events[1] == "session:acquire"
    assert "stage:reserve" not in events
    assert "camera:lock" not in events
    assert events[-1].endswith("Exposure did not converge.")


def test_stop_request_returns_stage_without_capturing(tmp_path) -> None:
    events: list[str] = []
    runtime = _runtime(
        events,
        camera=_RecordingCamera(events, stop_requested=True),
    )

    runtime.run(_single_tile_request_for(tmp_path))

    assert "stage:move" not in events
    assert not any(event.startswith("camera:capture") for event in events)
    assert events.index("stage:return") < events.index("camera:restore")
    assert events[-1].startswith("finished:false:Microscope scan stopped by user.")


def test_return_failure_preserves_capture_failure(tmp_path) -> None:
    events: list[str] = []
    runtime = _runtime(
        events,
        stage=_RecordingStage(events, return_error="return jammed"),
        camera=_FailingCameraAdapter(events),
    )

    runtime.run(_single_tile_request_for(tmp_path))

    message = events[-1].lower()
    assert "capture failed" in message
    assert "return to start failed: return jammed" in message
    assert events.index("stage:return") < events.index("camera:restore")


def test_restore_failure_changes_successful_run_to_failure(tmp_path) -> None:
    events: list[str] = []
    runtime = _runtime(
        events,
        camera=_RecordingCamera(events, restore_error="restore failed"),
    )

    runtime.run(_single_tile_request_for(tmp_path))

    assert events.index("stage:return") < events.index("camera:restore")
    assert events[-1].startswith("finished:false:")
    assert "camera settings restore failed: restore failed" in events[-1].lower()


def test_every_tile_uses_session_fixed_exposure(tmp_path) -> None:
    events: list[str] = []
    exposure = {"auto": "Continuous", "time": 1800.0}
    runtime = _runtime(
        events,
        camera=_RecordingCamera(events, exposure_state=exposure),
        session=_RecordingSession(events, exposure_state=exposure),
    )
    request = _single_tile_request_for(tmp_path, tile_count=2)

    runtime.run(request)

    assert [event for event in events if event.startswith("camera:capture")] == [
        "camera:capture:Off:2600.0",
        "camera:capture:Off:2600.0",
    ]
    assert exposure == {"auto": "Continuous", "time": 1800.0}
    assert events[-1].startswith("finished:true:")


def test_success_saves_tiles_then_mosaic_then_manifest(tmp_path) -> None:
    events: list[str] = []

    _runtime(events).run(_single_tile_request_for(tmp_path, tile_count=2))

    tile_1 = events.index("artifacts:tile:1")
    tile_2 = events.index("artifacts:tile:2")
    mosaic = events.index("artifacts:mosaic:")
    manifest = events.index("artifacts:manifest")
    assert tile_1 < tile_2 < mosaic < manifest < events.index("stage:return")
    assert "status:Microscope scan: locking camera settings." in events
    assert "status:Microscope scan: restoring camera settings." in events
    assert events[-1].startswith("finished:true:Microscope scan complete:")


def test_session_adapter_closes_lease_when_snapshot_fails() -> None:
    events: list[str] = []

    class _Lease:
        def snapshot(self):
            raise RuntimeError("snapshot failed")

        def close(self):
            events.append("session:close")
            return {"accepted": True}

    class _Manager:
        def open(self, operation: str, parent_token=None):
            assert operation == "microscope scan"
            assert parent_token is None
            return _Lease()

    adapter = microscope_scan_runtime_adapters.MicroscopeScanSessionAdapter(_Manager())

    try:
        adapter.acquire()
    except RuntimeError as exc:
        assert str(exc) == "snapshot failed"
    else:
        raise AssertionError("snapshot failure was not propagated")

    assert events == ["session:close"]


def _single_tile_request_for(
    output_dir: Path,
    *,
    tile_count: int = 1,
) -> microscope_scan_runtime.MicroscopeScanRunRequest:
    base = _single_tile_request()
    tiles = tuple(
        MicroscopeScanTile(
            index=index,
            row=0,
            column=index - 1,
            stage_xy=(float(index), float(index + 1)),
        )
        for index in range(1, tile_count + 1)
    )
    plan = MicroscopeScanPlan(
        tiles=tiles,
        stage_bounds=base.plan.stage_bounds,
        covered_stage_bounds=base.plan.covered_stage_bounds,
        fov_size_mm=base.plan.fov_size_mm,
        overlap_fraction=base.plan.overlap_fraction,
        row_count=1,
        column_count=tile_count,
    )
    return microscope_scan_runtime.MicroscopeScanRunRequest(
        output_dir=output_dir,
        scale=base.scale,
        plan=plan,
        flat_field_options=base.flat_field_options,
        camera_lock_settings=base.camera_lock_settings,
        settle_s=base.settle_s,
        tile_approach_mm=base.tile_approach_mm,
        refine_scale_from_overlaps=False,
    )
