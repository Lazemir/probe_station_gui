"""Persistent camera exposure policy and fixed-exposure sessions."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .auto_exposure import (
    AutoExposureConfig,
    AutoExposureFrame,
    highlight_level,
)


class ExposureEngine(str, Enum):
    """Available implementations for automatic exposure."""

    SOFTWARE = "software"
    CAMERA = "camera"


@dataclass(frozen=True)
class ExposurePolicy:
    """Persisted operator exposure choices."""

    auto_enabled: bool = True
    engine: ExposureEngine | str = ExposureEngine.SOFTWARE

    def __post_init__(self) -> None:
        object.__setattr__(self, "auto_enabled", bool(self.auto_enabled))
        try:
            selected = ExposureEngine(self.engine)
        except ValueError as exc:
            raise ExposurePolicyError(
                f"Unsupported exposure engine: {self.engine!r}."
            ) from exc
        object.__setattr__(self, "engine", selected)

    def clone(self) -> ExposurePolicy:
        return ExposurePolicy(self.auto_enabled, self.engine)

    def to_dict(self) -> dict[str, bool | str]:
        return {
            "auto_enabled": self.auto_enabled,
            "engine": self.engine.value,
        }


class ExposurePolicyError(RuntimeError):
    """Raised when an exposure policy operation cannot be completed."""


class ExposurePolicyBusyError(ExposurePolicyError):
    """Raised when an exposure operation is already active."""


SoftwareOnce = Callable[[AutoExposureConfig | None], Mapping[str, Any]]
SettingsRead = Callable[[list[str]], Mapping[str, Any]]
SettingsWrite = Callable[[list[tuple[str, object]]], Mapping[str, Any]]
FrameRead = Callable[[int, float], AutoExposureFrame]
PersistPolicy = Callable[[ExposurePolicy], None]
StateChanged = Callable[[dict[str, object]], None]


class ExposurePolicyController:
    """Own exposure policy transitions and software monitoring."""

    MONITOR_INTERVAL_S = 5.0
    DRIFT_TOLERANCE_FRACTION = 0.05
    HARDWARE_ONCE_TIMEOUT_S = 5.0
    FRAME_TIMEOUT_S = 2.5
    HARDWARE_POLL_INTERVAL_S = 0.01

    def __init__(
        self,
        *,
        initial_policy: object,
        software_once: SoftwareOnce,
        settings_read: SettingsRead,
        settings_write: SettingsWrite,
        frame_read: FrameRead,
        persist: PersistPolicy | None = None,
        state_changed: StateChanged | None = None,
    ) -> None:
        self._policy = ExposurePolicy(
            auto_enabled=bool(getattr(initial_policy, "auto_enabled", True)),
            engine=getattr(initial_policy, "engine", ExposureEngine.SOFTWARE),
        )
        self._software_once = software_once
        self._settings_read = settings_read
        self._settings_write = settings_write
        self._frame_read = frame_read
        self._persist = persist
        self._command_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._callbacks: list[StateChanged] = []
        if state_changed is not None:
            self._callbacks.append(state_changed)
        self._started = False
        self._monitoring = False
        self._busy = False
        self._session_active = False
        self._session_operation = ""
        self._last_error = ""
        self._warning = ""
        self._shutdown_requested = False
        self._shutdown_complete = False

    def subscribe(self, callback: StateChanged) -> None:
        """Notify a listener after subsequent state changes."""

        with self._state_lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return self._state_payload_locked()

    def set_policy(self, *, auto_enabled: bool, engine: str) -> dict[str, object]:
        self._ensure_commands_allowed()
        try:
            selected = ExposureEngine(engine)
        except ValueError as exc:
            raise ExposurePolicyError(
                f"Unsupported exposure engine: {engine!r}."
            ) from exc
        return self._run_exclusive(
            lambda: self._transition_policy(bool(auto_enabled), selected)
        )

    def run_once(self) -> dict[str, object]:
        self._ensure_commands_allowed()
        return self._run_exclusive(self._run_selected_engine_once)

    def start(self) -> dict[str, object]:
        """Activate the configured policy and start the monitor lazily."""

        self._ensure_commands_allowed()
        with self._state_lock:
            if self._started:
                return self._state_payload_locked()
        result = self._run_exclusive(self._activate_configured_policy)
        with self._state_lock:
            if not self._started:
                self._started = True
                self._stop_event.clear()
                thread = threading.Thread(
                    target=self._monitor_loop,
                    name="camera-exposure-monitor",
                    daemon=True,
                )
                self._monitor_thread = thread
                thread.start()
        self._wake_event.set()
        self._emit_state()
        return result

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """Stop controller and native exposure work within a bounded timeout."""

        timeout = max(0.0, float(timeout_s))
        deadline = time.monotonic() + timeout
        with self._state_lock:
            if self._shutdown_complete:
                return
            self._shutdown_requested = True
            self._monitoring = False
            thread = self._monitor_thread
        self._stop_event.set()
        self._wake_event.set()

        if thread is threading.current_thread():
            self._record_shutdown_failure(
                "Camera exposure shutdown did not stop the monitor thread."
            )
        remaining = max(0.0, deadline - time.monotonic())
        if not self._command_lock.acquire(timeout=remaining):
            self._record_shutdown_failure(
                "Camera exposure shutdown did not stop active camera work."
            )
        error: Exception | None = None
        try:
            with self._state_lock:
                if self._session_active:
                    raise ExposurePolicyError(
                        "Camera exposure shutdown did not stop the optical session."
                    )
                thread = self._monitor_thread
            if thread is not None and thread.is_alive():
                remaining = max(0.0, deadline - time.monotonic())
                thread.join(remaining)
            if thread is not None and thread.is_alive():
                raise ExposurePolicyError(
                    "Camera exposure shutdown did not stop the monitor thread."
                )
            with self._state_lock:
                self._monitor_thread = None
                self._started = False
                self._shutdown_complete = True
                self._last_error = ""
        except Exception as exc:
            error = exc
            with self._state_lock:
                self._last_error = _error_text(exc)
        finally:
            self._command_lock.release()
            self._emit_state()
        if error is not None:
            if isinstance(error, ExposurePolicyError):
                raise error
            raise ExposurePolicyError(
                f"Camera exposure shutdown did not stop camera work: {_error_text(error)}"
            ) from error

    def _record_shutdown_failure(self, message: str) -> None:
        with self._state_lock:
            self._last_error = str(message)
        raise ExposurePolicyError(message)

    def wake_monitor(self) -> None:
        """Restart the monitor interval after a policy operation."""

        self._wake_event.set()

    def _run_exclusive(
        self,
        command: Callable[[], dict[str, object]],
    ) -> dict[str, object]:
        if not self._command_lock.acquire(blocking=False):
            raise ExposurePolicyBusyError("Camera exposure policy is busy.")
        try:
            with self._state_lock:
                if self._shutdown_requested:
                    raise ExposurePolicyError(
                        "Camera exposure policy is shutting down."
                    )
                if self._session_active:
                    raise ExposurePolicyBusyError(
                        "An optical session is using camera exposure."
                    )
                self._busy = True
                self._last_error = ""
                self._warning = ""
            try:
                return command()
            except Exception as exc:
                with self._state_lock:
                    self._last_error = str(exc) or type(exc).__name__
                raise
            finally:
                with self._state_lock:
                    self._busy = False
        finally:
            self._command_lock.release()
            self._emit_state()

    def _activate_configured_policy(self) -> dict[str, object]:
        if not self._policy.auto_enabled:
            self._write_settings([("ExposureAuto", "Off")])
            self._set_monitoring(False)
            return self.snapshot()
        if self._policy.engine is ExposureEngine.SOFTWARE:
            result = self._software_once_result()
            self._write_settings([("ExposureAuto", "Off")])
            self._set_monitoring(True)
            return result
        result = self._hardware_once()
        self._enable_native_continuous_if_allowed()
        self._set_monitoring(False)
        return result

    def _transition_policy(
        self,
        auto_enabled: bool,
        engine: ExposureEngine,
    ) -> dict[str, object]:
        next_policy = ExposurePolicy(auto_enabled, engine)
        old_policy = self._policy
        if next_policy == old_policy:
            return self.snapshot()
        if not old_policy.auto_enabled and not next_policy.auto_enabled:
            self._persist_policy_change(next_policy, old_policy)
            with self._state_lock:
                self._policy = next_policy
            return self.snapshot()

        camera_snapshot = self._camera_snapshot()
        self._set_monitoring(False)
        try:
            self._write_settings([("ExposureAuto", "Off")])
            if next_policy.auto_enabled:
                if next_policy.engine is ExposureEngine.SOFTWARE:
                    self._software_once_result()
                    self._write_settings([("ExposureAuto", "Off")])
                else:
                    self._hardware_once()
                    self._enable_native_continuous_if_allowed()
            self._persist_policy_change(next_policy, old_policy)
        except Exception:
            try:
                self._restore_camera_snapshot(camera_snapshot)
            finally:
                self._restore_monitoring_for(old_policy)
            raise

        with self._state_lock:
            self._policy = next_policy
        self._restore_monitoring_for(next_policy)
        return self.snapshot()

    def _run_selected_engine_once(self) -> dict[str, object]:
        camera_snapshot = self._camera_snapshot()
        self._set_monitoring(False)
        try:
            self._write_settings([("ExposureAuto", "Off")])
            if self._policy.engine is ExposureEngine.SOFTWARE:
                result = self._software_once_result()
            else:
                result = self._hardware_once()
            self._write_settings([("ExposureAuto", "Off")])
            if (
                self._policy.auto_enabled
                and self._policy.engine is ExposureEngine.CAMERA
            ):
                self._enable_native_continuous_if_allowed()
        except Exception:
            try:
                self._restore_camera_snapshot(camera_snapshot)
            finally:
                self._restore_monitoring_for(self._policy)
            raise
        self._restore_monitoring_for(self._policy)
        return {**result, "engine": self._policy.engine.value}

    def _software_once_result(self) -> dict[str, object]:
        config = AutoExposureConfig(target_tolerance_fraction=0.02)
        raw = self._software_once(config)
        result = dict(raw)
        if not _response_accepted(result) or not bool(result.get("converged", True)):
            raise ExposurePolicyError(
                str(result.get("message") or "Software exposure did not converge.")
            )
        result.setdefault("accepted", True)
        result["engine"] = ExposureEngine.SOFTWARE.value
        return result

    def _hardware_once(self) -> dict[str, object]:
        response = self._write_settings([("ExposureAuto", "Once")])
        watermark = _response_counter(response, 0)
        deadline = time.monotonic() + self.HARDWARE_ONCE_TIMEOUT_S
        while True:
            nodes, read_response = self._read_nodes(("ExposureAuto",))
            watermark = max(watermark, _response_counter(read_response, watermark))
            if str(nodes["ExposureAuto"].get("value")) == "Off":
                break
            if time.monotonic() >= deadline:
                raise ExposurePolicyError("Camera exposure Once timed out.")
            time.sleep(self.HARDWARE_POLL_INTERVAL_S)
        frame = self._frame_read(watermark, self.FRAME_TIMEOUT_S)
        if int(frame.counter) <= watermark:
            raise ExposurePolicyError(
                "Camera exposure Once did not produce a fresh raw frame."
            )
        nodes, _response = self._read_nodes(("ExposureTime",))
        try:
            final_exposure = float(nodes["ExposureTime"].get("value"))
        except (TypeError, ValueError) as exc:
            raise ExposurePolicyError("Camera exposure time is unavailable.") from exc
        return {
            "accepted": True,
            "status_code": 200,
            "message": "Camera exposure Once completed.",
            "converged": True,
            "engine": ExposureEngine.CAMERA.value,
            "final_exposure_us": final_exposure,
            "final_frame_counter": int(frame.counter),
        }

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            woke = self._wake_event.wait(self.MONITOR_INTERVAL_S)
            self._wake_event.clear()
            if self._stop_event.is_set():
                return
            if woke:
                continue
            with self._state_lock:
                should_check = self._monitoring and not self._session_active
            if not should_check:
                continue
            try:
                self._run_exclusive(self._monitor_check)
            except ExposurePolicyBusyError:
                continue
            except Exception:
                continue

    def _monitor_check(self) -> dict[str, object]:
        _nodes, response = self._read_nodes(("ExposureTime",))
        watermark = _response_counter(response, 0)
        frame = self._frame_read(watermark, self.FRAME_TIMEOUT_S)
        if int(frame.counter) <= watermark:
            raise ExposurePolicyError(
                "Software exposure monitor received a stale frame."
            )
        config = AutoExposureConfig(target_tolerance_fraction=0.02)
        measured = highlight_level(frame.rgb, config)
        relative_error = abs(measured - config.target_level) / config.target_level
        result: dict[str, object] = {
            "accepted": True,
            "engine": ExposureEngine.SOFTWARE.value,
            "brightness": measured,
            "adjusted": False,
            "final_frame_counter": int(frame.counter),
        }
        if relative_error > self.DRIFT_TOLERANCE_FRACTION:
            result = self._software_once_result()
            result["adjusted"] = True
        return result

    def _camera_snapshot(self) -> dict[str, object]:
        nodes, _response = self._read_nodes(("ExposureAuto", "ExposureTime"))
        return {name: node.get("value") for name, node in nodes.items()}

    def _restore_camera_snapshot(self, snapshot: Mapping[str, object]) -> None:
        errors: list[str] = []
        try:
            self._write_settings([("ExposureAuto", "Off")])
        except Exception as exc:
            errors.append(str(exc))
        if "ExposureTime" in snapshot:
            try:
                self._write_settings([("ExposureTime", snapshot.get("ExposureTime"))])
            except Exception as exc:
                errors.append(str(exc))
        native = snapshot.get("ExposureAuto")
        if native not in (None, "Off") and not self._shutdown_is_requested():
            try:
                self._write_settings([("ExposureAuto", native)])
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise ExposurePolicyError(
                f"Unable to restore camera exposure: {'; '.join(errors)}"
            )

    def _read_nodes(
        self,
        names: Sequence[str],
    ) -> tuple[dict[str, dict[str, Any]], Mapping[str, Any]]:
        response = self._settings_read([str(name) for name in names])
        if not _response_accepted(response):
            raise ExposurePolicyError(
                str(response.get("message") or "Camera settings read failed.")
            )
        candidates: list[object] = list(response.get("nodes") or [])
        for node_map in response.get("maps") or []:
            if isinstance(node_map, Mapping):
                candidates.extend(node_map.get("nodes") or [])
        nodes = {
            str(node.get("name") or ""): dict(node)
            for node in candidates
            if isinstance(node, Mapping)
        }
        missing = [name for name in names if name not in nodes]
        if missing:
            raise ExposurePolicyError(f"Camera setting is unavailable: {missing[0]}.")
        return nodes, response

    def _write_settings(
        self,
        settings: list[tuple[str, object]],
    ) -> Mapping[str, Any]:
        response = self._settings_write(list(settings))
        if not _response_accepted(response):
            raise ExposurePolicyError(
                str(response.get("message") or "Camera settings write failed.")
            )
        return response

    def _persist_policy_change(
        self,
        new_policy: ExposurePolicy,
        old_policy: ExposurePolicy,
    ) -> None:
        if self._persist is None:
            return
        try:
            self._persist(new_policy.clone())
        except Exception as persist_error:
            try:
                self._persist(old_policy.clone())
            except Exception as rollback_error:
                raise ExposurePolicyError(
                    "Policy persistence failed: "
                    f"{_error_text(persist_error)}; persistence rollback failed: "
                    f"{_error_text(rollback_error)}"
                ) from persist_error
            raise

    def _restore_monitoring_for(self, policy: ExposurePolicy) -> None:
        enabled = (
            policy.auto_enabled
            and policy.engine is ExposureEngine.SOFTWARE
            and not self._shutdown_is_requested()
        )
        self._set_monitoring(enabled)
        if enabled:
            self._wake_event.set()

    def _set_monitoring(self, enabled: bool) -> None:
        with self._state_lock:
            self._monitoring = bool(enabled and not self._shutdown_requested)

    def _enable_native_continuous_if_allowed(self) -> bool:
        if self._shutdown_is_requested():
            return False
        self._write_settings([("ExposureAuto", "Continuous")])
        return True

    def _ensure_commands_allowed(self) -> None:
        if self._shutdown_is_requested():
            raise ExposurePolicyError("Camera exposure policy is shutting down.")

    def _shutdown_is_requested(self) -> bool:
        with self._state_lock:
            return self._shutdown_requested

    def _set_session_state(self, active: bool, operation: str = "") -> None:
        with self._state_lock:
            self._session_active = bool(active)
            self._session_operation = str(operation) if active else ""

    def _set_warning(self, warning: str) -> None:
        with self._state_lock:
            self._warning = str(warning)

    def _state_payload_locked(self) -> dict[str, object]:
        return {
            **self._policy.to_dict(),
            "busy": bool(self._busy or self._session_active),
            "monitoring": self._monitoring,
            "session_active": self._session_active,
            "session_operation": self._session_operation,
            "error": self._last_error,
            "warning": self._warning,
            "shutdown_requested": self._shutdown_requested,
            "shutdown_complete": self._shutdown_complete,
        }

    def _emit_state(self) -> None:
        with self._state_lock:
            state = dict(self._state_payload_locked())
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(dict(state))
            except Exception:
                continue


@dataclass
class _SessionRecord:
    token: str
    operation: str
    parent_token: str | None


class OpticalSessionManager:
    """Issue exclusive, nestable fixed-exposure leases."""

    def __init__(self, controller: ExposurePolicyController) -> None:
        self._controller = controller
        self._records: dict[str, _SessionRecord] = {}
        self._outer_token: str | None = None
        self._saved_policy: ExposurePolicy | None = None
        self._camera_snapshot: dict[str, object] | None = None

    def open(
        self,
        operation: str,
        parent_token: str | None = None,
    ) -> OpticalSessionLease:
        name = str(operation or "").strip()
        if not name:
            raise ExposurePolicyError("Optical session operation is required.")
        controller = self._controller
        if not controller._command_lock.acquire(blocking=False):
            raise ExposurePolicyBusyError("Camera exposure policy is busy.")
        state_changed = False
        try:
            controller._ensure_commands_allowed()
            if self._outer_token is not None:
                if parent_token is None:
                    raise ExposurePolicyBusyError(
                        "Another optical session is already active."
                    )
                if parent_token not in self._records:
                    raise ExposurePolicyError("Invalid optical session parent token.")
                token = uuid.uuid4().hex
                self._records[token] = _SessionRecord(token, name, parent_token)
                return OpticalSessionLease(self, token)
            if parent_token is not None:
                raise ExposurePolicyError("Invalid optical session parent token.")

            with controller._state_lock:
                controller._busy = True
                controller._last_error = ""
                controller._warning = ""
                policy = controller._policy.clone()
            camera_snapshot = controller._camera_snapshot()
            controller._set_monitoring(False)
            try:
                controller._write_settings([("ExposureAuto", "Off")])
                if policy.engine is ExposureEngine.SOFTWARE:
                    controller._software_once_result()
                else:
                    controller._hardware_once()
                controller._write_settings([("ExposureAuto", "Off")])
            except Exception:
                try:
                    controller._restore_camera_snapshot(camera_snapshot)
                finally:
                    controller._restore_monitoring_for(policy)
                raise

            token = uuid.uuid4().hex
            self._outer_token = token
            self._saved_policy = policy
            self._camera_snapshot = camera_snapshot
            self._records[token] = _SessionRecord(token, name, None)
            controller._set_session_state(True, name)
            state_changed = True
            return OpticalSessionLease(self, token)
        except Exception as exc:
            with controller._state_lock:
                controller._last_error = str(exc) or type(exc).__name__
            raise
        finally:
            with controller._state_lock:
                controller._busy = False
            controller._command_lock.release()
            if state_changed or self._outer_token is None:
                controller._emit_state()

    def close(self, token: str) -> dict[str, object]:
        controller = self._controller
        if not controller._command_lock.acquire(blocking=False):
            raise ExposurePolicyBusyError("Camera exposure policy is busy.")
        emit = False
        try:
            record = self._records.get(token)
            if record is None:
                raise ExposurePolicyError("Optical session token is not active.")
            children = [
                item for item in self._records.values() if item.parent_token == token
            ]
            if children:
                raise ExposurePolicyError(
                    "Close nested optical sessions before their parent."
                )
            if token != self._outer_token:
                self._records.pop(token, None)
                return {"accepted": True, "nested": True}

            with controller._state_lock:
                controller._busy = True
            policy = self._saved_policy
            if policy is None:
                raise ExposurePolicyError("Optical session policy is unavailable.")
            warning = ""
            try:
                if policy.auto_enabled:
                    if policy.engine is ExposureEngine.SOFTWARE:
                        controller._write_settings([("ExposureAuto", "Off")])
                        controller._restore_monitoring_for(policy)
                    else:
                        if not controller._enable_native_continuous_if_allowed():
                            controller._write_settings([("ExposureAuto", "Off")])
                else:
                    warning = self._finalize_manual_session(policy)
            finally:
                self._records.pop(token, None)
                self._outer_token = None
                self._saved_policy = None
                self._camera_snapshot = None
                controller._set_session_state(False)
                controller._set_warning(warning)
                emit = True
            result: dict[str, object] = {"accepted": True, "nested": False}
            if warning:
                result["warning"] = warning
            return result
        finally:
            with controller._state_lock:
                controller._busy = False
            controller._command_lock.release()
            if emit:
                controller._emit_state()

    def _finalize_manual_session(self, policy: ExposurePolicy) -> str:
        controller = self._controller
        errors: list[str] = []
        fixed_exposure: object | None = None
        try:
            nodes, _response = controller._read_nodes(("ExposureTime",))
            fixed_exposure = nodes["ExposureTime"].get("value")
        except Exception as exc:
            errors.append(f"Fixed exposure snapshot failed: {_error_text(exc)}")

        if fixed_exposure is not None:
            try:
                if policy.engine is ExposureEngine.SOFTWARE:
                    controller._software_once_result()
                else:
                    controller._hardware_once()
            except Exception as exc:
                errors.append(f"Final exposure Once failed: {_error_text(exc)}")

        try:
            controller._write_settings([("ExposureAuto", "Off")])
        except Exception as exc:
            errors.append(f"Final ExposureAuto Off failed: {_error_text(exc)}")

        if errors and fixed_exposure is not None:
            errors.extend(self._restore_fixed_exposure(fixed_exposure))
        return "; ".join(errors)

    def _restore_fixed_exposure(self, fixed_exposure: object) -> list[str]:
        controller = self._controller
        errors: list[str] = []
        restore_steps = (
            ("ExposureAuto Off", [("ExposureAuto", "Off")]),
            ("ExposureTime", [("ExposureTime", fixed_exposure)]),
            ("final ExposureAuto Off", [("ExposureAuto", "Off")]),
        )
        for label, settings in restore_steps:
            try:
                controller._write_settings(settings)
            except Exception as exc:
                errors.append(
                    f"Fixed exposure restore {label} failed: {_error_text(exc)}"
                )
        return errors


class OpticalSessionLease:
    """Context-managed handle for one optical session level."""

    def __init__(self, manager: OpticalSessionManager, token: str) -> None:
        self._manager = manager
        self.token = str(token)
        self._close_result: dict[str, object] | None = None

    def close(self) -> dict[str, object]:
        if self._close_result is None:
            self._close_result = self._manager.close(self.token)
        return dict(self._close_result)

    def __enter__(self) -> OpticalSessionLease:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


def _response_accepted(response: Mapping[str, Any]) -> bool:
    if "accepted" in response:
        return bool(response.get("accepted"))
    return bool(response.get("ok"))


def _response_counter(response: Mapping[str, Any], default: int) -> int:
    try:
        return int(response.get("frame_counter_at_completion", default))
    except (TypeError, ValueError):
        return int(default)


def _error_text(error: BaseException) -> str:
    return str(error) or type(error).__name__


__all__ = [
    "ExposureEngine",
    "ExposurePolicy",
    "ExposurePolicyBusyError",
    "ExposurePolicyController",
    "ExposurePolicyError",
    "OpticalSessionLease",
    "OpticalSessionManager",
]
