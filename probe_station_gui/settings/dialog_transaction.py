"""Settings-dialog commit and reconciliation transaction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateTransition,
    CustomSystemsRequest,
    DesignCalibrationObservation,
)
from probe_station_gui.coordinates.design_calibration import (
    design_calibration_fingerprints,
)
from probe_station_gui.coordinates.rotation_geometry import (
    rotation_geometry_snapshot,
)
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.settings.manager import Settings, SettingsManager
from probe_station_gui.settings.objective_config import (
    ObjectivesSettings,
    normalize_objective_name,
)
from probe_station_gui.settings.software_coordinates import (
    SoftwareCoordinateSettings,
)
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot


@dataclass(frozen=True)
class SettingsDialogContext:
    stage_busy: bool
    objective_mutation_busy: bool
    machine_snapshot: MachineCoordinateSnapshot | None = None


@dataclass(frozen=True)
class SettingsDialogNotice:
    message: str
    timeout_ms: int


@dataclass(frozen=True)
class SettingsDialogOutcome:
    accepted: bool = False
    apply_objective_runtime: bool = True
    refresh_coordinate_frame_display: bool = False
    observe_coordinate_authority: bool = False
    post_apply_notices: tuple[SettingsDialogNotice, ...] = ()


@dataclass
class _TransactionState:
    settings: Settings
    previous_coordinates: SoftwareCoordinateSettings
    custom_transition: CoordinateTransition | None = None
    calibration_records_changed: bool = False
    objective_authority_changed: bool = False
    axis_calibrations_changed: bool = False
    active_objective_update_rejected: bool = False


class SettingsDialogTransaction:
    """Own one settings-dialog commit and coordinate reconciliation."""

    def __init__(
        self,
        settings_manager: SettingsManager,
        coordinate_system: CoordinateSystemCoordinator,
        *,
        publish_notice: Callable[[str, int], None],
        publish_transition: Callable[[CoordinateTransition], None],
    ) -> None:
        self._settings_manager = settings_manager
        self._coordinate_system = coordinate_system
        self._publish_notice = publish_notice
        self._publish_transition = publish_transition

    def apply(
        self,
        submitted: object,
        context: SettingsDialogContext,
    ) -> SettingsDialogOutcome:
        if not isinstance(submitted, Settings):
            return SettingsDialogOutcome()
        if context.stage_busy:
            self._publish_notice("Stage is busy; settings not changed.", 4000)
            return SettingsDialogOutcome()
        state = _TransactionState(
            settings=submitted.clone(),
            previous_coordinates=(self._settings_manager.settings.software_coordinates),
        )
        self._synchronize_custom_systems(state)
        self._validate_pivot(state, context.machine_snapshot)
        current_objectives = self._settings_manager.objectives_configuration()
        state.active_objective_update_rejected = self._preserve_active_objective(
            state.settings,
            current_objectives,
            context.objective_mutation_busy,
        )
        state.objective_authority_changed = self._objective_authority_changed(
            state.settings.objectives,
            current_objectives,
        )
        state.axis_calibrations_changed = (
            state.settings.axis_calibrations
            != self._settings_manager.settings.axis_calibrations
        )
        self._settings_manager.replace_and_save(
            state.settings,
            preserve_exposure_policy=True,
        )
        state.calibration_records_changed = self._publish_transitions(state)
        return self._outcome(state, context)

    def _synchronize_custom_systems(
        self,
        state: _TransactionState,
    ) -> None:
        custom_transition = None
        custom_frames_changed = (
            state.settings.software_coordinates.custom_frames
            != state.previous_coordinates.custom_frames
        )
        if custom_frames_changed and self._coordinate_system.snapshot().frames_loaded:
            try:
                custom_transition = self._coordinate_system.synchronize_custom_systems(
                    CustomSystemsRequest(state.settings.software_coordinates)
                )
            except (TypeError, ValueError) as exc:
                state.settings.software_coordinates = state.previous_coordinates.clone()
                self._publish_notice(str(exc), 6000)
        state.custom_transition = custom_transition

    def _validate_pivot(
        self,
        state: _TransactionState,
        machine_snapshot: MachineCoordinateSnapshot | None,
    ) -> None:
        pivot_changed = (
            state.settings.software_coordinates.pivot
            != state.previous_coordinates.pivot
        )
        if not pivot_changed:
            return
        coordinate_snapshot = self._coordinate_system.snapshot()
        if coordinate_snapshot.registration.active_frame_id is None:
            return
        try:
            rotation_geometry_snapshot(state.settings.software_coordinates)
            if machine_snapshot is None:
                raise DesignModelError(
                    "A current Machine-coordinate snapshot is required "
                    "to change the B-axis pivot."
                )
            machine_snapshot.physical_machine_pose.require("B")
        except (DesignModelError, TypeError, ValueError) as exc:
            state.settings.software_coordinates.pivot = (
                state.previous_coordinates.pivot.clone()
            )
            self._publish_notice(str(exc), 6000)

    @staticmethod
    def _preserve_active_objective(
        settings: Settings,
        current: ObjectivesSettings,
        mutation_busy: bool,
    ) -> bool:
        if not mutation_busy:
            return False
        current_name = normalize_objective_name(current.active_name)
        submitted = settings.objectives
        submitted_name = normalize_objective_name(submitted.active_name)
        current_profile = current.objectives.get(current_name)
        submitted_profile = submitted.objectives.get(current_name)
        rejected = (
            submitted_name != current_name or submitted_profile != current_profile
        )
        if not rejected:
            return False
        submitted.active_name = current_name
        if current_profile is None:
            submitted.objectives.pop(current_name, None)
        else:
            submitted.objectives[current_name] = current_profile.clone()
        return True

    @staticmethod
    def _objective_authority_changed(
        submitted: ObjectivesSettings,
        current: ObjectivesSettings,
    ) -> bool:
        current_name = normalize_objective_name(current.active_name)
        submitted_name = normalize_objective_name(submitted.active_name)
        return submitted_name != current_name or submitted.objectives.get(
            submitted_name
        ) != current.objectives.get(current_name)

    def _publish_transitions(self, state: _TransactionState) -> bool:
        if state.custom_transition is not None:
            self._publish_transition(state.custom_transition)
        if self._coordinate_system.snapshot().frames_loaded:
            calibration_transition = (
                self._coordinate_system.observe_design_calibrations(
                    DesignCalibrationObservation(
                        design_calibration_fingerprints(
                            self._settings_manager.settings.axis_calibrations
                        )
                    )
                )
            )
            self._publish_transition(calibration_transition)
            return bool(calibration_transition.view_changed)
        return False

    @staticmethod
    def _outcome(
        state: _TransactionState,
        context: SettingsDialogContext,
    ) -> SettingsDialogOutcome:
        return SettingsDialogOutcome(
            accepted=True,
            apply_objective_runtime=not context.objective_mutation_busy,
            refresh_coordinate_frame_display=(
                state.settings.software_coordinates != state.previous_coordinates
                or state.calibration_records_changed
            ),
            observe_coordinate_authority=(
                state.settings.software_coordinates.pivot
                != state.previous_coordinates.pivot
                or state.objective_authority_changed
                or state.axis_calibrations_changed
            ),
            post_apply_notices=(
                (
                    SettingsDialogNotice(
                        "Stage is busy; active objective settings not changed.",
                        4000,
                    ),
                )
                if state.active_objective_update_rejected
                else ()
            ),
        )
