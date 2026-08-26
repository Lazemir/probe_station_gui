from __future__ import annotations

import importlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from probe_station_gui.camera import microscope_scan
from probe_station_gui.camera.distortion import (
    DistortionCorrection,
    apply_distortion_correction,
    correction_from_payload,
)
from probe_station_gui.camera.imaging import objective_scale_calibration
from probe_station_gui.camera.live_correction import (
    LiveCameraCorrectionRequest,
    LiveCameraCorrectionResult,
)
from probe_station_gui.coordinates.coordinator_model import DesignCoordinateLease
from probe_station_gui.views.main_window_auxiliary import create_design_layout_window

logger = logging.getLogger("main")


@dataclass(frozen=True)
class _MicroscopeScanLaunchSnapshot:
    objective_name: str
    magnification: float | None
    scan_name: str
    document_identity: str
    scale: object
    registration_matrix: tuple[tuple[float, float], tuple[float, float]] | None
    registration_offset: tuple[float, float] | None
    objective_xy_offset: tuple[float, float]

    def design_to_raw_stage(
        self,
        design_xy: tuple[float, float],
    ) -> tuple[float, float]:
        if self.registration_matrix is None or self.registration_offset is None:
            raise RuntimeError("Design registration is unavailable.")
        x_value = float(design_xy[0])
        y_value = float(design_xy[1])
        camera_x = (
            self.registration_matrix[0][0] * x_value
            + self.registration_matrix[0][1] * y_value
            + self.registration_offset[0]
        )
        camera_y = (
            self.registration_matrix[1][0] * x_value
            + self.registration_matrix[1][1] * y_value
            + self.registration_offset[1]
        )
        return (
            camera_x + self.objective_xy_offset[0],
            camera_y + self.objective_xy_offset[1],
        )

    def raw_stage_to_design(
        self,
        raw_stage_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        if self.registration_matrix is None or self.registration_offset is None:
            return None
        camera_x = float(raw_stage_xy[0]) - self.objective_xy_offset[0]
        camera_y = float(raw_stage_xy[1]) - self.objective_xy_offset[1]
        shifted_x = camera_x - self.registration_offset[0]
        shifted_y = camera_y - self.registration_offset[1]
        (a, b), (c, d) = self.registration_matrix
        determinant = a * d - b * c
        if abs(determinant) < 1e-18:
            raise RuntimeError("Design registration is singular.")
        return (
            (d * shifted_x - b * shifted_y) / determinant,
            (-c * shifted_x + a * shifted_y) / determinant,
        )

    def metadata(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "objective_name": self.objective_name,
            "magnification": self.magnification,
            "scan_name": self.scan_name,
            "document_identity": self.document_identity,
            "objective_xy_offset_mm": list(self.objective_xy_offset),
        }
        if self.registration_matrix is not None:
            payload["registration_matrix"] = [
                list(row) for row in self.registration_matrix
            ]
        if self.registration_offset is not None:
            payload["registration_offset"] = list(self.registration_offset)
        return payload


class _MainCameraPipelineMixin:
    def _preload_design_layout_window(self) -> None:
        if (
            self.design_layout_window is not None
            or self._design_layout_window_class is not None
            or self._design_layout_preload_started
        ):
            return
        self._design_layout_preload_started = True

        def load_design_window_module() -> None:
            try:
                from probe_station_gui.views.design_layout_window import (
                    DesignLayoutWindow as design_layout_window_class,
                )
            except Exception as exc:
                self.design_layout_module_ready.emit(None, exc)
                return
            self.design_layout_module_ready.emit(design_layout_window_class, None)

        threading.Thread(
            target=load_design_window_module,
            name="DesignLayoutImport",
            daemon=True,
        ).start()

    def _preload_lazy_dialog_modules(self) -> None:
        """Warm non-critical dialogs after the main window is already visible."""

        modules = (
            "probe_station_gui.dialogs.settings_dialog",
            "probe_station_gui.dialogs.route_measurement_dialog",
            "probe_station_gui.dialogs.microscope_scan_dialog",
        )

        def preload() -> None:
            for module_name in modules:
                try:
                    importlib.import_module(module_name)
                except Exception:
                    logger.debug(
                        "Lazy dialog preload failed: %s",
                        module_name,
                        exc_info=True,
                    )

        threading.Thread(
            target=preload,
            name="LazyDialogPreload",
            daemon=True,
        ).start()

    def _on_design_layout_module_ready(
        self,
        design_layout_window_class: object,
        error: object,
    ) -> None:
        if error is not None:
            self._design_layout_preload_started = False
            logger.error("Design window preload failed: %s", error)
            self._show_status(f"Unable to prepare design window: {error}")
            return
        self._design_layout_preload_started = False
        self._design_layout_window_class = design_layout_window_class
        if self._design_layout_window_requested:
            create_design_layout_window(self, design_layout_window_class)

    def _stage_serial_ready(self) -> bool:
        return bool(
            self.serial_connection is not None
            and getattr(self.serial_connection, "is_open", False)
        )

    def on_error(self, message: str) -> None:
        logger.error("Camera error: %s", message)
        self._telegram_runtime.send_alert(
            "camera_error",
            f"Probe station camera error:\n{message}",
            attach_photo=True,
        )

    def _on_camera_frame(self, qimg: QImage) -> None:
        with self._latest_camera_frame_condition:
            self._latest_raw_camera_frame = qimg.copy()
            self._latest_raw_camera_frame_counter = (
                int(getattr(self, "_latest_raw_camera_frame_counter", 0)) + 1
            )
            sequence = int(self._latest_raw_camera_frame_counter)
            self._latest_camera_frame_condition.notify_all()
        exposure_adapter = getattr(self, "_exposure_policy_adapter", None)
        now = time.monotonic()
        if (
            not qimg.isNull()
            and exposure_adapter is not None
            and not getattr(self, "_exposure_policy_start_in_flight", False)
            and not getattr(self, "_exposure_policy_started", False)
            and now >= getattr(self, "_exposure_policy_start_retry_after", 0.0)
        ):
            self._exposure_policy_start_in_flight = True
            exposure_adapter.request_start()
        objective = self.settings_manager.active_objective_configuration()
        request = LiveCameraCorrectionRequest(
            sequence=sequence,
            frame=qimg.copy(),
            objective_name=str(getattr(objective, "name", "") or ""),
            distortion_configured=bool(
                getattr(objective, "distortion_correction_configured", False)
            ),
            distortion_payload=getattr(objective, "distortion_correction", {}),
            suppress_gap_warning=bool(
                getattr(self, "_suppress_next_camera_ui_gap", False)
            ),
        )
        if not self._live_camera_frame_processor.submit(request):
            logger.debug("Live camera frame ignored during processor shutdown.")

    def _on_live_camera_frame_processed(
        self,
        result: LiveCameraCorrectionResult,
    ) -> None:
        now = time.monotonic()
        suppress_gap_warning = bool(result.suppress_gap_warning)
        if self._last_camera_frame_ui_timestamp is not None:
            frame_gap = now - self._last_camera_frame_ui_timestamp
            if (
                frame_gap > self.CAMERA_UI_FRAME_GAP_WARNING_S
                and not suppress_gap_warning
            ):
                logger.warning(
                    "Camera UI frame gap %.3fs before display update",
                    frame_gap,
                )
        self._last_camera_frame_ui_timestamp = now
        frame = result.frame
        with self._latest_camera_frame_condition:
            if int(result.sequence) <= int(self._latest_camera_frame_counter):
                return
            self._latest_camera_frame = frame.copy()
            self._latest_camera_frame_counter = int(result.sequence)
            self._latest_camera_frame_condition.notify_all()
        if suppress_gap_warning:
            self._suppress_next_camera_ui_gap = False
        self._latest_camera_frame_for_notifications = frame
        self.stage_controller.on_frame_ready(frame)
        self.view.set_frame(frame)

    def _on_live_camera_frame_processing_error(self, message: str) -> None:
        logger.warning("Live camera frame correction failed: %s", message)

    def _on_camera_frame_gap_suppressed(self) -> None:
        self._suppress_next_camera_ui_gap = True

    def _correct_camera_frame_for_active_objective(self, qimg: QImage) -> QImage:
        objective = self.settings_manager.active_objective_configuration()
        if not bool(getattr(objective, "distortion_correction_configured", False)):
            self._clear_distortion_correction_cache()
            return qimg
        payload = getattr(objective, "distortion_correction", {})
        try:
            correction = self._distortion_correction_for_objective(objective, payload)
            return apply_distortion_correction(qimg, correction)
        except ValueError as exc:
            logger.warning("Unable to apply lens distortion correction: %s", exc)
            self._clear_distortion_correction_cache()
            return qimg

    def _distortion_correction_for_objective(
        self,
        objective: object,
        payload: object,
    ) -> DistortionCorrection:
        signature = (
            str(getattr(objective, "name", "")),
            self._distortion_payload_signature(payload),
        )
        cached_signature = getattr(
            self,
            "_distortion_correction_cache_signature",
            None,
        )
        cached_model = getattr(self, "_distortion_correction_cache_model", None)
        if cached_signature == signature and cached_model is not None:
            return cached_model
        correction = correction_from_payload(payload)
        self._distortion_correction_cache_signature = signature
        self._distortion_correction_cache_model = correction
        return correction

    @staticmethod
    def _distortion_payload_signature(payload: object) -> str:
        try:
            return json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            return repr(payload)

    def _clear_distortion_correction_cache(self) -> None:
        self._distortion_correction_cache_signature = None
        self._distortion_correction_cache_model = None

    def _latest_camera_counter(self) -> int:
        with self._latest_camera_frame_condition:
            return int(self._latest_camera_frame_counter)

    def _latest_raw_camera_counter(self) -> int:
        direct_counter = getattr(
            getattr(self, "grabber", None),
            "latest_frame_counter",
            None,
        )
        if callable(direct_counter):
            return int(direct_counter())
        with self._latest_camera_frame_condition:
            return int(getattr(self, "_latest_raw_camera_frame_counter", 0))

    def _wait_for_camera_frame(
        self,
        *,
        after_counter: int | None = None,
        timeout_s: float = 2.0,
    ) -> tuple[QImage | None, int]:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._latest_camera_frame_condition:
            while True:
                frame = self._latest_camera_frame
                counter = int(self._latest_camera_frame_counter)
                fresh_enough = after_counter is None or counter > int(after_counter)
                if frame is not None and fresh_enough:
                    return frame.copy(), counter
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    if frame is not None and after_counter is None:
                        return frame.copy(), counter
                    return None, counter
                self._latest_camera_frame_condition.wait(min(remaining, 0.1))

    def _wait_for_raw_camera_frame(
        self,
        *,
        after_counter: int | None = None,
        timeout_s: float = 2.0,
    ) -> tuple[QImage | None, int]:
        direct_wait = getattr(
            getattr(self, "grabber", None),
            "wait_for_frame",
            None,
        )
        if callable(direct_wait):
            return direct_wait(
                after_counter=after_counter,
                timeout_s=timeout_s,
            )
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._latest_camera_frame_condition:
            while True:
                frame = getattr(self, "_latest_raw_camera_frame", None)
                counter = int(getattr(self, "_latest_raw_camera_frame_counter", 0))
                fresh_enough = after_counter is None or counter > int(after_counter)
                if frame is not None and fresh_enough:
                    return frame.copy(), counter
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    if frame is not None and after_counter is None:
                        return frame.copy(), counter
                    return None, counter
                self._latest_camera_frame_condition.wait(min(remaining, 0.1))

    def _active_microscope_scale(self):
        objective = self.settings_manager.active_objective_configuration()
        return objective_scale_calibration(objective)

    def _active_objective_metadata(self) -> tuple[str, float | None]:
        objective = self.settings_manager.active_objective_configuration()
        name = str(getattr(objective, "name", "") or "")
        try:
            magnification = float(getattr(objective, "magnification"))
        except (TypeError, ValueError):
            magnification = None
        if magnification is not None and not math.isfinite(magnification):
            magnification = None
        return name, magnification

    def _capture_microscope_scan_launch_snapshot(
        self,
        *,
        scale: object,
        document: object | None,
        frame_usability_snapshot: DesignCoordinateLease | None = None,
        registration: object | None = None,
    ) -> _MicroscopeScanLaunchSnapshot:
        objective_name, magnification = self._active_objective_metadata()
        objective_offset = tuple(
            float(value) for value in self._active_objective_xy_offset()
        )
        if len(objective_offset) != 2 or not all(
            math.isfinite(value) for value in objective_offset
        ):
            raise ValueError("Active objective offset is invalid.")

        registration_matrix = None
        registration_offset = None
        if frame_usability_snapshot is not None:
            if not frame_usability_snapshot.usable:
                raise ValueError(
                    frame_usability_snapshot.rejection_reason
                    or "Design coordinate frame is unavailable."
                )
            basis = tuple(
                self._coordinate_system_coordinator.project_design_to_camera_stage(
                    frame_usability_snapshot,
                    point,
                )
                for point in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
            )
            if any(point is None for point in basis):
                raise ValueError("Design coordinate frame is unavailable.")
            origin, x_basis, y_basis = basis
            assert origin is not None and x_basis is not None and y_basis is not None
            registration_matrix = (
                (x_basis[0] - origin[0], y_basis[0] - origin[0]),
                (x_basis[1] - origin[1], y_basis[1] - origin[1]),
            )
            registration_offset = (float(origin[0]), float(origin[1]))
        elif registration is not None and bool(getattr(registration, "valid", False)):
            matrix = getattr(registration, "matrix")
            offset_value = getattr(registration, "offset")
            registration_matrix = (
                (float(matrix[0][0]), float(matrix[0][1])),
                (float(matrix[1][0]), float(matrix[1][1])),
            )
            registration_offset = (
                float(offset_value[0]),
                float(offset_value[1]),
            )

        if registration_matrix is not None and registration_offset is not None:
            transform_values = (
                *registration_matrix[0],
                *registration_matrix[1],
                *registration_offset,
            )
            determinant = (
                registration_matrix[0][0] * registration_matrix[1][1]
                - registration_matrix[0][1] * registration_matrix[1][0]
            )
            if not all(math.isfinite(value) for value in transform_values):
                raise ValueError("Design registration is invalid.")
            if abs(determinant) < 1e-18:
                raise ValueError("Design registration is singular.")

        document_path = (
            getattr(document, "path", None) if document is not None else None
        )
        document_identity = str(document_path or "")
        scan_name = (
            Path(document_identity).stem
            if document_identity
            else microscope_scan.scan_name_from_document(None)
        )
        return _MicroscopeScanLaunchSnapshot(
            objective_name=str(objective_name),
            magnification=magnification,
            scan_name=scan_name,
            document_identity=document_identity,
            scale=scale,
            registration_matrix=registration_matrix,
            registration_offset=registration_offset,
            objective_xy_offset=(objective_offset[0], objective_offset[1]),
        )

    def _stage_position_for_image_metadata(
        self,
        *,
        stage_xy: tuple[float, float] | None = None,
    ) -> tuple[float, ...] | None:
        latest = self.stage_controller.latest_stage_position()
        if stage_xy is not None:
            x_mm = float(stage_xy[0])
            y_mm = float(stage_xy[1])
            if latest is not None:
                values = list(latest)
                if len(values) >= 2:
                    values[0] = x_mm
                    values[1] = y_mm
                    return tuple(float(value) for value in values)
            return (x_mm, y_mm)
        if latest is not None:
            return latest
        return None

    def _show_status(self, message: str, timeout_ms: int = 0) -> None:
        if message:
            status_text = str(message)
            try:
                timeout = int(timeout_ms)
            except (TypeError, ValueError):
                timeout = 0
            app = QApplication.instance()
            if app is not None and QThread.currentThread() != app.thread():
                self.status_message_requested.emit(status_text, timeout)
                return
            self._latest_status_message = status_text
            self.statusBar().showMessage(status_text, timeout)
            self._status_log.appendPlainText(status_text)
            self._append_status_log(status_text)
