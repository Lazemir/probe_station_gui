"""Persistent camera exposure policy and monitor orchestration."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .exposure_adjustment import (
    ExposureAdjustmentController,
    FrameRead,
    SettingsRead,
    SettingsWrite,
    SoftwareOnce,
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


PersistPolicy = Callable[[ExposurePolicy], None]
StateChanged = Callable[[dict[str, object]], None]


class ExposurePolicyController:
    """Own exposure policy transitions and software monitoring."""

    MONITOR_INTERVAL_S = 5.0

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
        self._adjustment = ExposureAdjustmentController(
            software_once=software_once,
            settings_read=settings_read,
            settings_write=settings_write,
            frame_read=frame_read,
            error_factory=ExposurePolicyError,
            shutdown_requested=self._shutdown_is_requested,
            warning_changed=self._set_warning,
        )

    def subscribe(self, callback: StateChanged) -> None:
        """Notify a listener after subsequent state changes."""

        with self._state_lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def unsubscribe(self, callback: StateChanged) -> None:
        """Stop notifying a listener after the current emission completes."""

        with self._state_lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

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

    def run_manual_exposure_write(
        self,
        command: Callable[[], Mapping[str, Any]],
    ) -> dict[str, object]:
        """Run one public manual exposure-time write under policy ownership."""

        def run() -> dict[str, object]:
            with self._state_lock:
                if self._policy.auto_enabled:
                    raise ExposurePolicyError(
                        "ExposureTime cannot be changed while automatic exposure is enabled."
                    )
            result = command()
            if not isinstance(result, Mapping):
                raise ExposurePolicyError("Camera settings write returned an invalid result.")
            return dict(result)

        return self._run_exclusive(run)

    def start(self) -> dict[str, object]:
        """Activate the configured policy and start the monitor lazily."""

        self._ensure_commands_allowed()
        with self._state_lock:
            if self._started:
                return self._state_payload_locked()
        return self._run_exclusive(self._activate_and_publish_monitor)

    def _activate_and_publish_monitor(self) -> dict[str, object]:
        result = self._activate_configured_policy()
        with self._state_lock:
            if self._shutdown_requested:
                raise ExposurePolicyError("Camera exposure policy is shutting down.")
            self._started = True
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._monitor_loop,
                name="camera-exposure-monitor",
                daemon=True,
            )
            self._monitor_thread = thread
        try:
            thread.start()
        except Exception:
            self._stop_event.set()
            with self._state_lock:
                if self._monitor_thread is thread:
                    self._monitor_thread = None
                    self._started = False
            raise
        self._wake_event.set()
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
            if time.monotonic() > deadline:
                raise ExposurePolicyError(
                    "Camera exposure shutdown did not stop active camera work."
                )
            self._adjustment.native_off()
            if time.monotonic() > deadline:
                raise ExposurePolicyError(
                    "Camera exposure shutdown did not stop active camera work."
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
            self._adjustment.clear_software_retry()
            self._adjustment.native_off()
            self._set_monitoring(False)
            return self.snapshot()
        if self._policy.engine is ExposureEngine.SOFTWARE:
            result = self._adjustment.start_software_auto()
            self._set_monitoring(True)
            return result
        result = self._adjustment.run_once(ExposureEngine.CAMERA.value)
        self._adjustment.clear_software_retry()
        self._adjustment.enable_native_continuous_if_allowed()
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

        camera_snapshot = self._adjustment.camera_snapshot()
        self._set_monitoring(False)
        try:
            if (
                next_policy.auto_enabled
                and next_policy.engine is ExposureEngine.SOFTWARE
            ):
                self._adjustment.start_software_auto()
            else:
                self._adjustment.native_off()
                if next_policy.auto_enabled:
                    self._adjustment.run_once(ExposureEngine.CAMERA.value)
                    self._adjustment.enable_native_continuous_if_allowed()
            self._persist_policy_change(next_policy, old_policy)
        except Exception:
            try:
                self._adjustment.restore_camera_snapshot(camera_snapshot)
            finally:
                self._restore_monitoring_for(old_policy)
            raise

        with self._state_lock:
            self._policy = next_policy
        if not (
            next_policy.auto_enabled
            and next_policy.engine is ExposureEngine.SOFTWARE
        ):
            self._adjustment.clear_software_retry()
        self._restore_monitoring_for(next_policy)
        return self.snapshot()

    def _run_selected_engine_once(self) -> dict[str, object]:
        camera_snapshot = self._adjustment.camera_snapshot()
        self._set_monitoring(False)
        try:
            self._adjustment.native_off()
            result = self._adjustment.run_once(self._policy.engine.value)
            self._adjustment.native_off()
            if (
                self._policy.auto_enabled
                and self._policy.engine is ExposureEngine.CAMERA
            ):
                self._adjustment.enable_native_continuous_if_allowed()
        except Exception:
            try:
                self._adjustment.restore_camera_snapshot(camera_snapshot)
            finally:
                self._restore_monitoring_for(self._policy)
            raise
        self._restore_monitoring_for(self._policy)
        return {**result, "engine": self._policy.engine.value}

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
                self._run_exclusive(self._adjustment.monitor_check)
            except ExposurePolicyBusyError:
                continue
            except Exception:
                continue

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


def _error_text(error: BaseException) -> str:
    return str(error) or type(error).__name__


__all__ = [
    "ExposureEngine",
    "ExposurePolicy",
    "ExposurePolicyBusyError",
    "ExposurePolicyController",
    "ExposurePolicyError",
]
