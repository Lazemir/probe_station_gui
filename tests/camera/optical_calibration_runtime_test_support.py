from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera.optical_calibration_runtime import (
    FlatFieldCalibrationRequest,
    LensDistortionCalibrationRequest,
    OpticalCalibrationRuntime,
)


class _FailingSession:
    def open(self, operation: str, parent_token: str | None = None):
        raise RuntimeError("Exposure did not converge.")


class _ForbiddenPort:
    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected port call: {name}")


class _Events:
    def __init__(self) -> None:
        self.finished = []
        self.progress_events = []

    def progress(self, event) -> None:
        self.progress_events.append(event)

    def complete(self, outcome) -> None:
        self.finished.append(outcome)

    def warning(self, message: str) -> None:
        self.finished.append(("warning", message))


class _InlineThread:
    errors: list[BaseException] = []

    def __init__(self, *, target, **_kwargs) -> None:
        self._target = target
        self._alive = False

    def start(self) -> None:
        self._alive = True
        try:
            self._target()
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def join(self, _timeout=None) -> None:
        return None


class _DeferredThread:
    def __init__(self, *, target, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return False

    def join(self, _timeout=None) -> None:
        return None


class _Lease:
    def __init__(
        self,
        events: list[object],
        operation: str,
        token: str,
        *,
        close_warning: str = "",
        close_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.operation = operation
        self.token = token
        self.close_warning = close_warning
        self.close_error = close_error
        self.active = True

    def snapshot(self) -> dict[str, object]:
        self.events.append(("session_snapshot", self.operation))
        return {"operation": self.operation, "fixed_exposure_us": 3200.0}

    def close(self) -> dict[str, object]:
        self.events.append(("session_close", self.operation))
        if self.close_error is not None:
            raise self.close_error
        self.active = False
        result: dict[str, object] = {"accepted": True}
        if self.close_warning:
            result["warning"] = self.close_warning
        return result

    def is_active(self) -> bool:
        return self.active


class _Sessions:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.leases: list[_Lease] = []
        self.close_warning_by_operation: dict[str, str] = {}

    def open(self, operation: str, parent_token: str | None = None) -> _Lease:
        token = f"lease-{len(self.leases) + 1}"
        self.events.append(("session_open", operation, parent_token, token))
        lease = _Lease(
            self.events,
            operation,
            token,
            close_warning=self.close_warning_by_operation.get(operation, ""),
        )
        self.leases.append(lease)
        return lease


class _Stage:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.fail_on: str | None = None
        self.release_error: Exception | None = None

    def reserve(self, operation: str) -> None:
        self.events.append(("stage_reserve", operation))
        if self.fail_on == "reserve":
            raise RuntimeError("reserve failed")

    def start_position(self) -> tuple[float, float]:
        self.events.append(("stage_position",))
        if self.fail_on == "position":
            raise RuntimeError("position failed")
        return 10.0, 20.0

    def raise_needles(self, feedrate: float) -> None:
        self.events.append(("needles", feedrate))

    def move_xy(self, x_mm: float, y_mm: float, feedrate: float) -> None:
        self.events.append(("move", x_mm, y_mm, feedrate))
        if self.fail_on == "move":
            raise RuntimeError("move failed")

    def release(self) -> None:
        self.events.append(("stage_release",))
        if self.release_error is not None:
            raise self.release_error


class _Camera:
    def __init__(self, events: list[object], frames: list[QImage]) -> None:
        self.events = events
        self.frames = list(frames)
        self.counter = 0
        self.restore_warning = ""
        self.restore_error: Exception | None = None

    def lock(self) -> str:
        self.events.append(("camera_lock",))
        return "camera-key"

    def restore(self, key: str) -> str:
        self.events.append(("camera_restore", key))
        if self.restore_error is not None:
            raise self.restore_error
        return self.restore_warning

    def latest_raw_counter(self) -> int:
        self.events.append(("camera_counter", self.counter))
        return self.counter

    def wait_raw(self, *, after_counter: int | None, timeout_s: float) -> QImage | None:
        self.events.append(("camera_frame", after_counter, timeout_s))
        if not self.frames:
            return None
        self.counter += 1
        return self.frames.pop(0)


class _Store:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.calls: list[tuple[object, ...]] = []

    def install(self, objective_name, frames, metadata):
        self.events.append(("store", objective_name, len(frames)))
        self.calls.append((objective_name, tuple(frames), metadata))
        return SimpleNamespace(
            current_manifest="current.json", reference_image="flat.png"
        )


def _frame(width: int = 100, height: int = 50) -> QImage:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(100, 110, 120))
    return image


def _valid_lens_payload() -> dict[str, object]:
    return {
        "model_version": 1,
        "model_type": "stage_geometry",
        "frame_size": [100, 50],
        "pixels_to_mm": [[-0.001, 0.0002], [-0.0003, -0.002]],
        "calibrated_pixels_to_mm": [[-0.001, 0.0002], [-0.0003, -0.002]],
        "center_px": [50.0, 25.0],
        "k1": 0.0,
        "k2": 0.0,
        "p1": 0.0,
        "p2": 0.0,
        "baseline_residual_mean_px": 1.5,
        "baseline_residual_max_px": 2.5,
        "residual_mean_px": 0.5,
        "residual_max_px": 1.0,
        "feature_count": 20,
        "observation_count": 40,
        "optimizer_success": True,
    }


def _flat_request(**changes) -> FlatFieldCalibrationRequest:
    request = FlatFieldCalibrationRequest(
        run_id="flat-1",
        wizard_run_id=None,
        objective_name="X20",
        magnification=20.0,
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
        pixel_size_mm=(0.001, 0.002),
        linear_feedrate=120.0,
        needle_feedrate=70.0,
        settle_s=0.0,
    )
    return replace(request, **changes)


def _lens_request(**changes) -> LensDistortionCalibrationRequest:
    request = LensDistortionCalibrationRequest(
        run_id="lens-1",
        wizard_run_id=None,
        objective_name="X20",
        magnification=20.0,
        pixels_to_mm=((-0.001, 0.0002), (0.0003, -0.002)),
        pixel_size_mm=(0.001, 0.002),
        linear_feedrate=120.0,
        needle_feedrate=70.0,
        settle_s=0.0,
    )
    return replace(request, **changes)


def _runtime(events, *, frames, sessions=None, stage=None, store=None, **kwargs):
    event_port = _Events()
    runtime = OpticalCalibrationRuntime(
        stage=stage or _Stage(events),
        camera=_Camera(events, frames),
        sessions=sessions or _Sessions(events),
        store=store or _Store(events),
        events=event_port,
        thread_factory=_InlineThread,
        **kwargs,
    )
    return runtime, event_port
