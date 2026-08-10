"""Registration mark capture and X/Y/B fit workflow."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from probe_station_gui.design import objective_offsets
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
    commit_xyb_registration,
    update_check_registration,
)
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.design.registration_lifecycle import (
    DesignRegistrationLifecycle,
    RegistrationCancellation,
    RegistrationCaptureContext,
    RegistrationCaptureOutcome,
    RegistrationCaptureToken,
    RegistrationEffects,
    RegistrationSample,
)
from probe_station_gui.design.session import DesignSession

from .coordinator_model import (
    CaptureMachinePoseIntent,
    CoordinateNotice,
    DesignSessionCheckpoint,
    FinishOperatorAlignmentUiEffect,
    FrameRecordsPublication,
    MachinePoseCaptureResult,
    OperatorPickRelease,
    RegistrationCaptureRequest,
    RestoreOperatorAlignmentUiEffect,
    _RegistrationTransitionParts,
)
from .registry import CoordinateFrameRegistry
from .transforms import rotate_xy


class RegistrationCaptureWorkflow:
    """Own synchronized capture tokens, evidence, normalization, and fit."""

    def __init__(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        lifecycle: DesignRegistrationLifecycle,
        allocate_intent_id: Callable[[], int],
        apply_effects: Callable[[RegistrationEffects], None],
    ) -> None:
        self._registry = registry
        self._session = session
        self._lifecycle = lifecycle
        self._allocate_intent_id = allocate_intent_id
        self._apply_effects = apply_effects
        self._tokens: dict[int, RegistrationCaptureToken] = {}
        self._operator_stage_marks: dict[int, tuple[float, float]] = {}
        self._operator_design_marks: tuple[tuple[float, float], ...] | None = None
        self._fit_residuals: tuple[float, float] | None = None

    @property
    def operator_stage_marks(self) -> tuple[tuple[float, float] | None, ...]:
        count = max(self._operator_stage_marks, default=-1) + 1
        return tuple(self._operator_stage_marks.get(index) for index in range(count))

    @property
    def fit_residuals(self) -> tuple[float, float] | None:
        return self._fit_residuals

    def begin(
        self,
        request: RegistrationCaptureRequest,
    ) -> _RegistrationTransitionParts:
        context = self._context(request)
        if context is None:
            return _RegistrationTransitionParts(
                notices=(self._warning("Design coordinate frame is unavailable."),)
            )
        token = self._lifecycle.begin_capture(context)
        superseded = _RegistrationTransitionParts()
        if token.superseded_effects.restore_baseline:
            self.observe_effects(token.superseded_effects)
            superseded = self.cancel(token.superseded_effects)
        if not token.capture_allowed:
            return _RegistrationTransitionParts(
                notices=(
                    CoordinateNotice(
                        "Alignment point capture is already running.",
                        duration_ms=4000,
                    ),
                ),
                view_changed=superseded.view_changed,
                ui_effects=superseded.ui_effects,
            )
        if token.operator_alignment:
            self._operator_design_marks = tuple(token.source_design_marks)
        intent_id = self._allocate_intent_id()
        self._tokens[intent_id] = token
        return _RegistrationTransitionParts(
            intents=(CaptureMachinePoseIntent(intent_id),),
            view_changed=superseded.view_changed,
            ui_effects=superseded.ui_effects,
        )

    def complete(
        self,
        intent_id: int,
        result: object,
    ) -> _RegistrationTransitionParts | None:
        if not isinstance(result, MachinePoseCaptureResult):
            return None
        token = self._tokens.pop(intent_id, None)
        if token is None or result.intent_id != intent_id:
            return _RegistrationTransitionParts()
        if not self._token_context_is_current(token):
            effects = self._lifecycle.cancel(RegistrationCancellation.FRAME_CHANGED)
            return self._rollback_transition(token, effects)
        checkpoint = DesignSessionCheckpoint.capture(self._session)
        succeeded = bool(result.succeeded and result.snapshot is not None)
        callback = self._lifecycle.accept_sample(
            token,
            RegistrationCaptureOutcome(
                succeeded=succeeded,
                message=(
                    None
                    if succeeded
                    else str(result.message or "Machine coordinates are unavailable.")
                ),
            ),
        )
        if not callback.accepted:
            return _RegistrationTransitionParts()
        self.observe_effects(callback)
        if not succeeded:
            return _RegistrationTransitionParts(
                notices=(
                    self._warning(
                        callback.reason or "Machine coordinates are unavailable."
                    ),
                )
            )
        try:
            sample = self._sample(token, result.snapshot)
        except Exception as exc:
            failure = self._lifecycle.accept_sample(
                token,
                RegistrationCaptureOutcome(succeeded=False, message=str(exc)),
            )
            if failure.accepted:
                self.observe_effects(failure)
                rollback = self._lifecycle.cancel(
                    RegistrationCancellation.MARK_SET_CHANGED
                )
                return self._rollback_transition(
                    token,
                    rollback,
                    notice=self._warning(failure.reason or str(exc)),
                )
            return _RegistrationTransitionParts(
                notices=(self._warning(failure.reason or str(exc)),),
            )
        effects = self._lifecycle.accept_sample(
            token,
            sample,
        )
        if not effects.accepted:
            return _RegistrationTransitionParts()
        try:
            if token.frame_id is None:
                self._session.record_legacy_stage_coordinate_provenance(
                    self._legacy_capture_provenance(result.snapshot)
                )
            self.observe_effects(effects)
        except Exception as exc:
            rollback = effects.rollback_effects or self._lifecycle.cancel(
                RegistrationCancellation.MARK_SET_CHANGED
            )
            restored = (
                checkpoint
                if rollback is None
                else checkpoint.with_registration_baseline(rollback)
            )
            restored.restore(self._session)
            self.clear(rollback)
            return _RegistrationTransitionParts(
                notices=(self._warning(str(exc)),),
                view_changed=bool(rollback and rollback.restore_baseline),
                ui_effects=self._rollback_ui_effects(token, rollback),
            )
        pick_release = self._operator_pick_release(token, result)
        if not effects.commit_requested:
            return _RegistrationTransitionParts(
                notices=(self._capture_notice(token, sample),),
                operator_pick_release=pick_release,
            )
        committed = self._commit(
            token,
            effects,
            checkpoint=checkpoint,
            snapshot=result.snapshot,
        )
        return replace(committed, operator_pick_release=pick_release)

    @staticmethod
    def _operator_pick_release(
        token: RegistrationCaptureToken,
        result: MachinePoseCaptureResult,
    ) -> OperatorPickRelease | None:
        if not token.operator_alignment or token.mark_index is None:
            return None
        matching_unarmed = bool(
            result.active_operator_pick_slot is None
            and token.operator_pick_generation is None
        )
        matching_armed = bool(
            result.active_operator_pick_slot == token.mark_index
            and token.operator_pick_generation is not None
            and result.active_operator_pick_generation
            == token.operator_pick_generation
        )
        if not matching_unarmed and not matching_armed:
            return None
        return OperatorPickRelease(
            slot=token.mark_index,
            generation=token.operator_pick_generation,
        )

    def observe_effects(self, effects: RegistrationEffects) -> None:
        self._apply_effects(effects)
        if effects.restore_baseline and effects.operator_alignment:
            self._operator_stage_marks = {
                index: point
                for index, point in enumerate(effects.baseline_source_stage_marks)
                if point is not None
            }
        sample = effects.captured_sample
        if (
            effects.operator_alignment
            and sample is not None
            and sample.stage_xy is not None
            and sample.mark_index is not None
        ):
            self._operator_stage_marks[sample.mark_index] = sample.stage_xy

    def clear(self, effects: RegistrationEffects | None = None) -> None:
        self._tokens.clear()
        self._operator_stage_marks.clear()
        self._operator_design_marks = None
        self._fit_residuals = None
        if effects is not None and effects.restore_baseline and effects.operator_alignment:
            self._operator_stage_marks = {
                index: point
                for index, point in enumerate(effects.baseline_source_stage_marks)
                if point is not None
            }

    def cancel(self, effects: RegistrationEffects) -> _RegistrationTransitionParts:
        ui_effects = self._cancellation_ui_effects(effects)
        self.clear(effects)
        return _RegistrationTransitionParts(
            view_changed=effects.restore_baseline,
            ui_effects=ui_effects,
        )

    def _cancellation_ui_effects(
        self,
        effects: RegistrationEffects,
    ) -> tuple[RestoreOperatorAlignmentUiEffect, ...]:
        design_marks = self._operator_design_marks
        if (
            design_marks is None
            or not effects.restore_baseline
            or not effects.operator_alignment
        ):
            return ()
        return (
            RestoreOperatorAlignmentUiEffect(
                design_marks=design_marks,
                stage_marks=tuple(
                    None
                    if point is None
                    else (float(point[0]), float(point[1]))
                    for point in effects.baseline_source_stage_marks
                ),
            ),
        )

    def _commit(
        self,
        token: RegistrationCaptureToken,
        effects: RegistrationEffects,
        *,
        checkpoint: DesignSessionCheckpoint,
        snapshot: object,
    ) -> _RegistrationTransitionParts:
        document = self._session.document
        rollback = effects.rollback_effects
        if (
            token.frame_id is None
            or document is None
            or not effects.normalized_samples
            or rollback is None
        ):
            return _RegistrationTransitionParts()
        checkpoint = checkpoint.with_registration_baseline(rollback)
        current = self._registry.get(token.frame_id)
        if current is None or current.version != token.frame_version:
            return self._rollback_transition(token, rollback)
        try:
            source_identity = (
                str(Path(document.path).expanduser().resolve()),
                str(document.source_load_id),
            )
        except OSError as exc:
            return self._rollback_transition(
                token,
                rollback,
                notice=self._warning(str(exc)),
            )
        if not self._context_is_current(token, source_identity):
            return self._rollback_transition(token, rollback)
        metadata = DesignFrameMetadata.from_mapping(current.metadata)
        reference = effects.normalized_samples[0]
        target_b = reference.reference_b_deg
        pivot = reference.captured_pivot_machine_xy
        turns = token.rotation_quarter_turns
        design_marks = tuple(
            document.rotate_point(point, -turns) for point in token.source_design_marks
        )
        check_design_marks = tuple(
            document.rotate_point(point, -turns) for point in token.check_design_marks
        )
        new_source = tuple(
            item.machine_xy
            for item in effects.normalized_samples
            if item.mark_kind == "source"
        )
        new_checks = tuple(
            item.machine_xy
            for item in effects.normalized_samples
            if item.mark_kind == "check"
        )
        existing_source, existing_checks = self._existing_at_b(
            current,
            metadata,
            target_b=target_b,
            pivot=pivot,
        )
        try:
            committed = self._fit(
                current,
                metadata,
                design_marks=design_marks,
                physical_marks=existing_source + new_source,
                check_design_marks=check_design_marks,
                check_machine_marks=existing_checks + new_checks,
                target_b=target_b,
                pivot=pivot,
                has_new_source=bool(new_source),
            )
        except (DesignModelError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            return self._rollback_transition(
                token,
                rollback,
                notice=self._warning(str(exc)),
            )
        runtime_record = committed
        mapper = self._machine_mapper(snapshot, token.objective_xy_offset)
        try:
            try:
                projection = self._session.prepare_active_frame_link(
                    committed,
                    machine_point_for_navigation=mapper,
                    machine_b_deg=target_b,
                    pivot_machine_xy=pivot,
                )
            except DesignModelError as exc:
                runtime_record = committed.with_authority_block(
                    {"X", "Y", "B"},
                    str(exc),
                )
                projection = self._session.prepare_active_frame_link(
                    runtime_record,
                    machine_b_deg=target_b,
                    pivot_machine_xy=pivot,
                )
        except (DesignModelError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            return self._rollback_transition(
                token,
                rollback,
                notice=self._warning(str(exc)),
            )
        message, code = self._success(committed, token.operator_alignment)
        ui_effects = ()
        rollback_ui_effects = ()
        if token.operator_alignment:
            ui_effects = (FinishOperatorAlignmentUiEffect(),)
            rollback_ui_effects = self._rollback_ui_effects(token, rollback)
        return _RegistrationTransitionParts(
            notices=(self._saving_notice(),),
            publication=FrameRecordsPublication.for_committed_record(
                self._registry.snapshot().records,
                committed,
                previous_record=current,
                previous_session=checkpoint,
                projection=projection,
                runtime_record=runtime_record,
                success_message=message,
                success_duration_ms=7000,
                success_code=code,
                ui_effects=ui_effects,
                rollback_ui_effects=rollback_ui_effects,
            ),
        )

    def _rollback_transition(
        self,
        token: RegistrationCaptureToken,
        rollback: RegistrationEffects,
        *,
        notice: CoordinateNotice | None = None,
    ) -> _RegistrationTransitionParts:
        self.observe_effects(rollback)
        self.clear(rollback)
        return _RegistrationTransitionParts(
            notices=() if notice is None else (notice,),
            view_changed=rollback.restore_baseline,
            ui_effects=self._rollback_ui_effects(token, rollback),
        )

    @staticmethod
    def _rollback_ui_effects(
        token: RegistrationCaptureToken,
        rollback: RegistrationEffects | None,
    ) -> tuple[RestoreOperatorAlignmentUiEffect, ...]:
        if (
            rollback is None
            or not rollback.restore_baseline
            or not rollback.operator_alignment
        ):
            return ()
        return (
            RestoreOperatorAlignmentUiEffect(
                design_marks=tuple(
                    (float(point[0]), float(point[1]))
                    for point in token.source_design_marks
                ),
                stage_marks=tuple(
                    None
                    if point is None
                    else (float(point[0]), float(point[1]))
                    for point in rollback.baseline_source_stage_marks
                ),
            ),
        )

    def _context_is_current(
        self,
        token: RegistrationCaptureToken,
        source_identity: tuple[str, str],
    ) -> bool:
        return bool(
            source_identity == token.source_identity
            and self._token_context_is_current(token)
        )

    def _token_context_is_current(
        self,
        token: RegistrationCaptureToken,
    ) -> bool:
        document = self._session.document
        record = (
            None
            if token.frame_id is None
            else self._registry.get(token.frame_id)
        )
        try:
            source_identity = (
                str(Path(document.path).expanduser().resolve()),
                str(document.source_load_id),
            )
        except (AttributeError, OSError):
            return False
        return bool(
            document is not None
            and id(self._session) == token.session_identity
            and self._session.active_frame_id == token.frame_id
            and (
                (record is None and token.frame_id is None and token.frame_version is None)
                or (
                    record is not None
                    and record.version == token.frame_version
                )
            )
            and source_identity == token.source_identity
            and str(document.top_cell_name) == token.top_cell_name
            and int(document.rotation_quarter_turns) % 4
            == token.rotation_quarter_turns
            and tuple(self._session.source_design_marks_compact())
            == token.source_design_marks
            and tuple(self._session.check_design_marks) == token.check_design_marks
        )

    @staticmethod
    def _legacy_capture_provenance(snapshot: object) -> dict[str, object]:
        return {
            "position_reporting_mode": str(snapshot.position_reporting_mode),
            "coordinate_system": snapshot.coordinate_system,
            "work_offset": [float(value) for value in snapshot.work_offset],
        }

    def _capture_notice(
        self,
        token: RegistrationCaptureToken,
        sample: RegistrationSample,
    ) -> CoordinateNotice:
        if token.operator_alignment:
            remaining = max(
                0,
                len(token.source_design_marks) - len(self._operator_stage_marks),
            )
            point_label = "point" if remaining == 1 else "points"
            message = (
                f"Design alignment: point {int(token.mark_index or 0) + 1} "
                f"captured from {token.capture_source or 'stage'}. "
                f"Capture {remaining} remaining {point_label}."
            )
        else:
            message = (
                f"Stage {token.mark_kind} mark captured at "
                f"X={sample.physical_machine_xy[0]:.3f}, "
                f"Y={sample.physical_machine_xy[1]:.3f}."
            )
        return CoordinateNotice(
            message,
            duration_ms=4000,
            code="registration_capture_updated",
        )

    @staticmethod
    def _saving_notice() -> CoordinateNotice:
        return CoordinateNotice(
            "Saving design registration.",
            duration_ms=0,
            code="registration_save_pending",
        )

    @staticmethod
    def _existing_at_b(current, metadata, *, target_b, pivot):
        if current.transform is None:
            return (), ()
        delta_b = target_b - current.transform.reference_b_deg

        def rotate(point):
            value = rotate_xy((point[0] - pivot[0], point[1] - pivot[1]), delta_b)
            return pivot[0] + value[0], pivot[1] + value[1]

        return (
            tuple(rotate(point) for point in metadata.source_machine_marks),
            tuple(rotate(point) for point in metadata.check_machine_marks),
        )

    @staticmethod
    def _fit(
        current,
        metadata,
        *,
        design_marks,
        physical_marks,
        check_design_marks,
        check_machine_marks,
        target_b,
        pivot,
        has_new_source,
    ):
        if len(metadata.source_machine_marks) >= 2 and not has_new_source:
            fitted = update_check_registration(
                current,
                check_design_points=check_design_marks,
                physical_check_machine_points=check_machine_marks,
                physical_b_deg=target_b,
                pivot_machine_xy=pivot,
            )
        else:
            fitted = commit_xyb_registration(
                current,
                design_points=design_marks,
                physical_machine_points=physical_marks,
                physical_b_deg=target_b,
                pivot_machine_xy=pivot,
                check_design_points=check_design_marks,
                check_machine_points=check_machine_marks,
            )
        return replace(fitted, version=current.version + 1)

    def _success(self, committed, operator_alignment):
        if not operator_alignment:
            return "Design registration saved.", None
        metadata = DesignFrameMetadata.from_mapping(committed.metadata)
        self._fit_residuals = (
            float(metadata.rms_residual_mm or 0.0),
            float(metadata.max_residual_mm or 0.0),
        )
        self._operator_stage_marks.clear()
        self._operator_design_marks = None
        return (
            "Design alignment complete. "
            f"RMS {self._fit_residuals[0]:.4f} mm, "
            f"max {self._fit_residuals[1]:.4f} mm.",
            "operator_alignment_saved",
        )

    def _context(
        self,
        request: RegistrationCaptureRequest,
    ) -> RegistrationCaptureContext | None:
        document = self._session.document
        frame_id = self._session.active_frame_id
        record = self._registry.get(frame_id) if frame_id is not None else None
        if document is None:
            return None
        try:
            source_identity = (
                str(Path(document.path).expanduser().resolve()),
                str(document.source_load_id),
            )
            metadata = None if record is None else DesignFrameMetadata.from_mapping(record.metadata)
        except (KeyError, OSError, TypeError, ValueError):
            return None
        return RegistrationCaptureContext(
            session_identity=id(self._session),
            frame_id=frame_id,
            frame_version=None if record is None else record.version,
            source_identity=source_identity,
            top_cell_name=str(document.top_cell_name),
            rotation_quarter_turns=int(document.rotation_quarter_turns) % 4,
            pivot_machine_xy=request.pivot_machine_xy,
            objective_xy_offset=request.objective_xy_offset,
            mark_kind="check" if request.check_mark else "source",
            source_design_marks=tuple(self._session.source_design_marks_compact()),
            check_design_marks=tuple(self._session.check_design_marks),
            baseline_source_stage_marks=tuple(self._session.source_stage_marks),
            baseline_check_stage_marks=tuple(self._session.check_stage_marks),
            baseline_registration=self._session.registration,
            baseline_registration_status=str(self._session.registration_status),
            existing_source_machine_marks=(
                () if record is None or record.transform is None or metadata is None
                else tuple(metadata.source_machine_marks)
            ),
            existing_check_machine_marks=(
                () if record is None or record.transform is None or metadata is None
                else tuple(metadata.check_machine_marks)
            ),
            operator_alignment=request.operator_alignment,
            mark_index=request.mark_index,
            configured_target_xy=request.configured_target_xy,
            capture_source=request.capture_source,
            operator_pick_generation=request.operator_pick_generation,
        )

    @staticmethod
    def _sample(token: RegistrationCaptureToken, snapshot: object) -> RegistrationSample:
        physical_pose = snapshot.physical_machine_pose
        physical_b = physical_pose.require("B")
        if token.configured_target_xy is None:
            physical_xy = physical_pose.require("X"), physical_pose.require("Y")
            configured_xy = (
                snapshot.physical_machine_to_configured_controller("X", physical_xy[0]),
                snapshot.physical_machine_to_configured_controller("Y", physical_xy[1]),
            )
        else:
            configured_xy = token.configured_target_xy
            physical_xy = (
                snapshot.configured_controller_to_physical_machine("X", configured_xy[0]),
                snapshot.configured_controller_to_physical_machine("Y", configured_xy[1]),
            )
        stage_xy = objective_offsets.raw_stage_to_camera_stage(
            configured_xy,
            token.objective_xy_offset,
        )
        return RegistrationSample(
            mark_kind=token.mark_kind,
            physical_machine_xy=(float(physical_xy[0]), float(physical_xy[1])),
            physical_b_deg=float(physical_b),
            stage_xy=(float(stage_xy[0]), float(stage_xy[1])),
            mark_index=token.mark_index,
        )

    @staticmethod
    def _machine_mapper(snapshot: object, objective_offset):
        def map_point(machine_xy):
            configured = (
                snapshot.physical_machine_to_configured_controller("X", machine_xy[0]),
                snapshot.physical_machine_to_configured_controller("Y", machine_xy[1]),
            )
            return objective_offsets.raw_stage_to_camera_stage(configured, objective_offset)

        return map_point

    @staticmethod
    def _warning(message: str) -> CoordinateNotice:
        return CoordinateNotice(str(message), severity="warning", duration_ms=6000)


__all__ = ["RegistrationCaptureWorkflow"]
