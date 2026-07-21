"""Thread-owned flat-field and lens calibration transactions."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

from PySide6.QtGui import QImage

from probe_station_gui.camera.distortion import GridCalibrationFrame
from probe_station_gui.camera.optical_calibration_geometry import (
    LensFitArtifact,
    LensFitLimits,
    PixelMatrix,
    fit_lens_artifact,
    flat_field_capture_offsets_mm,
    frame_size,
    lens_capture_offsets_mm,
    lens_success_message,
)
from probe_station_gui.camera.imaging import utc_timestamp


from probe_station_gui.camera.optical_calibration_adapters import (
    CalibrationStartDecision,
    CameraPort,
    FlatFieldCalibrationRequest,
    FlatFieldStorePort,
    LensCalibrationArtifact,
    LensDistortionCalibrationRequest,
    OpticalCalibrationEventPort,
    OpticalCalibrationOutcome,
    OpticalCalibrationProgress,
    OpticalCalibrationState,
    OpticalSessionLeasePort,
    OpticalSessionPort,
    StagePort,
)
from probe_station_gui.camera.optical_calibration_lifecycle import (
    OpticalCalibrationLifecycle,
)
ThreadFactory = Callable[..., threading.Thread]
Request = FlatFieldCalibrationRequest | LensDistortionCalibrationRequest


class OpticalCalibrationRuntime:
    """Own one optical worker plus the retained full-wizard exposure lease."""

    def __init__(
        self,
        *,
        stage: StagePort,
        camera: CameraPort,
        sessions: OpticalSessionPort,
        store: FlatFieldStorePort,
        events: OpticalCalibrationEventPort,
        thread_factory: ThreadFactory = threading.Thread,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._stage = stage
        self._camera = camera
        self._sessions = sessions
        self._store = store
        self._events = events
        self._lifecycle = OpticalCalibrationLifecycle(
            sessions=sessions,
            events=events,
            thread_factory=thread_factory,
            sleep=sleep,
        )

    def start_flat(
        self,
        request: FlatFieldCalibrationRequest,
    ) -> CalibrationStartDecision:
        return self._lifecycle.start(request, "flat", self._run_flat)

    def start_lens(
        self,
        request: LensDistortionCalibrationRequest,
    ) -> CalibrationStartDecision:
        return self._lifecycle.start(request, "lens", self._run_lens)

    def cancel(self, run_id: str | None = None) -> None:
        self._lifecycle.cancel(run_id)

    def state(self) -> OpticalCalibrationState:
        return self._lifecycle.state()

    def shutdown(self, timeout_s: float) -> bool:
        return self._lifecycle.shutdown(timeout_s)

    def _run_flat(self, raw_request: Request) -> None:
        request = raw_request
        assert isinstance(request, FlatFieldCalibrationRequest)
        outcome = self._flat_transaction(request)
        self._finish_run(request, outcome)

    def _flat_transaction(
        self,
        request: FlatFieldCalibrationRequest,
    ) -> OpticalCalibrationOutcome:
        child: OpticalSessionLeasePort | None = None
        reserved = False
        camera_key: str | None = None
        moved = False
        start_xy: tuple[float, float] | None = None
        payload: dict[str, object] | None = None
        success = False
        message = "Flat-field calibration stopped."
        restore_warning = ""
        try:
            child = self._lifecycle.open_child_session(
                "flat-field calibration", request
            )
            snapshot = child.snapshot()
            self._check_cancelled("Flat-field")
            self._stage.reserve("flat-field calibration")
            reserved = True
            start_xy = self._stage.start_position()
            initial = self._require_frame(None, request.camera_timeout_s)
            size_px = frame_size(initial)
            offsets = flat_field_capture_offsets_mm(
                size_px,
                request.pixel_size_mm,
                request.overlap_fraction,
            )
            camera_key = self._camera.lock()
            self._progress(request, "Flat-field calibration: raising needles.")
            self._stage.raise_needles(request.needle_feedrate)
            frames, moved = self._capture_flat_grid(request, start_xy, offsets)
            stored = self._store.install(
                request.objective_name,
                frames,
                self._flat_metadata(request, start_xy, offsets, snapshot),
            )
            payload = {
                "objective": request.objective_name,
                "current_manifest": str(getattr(stored, "current_manifest", "")),
                "reference_image": str(getattr(stored, "reference_image", "")),
            }
            success = True
            message = "Flat-field calibration saved."
        except Exception as exc:
            message = f"Flat-field calibration failed: {exc}"
        finally:
            success, message, restore_warning = self._cleanup(
                kind="flat",
                request=request,
                child=child,
                reserved=reserved,
                moved=moved,
                start_xy=start_xy,
                camera_key=camera_key,
                success=success,
                message=message,
            )
        return OpticalCalibrationOutcome(
            request.run_id,
            request.wizard_run_id,
            "flat",
            success,
            message,
            request.objective_name,
            request.full_wizard,
            flat_payload=payload,
            restore_warning=restore_warning,
        )

    def _run_lens(self, raw_request: Request) -> None:
        request = raw_request
        assert isinstance(request, LensDistortionCalibrationRequest)
        outcome = self._lens_transaction(request)
        self._finish_run(request, outcome)

    def _lens_transaction(
        self,
        request: LensDistortionCalibrationRequest,
    ) -> OpticalCalibrationOutcome:
        child: OpticalSessionLeasePort | None = None
        reserved = False
        camera_key: str | None = None
        moved = False
        start_xy: tuple[float, float] | None = None
        artifact: LensCalibrationArtifact | None = None
        success = False
        message = "Lens distortion calibration stopped."
        restore_warning = ""
        try:
            child = self._lifecycle.open_child_session(
                "lens distortion calibration", request
            )
            snapshot = child.snapshot()
            self._check_cancelled("Lens distortion")
            self._stage.reserve("lens distortion calibration")
            reserved = True
            start_xy = self._stage.start_position()
            camera_key = self._camera.lock()
            self._progress(request, "Lens distortion calibration: raising needles.")
            self._stage.raise_needles(request.needle_feedrate)
            initial = self._require_frame(None, request.camera_timeout_s)
            size_px = frame_size(initial)
            offsets = lens_capture_offsets_mm(
                size_px,
                request.pixels_to_mm,
                request.pixel_size_mm,
                grid_size=request.grid_size,
                fov_fraction=request.fov_fraction,
            )
            frames, moved = self._capture_lens_grid(request, start_xy, offsets, size_px)
            self._return_to_start(request, start_xy)
            moved = False
            fitted = fit_lens_artifact(
                frames,
                size_px=size_px,
                pixels_to_mm=request.pixels_to_mm,
                limits=request.fit_limits,
            )
            fitted.payload["optical_session"] = dict(snapshot)
            artifact = LensCalibrationArtifact(
                fitted.payload,
                fitted.before_preview,
                fitted.after_preview,
            )
            success = True
            message = lens_success_message(artifact.payload, request.fit_limits)
        except Exception as exc:
            message = f"Lens distortion calibration failed: {exc}"
        finally:
            success, message, restore_warning = self._cleanup(
                kind="lens",
                request=request,
                child=child,
                reserved=reserved,
                moved=moved,
                start_xy=start_xy,
                camera_key=camera_key,
                success=success,
                message=message,
            )
        return OpticalCalibrationOutcome(
            request.run_id,
            request.wizard_run_id,
            "lens",
            success,
            message,
            request.objective_name,
            request.full_wizard,
            lens_artifact=artifact,
            restore_warning=restore_warning,
        )

    def _capture_flat_grid(
        self,
        request: FlatFieldCalibrationRequest,
        start_xy: tuple[float, float],
        offsets: Sequence[tuple[float, float]],
    ) -> tuple[list[QImage], bool]:
        frames: list[QImage] = []
        expected_size: tuple[int, int] | None = None
        for index, (dx_mm, dy_mm) in enumerate(offsets, start=1):
            self._check_cancelled("Flat-field")
            self._progress(request, f"Flat-field calibration: capture {index}/{len(offsets)}.")
            self._stage.move_xy(
                start_xy[0] + dx_mm,
                start_xy[1] + dy_mm,
                request.linear_feedrate,
            )
            self._settle(request.settle_s, "Flat-field")
            frame = self._require_fresh_frame(request.camera_timeout_s)
            current_size = frame_size(frame)
            expected_size = current_size if expected_size is None else expected_size
            if current_size != expected_size:
                raise RuntimeError("Camera frame size changed during calibration.")
            frames.append(frame)
        return frames, True

    def _capture_lens_grid(
        self,
        request: LensDistortionCalibrationRequest,
        start_xy: tuple[float, float],
        offsets: Sequence[tuple[float, float]],
        expected_size: tuple[int, int],
    ) -> tuple[list[GridCalibrationFrame], bool]:
        frames: list[GridCalibrationFrame] = []
        for index, (dx_mm, dy_mm) in enumerate(offsets, start=1):
            self._check_cancelled("Lens distortion")
            self._progress(
                request,
                f"Lens distortion calibration: capture {index}/{len(offsets)}.",
            )
            self._stage.move_xy(
                start_xy[0] + dx_mm,
                start_xy[1] + dy_mm,
                request.linear_feedrate,
            )
            self._settle(request.settle_s, "Lens distortion")
            frame = self._require_fresh_frame(request.camera_timeout_s)
            if frame_size(frame) != expected_size:
                raise RuntimeError("Camera frame size changed during calibration.")
            frames.append(GridCalibrationFrame(frame, (dx_mm, dy_mm)))
        return frames, True

    def _cleanup(
        self,
        *,
        kind: str,
        request: Request,
        child: OpticalSessionLeasePort | None,
        reserved: bool,
        moved: bool,
        start_xy: tuple[float, float] | None,
        camera_key: str | None,
        success: bool,
        message: str,
    ) -> tuple[bool, str, str]:
        success, message, warnings = self._restore_reserved_resources(
            kind=kind,
            request=request,
            reserved=reserved,
            moved=moved,
            start_xy=start_xy,
            camera_key=camera_key,
            success=success,
            message=message,
        )
        if child is not None:
            warnings.extend(self._lifecycle.close_child(child))
        if self._lifecycle.should_close_parent(kind, request, success):
            parent_warning = self._lifecycle.close_parent_session()
            if parent_warning:
                warnings.append(parent_warning)
        return self._apply_restore_warnings(kind, success, message, warnings)

    def _restore_reserved_resources(
        self,
        *,
        kind: str,
        request: Request,
        reserved: bool,
        moved: bool,
        start_xy: tuple[float, float] | None,
        camera_key: str | None,
        success: bool,
        message: str,
    ) -> tuple[bool, str, list[str]]:
        warnings: list[str] = []
        if reserved and moved and start_xy is not None:
            try:
                self._return_to_start(request, start_xy)
            except Exception as exc:
                success = False
                message = f"{self._label(kind)} calibration restore failed: {exc}"
        if reserved and camera_key is not None:
            camera_warning = self._camera.restore(camera_key)
            if camera_warning:
                warnings.append(str(camera_warning))
        if reserved:
            self._stage.release()
        return success, message, warnings

    def _apply_restore_warnings(
        self,
        kind: str,
        success: bool,
        message: str,
        warnings: Sequence[str],
    ) -> tuple[bool, str, str]:
        warning = "; ".join(item for item in warnings if item)
        if not warning:
            return success, message, ""
        if success:
            return False, self._restore_failure_message(kind, warning), warning
        return (
            False,
            f"{message} Exposure policy restore failed: {warning}",
            warning,
        )

    def _finish_run(
        self,
        request: Request,
        outcome: OpticalCalibrationOutcome,
    ) -> None:
        if self._lifecycle.finish(request):
            self._events.complete(outcome)

    def _progress(self, request: Request, message: str) -> None:
        if self._lifecycle.is_current(request):
            self._events.progress(
                OpticalCalibrationProgress(
                    request.run_id,
                    request.wizard_run_id,
                    "flat" if isinstance(request, FlatFieldCalibrationRequest) else "lens",
                    message,
                )
            )

    def _return_to_start(self, request: Request, start_xy: tuple[float, float]) -> None:
        self._progress(request, f"{self._label_for_request(request)} calibration: returning to start.")
        self._stage.move_xy(start_xy[0], start_xy[1], request.linear_feedrate)

    def _require_fresh_frame(self, timeout_s: float) -> QImage:
        counter = self._camera.latest_raw_counter()
        return self._require_frame(counter, timeout_s)

    def _require_frame(self, after_counter: int | None, timeout_s: float) -> QImage:
        frame = self._camera.wait_raw(after_counter=after_counter, timeout_s=timeout_s)
        if frame is None:
            raise RuntimeError("Camera frame timeout.")
        return frame

    def _settle(self, seconds: float, label: str) -> None:
        if self._lifecycle.cancel_event.wait(max(0.0, float(seconds))):
            raise RuntimeError(f"{label} calibration stopped by user.")

    def _check_cancelled(self, label: str) -> None:
        if self._lifecycle.cancel_event.is_set():
            raise RuntimeError(f"{label} calibration stopped by user.")

    @staticmethod
    def _flat_metadata(
        request: FlatFieldCalibrationRequest,
        start_xy: tuple[float, float],
        offsets: Sequence[tuple[float, float]],
        session_snapshot: dict[str, object],
    ) -> dict[str, object]:
        return {
            "calibrated_at": utc_timestamp(),
            "magnification": request.magnification,
            "capture_grid": [request.grid_size, request.grid_size],
            "overlap_fraction": request.overlap_fraction,
            "start_stage_xy_mm": list(start_xy),
            "capture_offsets_mm": [list(item) for item in offsets],
            "optical_session": dict(session_snapshot),
        }

    @staticmethod
    def _label(kind: str) -> str:
        return "Flat-field" if kind == "flat" else "Lens distortion"

    @staticmethod
    def _label_for_request(request: Request) -> str:
        return (
            "Flat-field"
            if isinstance(request, FlatFieldCalibrationRequest)
            else "Lens distortion"
        )

    @staticmethod
    def _restore_failure_message(kind: str, warning: str) -> str:
        if kind == "flat":
            return f"Flat-field calibration saved, but restore failed: {warning}"
        return f"Lens distortion calibration complete, but restore failed: {warning}"

__all__ = [
    "CalibrationStartDecision",
    "FlatFieldCalibrationRequest",
    "LensCalibrationArtifact",
    "LensDistortionCalibrationRequest",
    "OpticalCalibrationOutcome",
    "OpticalCalibrationProgress",
    "OpticalCalibrationRuntime",
    "OpticalCalibrationState",
]
