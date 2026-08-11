"""Exclusive, nestable fixed-exposure optical sessions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .exposure_policy import (
    ExposureEngine,
    ExposurePolicy,
    ExposurePolicyBusyError,
    ExposurePolicyController,
    ExposurePolicyError,
    _error_text,
)


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
        self._session_snapshot: dict[str, object] | None = None

    def open(
        self,
        operation: str,
        parent_token: str | None = None,
    ) -> OpticalSessionLease:
        name = str(operation or "").strip()
        if not name:
            raise ExposurePolicyError("Optical session operation is required.")
        controller = self._controller
        controller._command_lock.acquire()
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
            adjustment_controller = controller._adjustment
            camera_snapshot = adjustment_controller.camera_snapshot()
            controller._set_monitoring(False)
            try:
                adjustment_controller.native_off()
                adjustment = adjustment_controller.run_once(policy.engine.value)
                adjustment_controller.native_off()
                nodes, _response = adjustment_controller._read_nodes(("ExposureTime",))
                fixed_exposure = float(nodes["ExposureTime"].get("value"))
            except Exception:
                try:
                    adjustment_controller.restore_camera_snapshot(camera_snapshot)
                finally:
                    controller._restore_monitoring_for(policy)
                raise

            token = uuid.uuid4().hex
            self._outer_token = token
            self._saved_policy = policy
            self._camera_snapshot = camera_snapshot
            adjustment_snapshot = {
                key: adjustment[key]
                for key in (
                    "accepted",
                    "converged",
                    "engine",
                    "final_exposure_us",
                    "final_frame_counter",
                )
                if key in adjustment
            }
            adjustment_snapshot["final_exposure_us"] = fixed_exposure
            self._session_snapshot = {
                "outer_operation": name,
                "policy": policy.to_dict(),
                "fixed_exposure_us": fixed_exposure,
                "adjustment": adjustment_snapshot,
            }
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
        controller._command_lock.acquire()
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
                        controller._adjustment.native_off()
                        controller._restore_monitoring_for(policy)
                    else:
                        if not (
                            controller._adjustment.enable_native_continuous_if_allowed()
                        ):
                            controller._adjustment.native_off()
                else:
                    warning = self._finalize_manual_session(policy)
            finally:
                self._records.pop(token, None)
                self._outer_token = None
                self._saved_policy = None
                self._camera_snapshot = None
                self._session_snapshot = None
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

    def is_active(self, token: str) -> bool:
        controller = self._controller
        with controller._command_lock:
            return token in self._records

    def snapshot(self, token: str) -> dict[str, object]:
        controller = self._controller
        with controller._state_lock:
            record = self._records.get(token)
            session_snapshot = self._session_snapshot
            if record is None or session_snapshot is None:
                raise ExposurePolicyError("Optical session token is not active.")
            return {
                "operation": record.operation,
                **session_snapshot,
            }

    def _finalize_manual_session(self, policy: ExposurePolicy) -> str:
        controller = self._controller
        errors: list[str] = []
        fixed_exposure: object | None = None
        try:
            nodes, _response = controller._adjustment._read_nodes(("ExposureTime",))
            fixed_exposure = nodes["ExposureTime"].get("value")
            if fixed_exposure is None:
                errors.append("Fixed exposure snapshot is unavailable.")
        except Exception as exc:
            errors.append(f"Fixed exposure snapshot failed: {_error_text(exc)}")

        try:
            controller._adjustment.run_once(policy.engine.value)
        except Exception as exc:
            errors.append(f"Final exposure Once failed: {_error_text(exc)}")

        try:
            controller._adjustment.native_off()
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
                controller._adjustment._write_settings(settings)
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

    def snapshot(self) -> dict[str, object]:
        return self._manager.snapshot(self.token)

    def is_active(self) -> bool:
        return self._manager.is_active(self.token)

    def __enter__(self) -> OpticalSessionLease:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


__all__ = [
    "OpticalSessionLease",
    "OpticalSessionManager",
]
