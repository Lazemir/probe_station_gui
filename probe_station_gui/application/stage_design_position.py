from __future__ import annotations

import logging
import math

from probe_station_gui.application.stage_motion_types import StageMotionPresentation
from probe_station_gui.design import navigation_targeting
from probe_station_gui.stage import position_update as stage_position_update
from probe_station_gui.stage.coordinate_targets import (
    normalize_api_coordinate_input_mode,
    resolve_stage_axis_target,
    stage_axis_target_limit_error,
)
from probe_station_gui.views import (
    main_window_coordinate_flow as coordinate_flow,
)
from probe_station_gui.views import (
    main_window_design_workspace as design_workspace,
)
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)

logger = logging.getLogger("main")


class _MainStageDesignPositionMixin:
    def _apply_stage_motion_presentation(
        self,
        presentation: StageMotionPresentation,
    ) -> None:
        if not isinstance(presentation, StageMotionPresentation):
            raise TypeError("presentation must be a StageMotionPresentation")
        if not presentation.material_change:
            return
        if presentation.active_axes:
            stage_position_panel_adapter.set_stage_motion_axes(
                self,
                set(presentation.active_axes),
            )
        reported = presentation.reported_position
        if isinstance(reported, tuple) and len(reported) >= 2:
            design_workspace.maybe_restore_persisted_design(self, reported)
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_current_stage_position(
                presentation.contact_calibration_position
            )
        display_position = (
            reported
            if presentation.unhomed_fallback
            else presentation.presented_position
        )
        stage_position_panel_adapter.update_stage_position_display(
            self,
            display_position,
        )
        coordinate_flow.observe_coordinate_authority(
            self,
            presentation.physical_machine_pose,
            machine_snapshot=presentation.motion_coordinate_snapshot,
            homed_axes=presentation.homed_axes,
        )
        can_display_design = self._can_display_design_position()
        coordinate_xy = (
            presentation.presented_stage_xy
            if can_display_design and not presentation.unhomed_fallback
            else None
        )
        design_xy = (
            presentation.raw_stage_xy
            if can_display_design and presentation.unhomed_fallback
            else coordinate_xy
        )
        self._update_coordinate_display(center_xy=coordinate_xy)
        self._update_design_position(design_xy)
        if presentation.clear_motion_axes:
            stage_position_panel_adapter.clear_stage_motion_axes(self)

    def _raw_target_from_display_value(
        self, axis_name: str, display_target: float
    ) -> float | None:
        axis = axis_name.strip().upper()
        if axis not in self._stage_axis_raw_values:
            return None
        return stage_position_panel_adapter.raw_axis_value_from_display(
            self,
            axis,
            display_target,
        )

    def _resolve_stage_axis_target(
        self,
        axis_name: str,
        input_value: float,
        input_mode: str,
    ) -> tuple[float | None, float]:
        return resolve_stage_axis_target(
            self.STAGE_AXIS_NAMES,
            raw_target_from_display_value=self._raw_target_from_display_value,
            display_values=self._stage_axis_display_values,
            axis_name=axis_name,
            input_value=input_value,
            input_mode=input_mode,
        )

    def _api_machine_display_position(self) -> dict[str, float]:
        snapshot = self._api_machine_coordinate_snapshot()
        if snapshot is None:
            return {}
        return {
            axis: float(value)
            for axis, value in snapshot.physical_machine_pose.values.items()
            if axis in self.STAGE_AXIS_NAMES
        }

    def _api_machine_coordinate_snapshot(self) -> object | None:
        for getter_name in (
            "latest_motion_coordinate_snapshot",
            "latest_machine_coordinate_snapshot",
        ):
            getter = getattr(self.stage_controller, getter_name, None)
            if not callable(getter):
                continue
            snapshot = getter()
            if snapshot is not None:
                return snapshot
        return None

    def _resolve_api_stage_axis_target(
        self,
        axis_name: str,
        input_value: float,
        input_mode: str,
    ) -> tuple[float | None, float]:
        axis = str(axis_name).strip().upper()
        value = float(input_value)
        if axis not in self.STAGE_AXIS_NAMES:
            return None, value
        snapshot = self._api_machine_coordinate_snapshot()
        if snapshot is None:
            return None, value
        mode = normalize_api_coordinate_input_mode(input_mode) or "G90"
        if mode == "G91":
            current = snapshot.physical_machine_pose.values.get(axis)
            if current is None:
                return None, value
            physical_target = float(current) + value
        else:
            physical_target = value
        try:
            raw_target = snapshot.physical_machine_to_configured_controller(
                axis,
                physical_target,
            )
        except (TypeError, ValueError):
            return None, physical_target
        return float(raw_target), physical_target

    def _stage_axis_target_limit_error(
        self,
        axis_name: str,
        display_target: float,
    ) -> str | None:
        return stage_axis_target_limit_error(
            axis_name,
            display_target,
            homed_axes=self._stage_axis_homed,
            axis_display_limits=self.stage_controller.axis_display_limits,
        )

    def _machine_axis_target_limit_error(
        self,
        axis_name: str,
        physical_machine_target: float,
    ) -> str | None:
        return stage_axis_target_limit_error(
            axis_name,
            physical_machine_target,
            homed_axes=self._stage_axis_homed,
            axis_display_limits=self.stage_controller.axis_machine_display_limits,
        )

    def _refresh_controller_status(self) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return
        self.stage_controller.request_status_refresh()

    def _refresh_design_position(self) -> None:
        if self._design_session.document is None:
            return
        if self.serial_connection is None or not self.serial_connection.is_open:
            self._update_design_position(None)
            return
        preferred_stage_xy = stage_position_update.preferred_design_display_stage_xy(
            self
        )
        if preferred_stage_xy is None:
            self.stage_controller.request_status_refresh()
            return
        self._pending_design_stage_xy = preferred_stage_xy
        self._flush_pending_design_position()

    def _flush_pending_design_position(self) -> None:
        stage_xy = self._pending_design_stage_xy
        self._pending_design_stage_xy = None
        self._update_design_position(stage_xy)

    def _update_design_position(self, stage_xy: tuple[float, float] | None) -> None:
        self._current_design_stage_xy = stage_xy
        design_xy = None
        fov_design_size = None
        if stage_xy is not None:
            design_xy = self._design_xy_from_raw_stage_xy(stage_xy)
            fov_design_size = self._resolve_design_fov_size()
        coordinate_snapshot = self._coordinate_system_coordinator.snapshot()
        p = navigation_targeting.design_position_presentation(
            self._design_session,
            coordinate_snapshot.registration,
            stage_xy=stage_xy,
            design_xy=design_xy,
            fov_design_size=fov_design_size,
            last_selected_design_point=self._last_selected_design_point,
        )
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_current_position(
                p.stage_xy, p.current_design_position, fov_design_size=p.fov_design_size
            )
        if self.design_layout_window is not None:
            self.design_layout_window.set_current_design_position(
                p.current_design_position, fov_design_size=p.fov_design_size
            )
        self.view.set_design_minimap_data(
            document=p.document,
            targets=p.targets,
            selected_target_id=p.selected_target_id,
            probe_route=p.probe_route,
            selected_route_point_index=p.selected_route_point_index,
            selected_design_point=p.selected_design_point,
            current_design_position=p.current_design_position,
            fov_design_size=p.fov_design_size,
            source_design_marks=p.source_design_marks,
            check_design_marks=p.check_design_marks,
        )

    def _log_design_position_reconcile(
        self,
        predicted_stage_xy: tuple[float, float],
        actual_stage_xy: tuple[float, float],
    ) -> None:
        state = self.stage_controller.latest_stage_state()
        predicted_design_xy = self._design_xy_from_raw_stage_xy(predicted_stage_xy)
        actual_design_xy = self._design_xy_from_raw_stage_xy(actual_stage_xy)
        delta_x = float(actual_stage_xy[0] - predicted_stage_xy[0])
        delta_y = float(actual_stage_xy[1] - predicted_stage_xy[1])
        logger.debug(
            "MOTION PREDICTION reconcile predicted_stage=%s actual_stage=%s delta=(%.4f, %.4f) delta_norm=%.4f state=%s predicted_design=%s actual_design=%s",
            self._format_optional_point(predicted_stage_xy),
            self._format_optional_point(actual_stage_xy),
            delta_x,
            delta_y,
            math.hypot(delta_x, delta_y),
            state,
            self._format_optional_point(predicted_design_xy),
            self._format_optional_point(actual_design_xy),
        )

    @staticmethod
    def _format_optional_point(point: tuple[float, float] | None) -> str:
        return (
            "None"
            if point is None
            else f"({float(point[0]):.4f}, {float(point[1]):.4f})"
        )

    def _resolve_design_fov_size(self) -> tuple[float, float] | None:
        import numpy as np

        registration = self._coordinate_system_coordinator.snapshot().registration.registration_projection
        if registration is None or not registration.valid:
            return None
        stage_fov = self.stage_controller.current_fov_size_mm()
        if stage_fov is None:
            return None
        try:
            inverse = np.linalg.inv(registration.matrix)
        except np.linalg.LinAlgError:
            return None
        width_vec = inverse @ np.asarray([float(stage_fov[0]), 0.0], dtype=float)
        height_vec = inverse @ np.asarray([0.0, float(stage_fov[1])], dtype=float)
        design_unit_mm = abs(float(registration.design_unit_mm))
        if not math.isfinite(design_unit_mm) or design_unit_mm <= 0.0:
            return None
        return (
            float(np.linalg.norm(width_vec)) / design_unit_mm,
            float(np.linalg.norm(height_vec)) / design_unit_mm,
        )
