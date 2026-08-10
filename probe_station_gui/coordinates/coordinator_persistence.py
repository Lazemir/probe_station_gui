"""Private persistence reducer for the coordinate-system coordinator."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Protocol

from probe_station_gui.design.session import DesignSession

from .coordinator_model import (
    CoordinateAdapterCompletion,
    CoordinateNotice,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    DesignSessionCheckpoint,
    FrameRecordsPublication,
    LoadCoordinateFramesIntent,
    MachineProfileObservation,
    SaveCoordinateFramesIntent,
)
from .lifecycle import (
    CoordinateFrameLifecycle,
    FrameLoadResult,
    FramePublication,
    FramePublicationResult,
)
from .model import CoordinateFrameRecord
from .provenance import mark_design_frame_provenance_pending
from .registry import CoordinateFrameRegistry
from .store_model import (
    CoordinateFrameLoadResult,
    CoordinateFrameStoreFailure,
    CoordinateFrameStoreSuccess,
)


class _CoordinateFrameDocument(Protocol):
    records: tuple[CoordinateFrameRecord, ...]
    diagnostics: tuple[object, ...]

    def with_records(
        self,
        records: tuple[CoordinateFrameRecord, ...],
    ) -> _CoordinateFrameDocument: ...


@dataclass(frozen=True)
class _JournalEntry:
    request_id: int
    publication: FrameRecordsPublication
    previous_records: tuple[CoordinateFrameRecord, ...]
    contact_rollback: _ContactCommitRollback | None
    applied_session_lease: _SessionRollbackLease | None = None


@dataclass(frozen=True)
class _ContactCommitRollback:
    session_identity: int
    frame_id: str
    frame_version: int


@dataclass(frozen=True)
class _SessionRollbackLease:
    session_identity: int
    source_path: str
    source_load_id: str
    top_cell_name: str
    visible_layers: tuple[tuple[int, int], ...]
    rotation_quarter_turns: int
    active_frame_id: str
    coordinate_fingerprint: tuple[object, ...]


_Completion = tuple[CoordinateTransition, tuple[_ContactCommitRollback, ...]]


class CoordinatePersistenceReducer:
    """Own load generations, durable document, and publication rollback."""

    def __init__(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        lifecycle: CoordinateFrameLifecycle | None = None,
    ) -> None:
        self._registry = registry
        self._session = session
        self._lifecycle = lifecycle or CoordinateFrameLifecycle()
        self._next_intent_id = 0
        self._current_load_intent_id: int | None = None
        self._frames_loaded = False
        self._document: _CoordinateFrameDocument | None = None
        self._journal: dict[int, _JournalEntry] = {}

    def snapshot(self) -> CoordinateSystemSnapshot:
        return CoordinateSystemSnapshot(
            frames_loaded=self._frames_loaded,
            records=self._registry.snapshot().records,
            document=self._document,
            selected_frame_id=self._lifecycle.selected_frame_id,
        )

    def start(self, profile: MachineProfileObservation) -> CoordinateTransition:
        request_id = self._allocate_intent_id()
        self._current_load_intent_id = request_id
        effects = self._lifecycle.begin_load(request_id)
        records = self._registry.snapshot().records
        if records:
            self._registry.reset(mark_design_frame_provenance_pending(records))
        if self._session.active_frame_id and effects.invalidate_session_reason:
            self._session.invalidate_registration(effects.invalidate_session_reason)
        self._frames_loaded = False
        return CoordinateTransition(
            snapshot=self.snapshot(),
            intents=(
                LoadCoordinateFramesIntent(
                    intent_id=request_id,
                    machine_profile_id=profile.machine_profile_id,
                ),
            ),
        )

    def publish(
        self,
        publication: FrameRecordsPublication,
    ) -> CoordinateTransition:
        if (
            publication.proposed_session_link is not None
            and publication.previous_session is None
        ):
            publication = replace(
                publication,
                previous_session=DesignSessionCheckpoint.capture(self._session),
            )
        request_id = self._allocate_intent_id()
        records = tuple(publication.records)
        self._validate_records(records)
        if self._document is None:
            raise RuntimeError("Coordinate frames must load before publication.")
        document = self._document.with_records(records)
        previous_records = self._registry.snapshot().records
        entry = _JournalEntry(
            request_id=request_id,
            publication=publication,
            previous_records=previous_records,
            contact_rollback=self._contact_commit_rollback(publication),
        )
        self._journal[request_id] = entry
        self._lifecycle.track_publication(
            FramePublication(
                request_id=request_id,
                records=records,
                previous_record=publication.previous_record,
                committed_record=publication.committed_record,
                success_message=(
                    None
                    if publication.success_notice is None
                    else publication.success_notice.message
                ),
            )
        )
        try:
            self._registry.reset(records)
            self._apply_session_link(publication)
            self._journal[request_id] = replace(
                entry,
                applied_session_lease=self._capture_session_rollback_lease(
                    publication
                ),
            )
        except Exception:
            self._registry.reset(previous_records)
            if publication.previous_session is not None:
                publication.previous_session.restore(self._session)
            self._journal.pop(request_id, None)
            self._lifecycle.finish_publication(
                FramePublicationResult(request_id=request_id, succeeded=False)
            )
            raise
        self._document = document
        return CoordinateTransition(
            snapshot=self.snapshot(),
            intents=(SaveCoordinateFramesIntent(request_id, document),),
            view_changed=True,
            ui_effects=publication.ui_effects,
        )

    def complete(
        self,
        completion: CoordinateAdapterCompletion,
    ) -> _Completion:
        result = completion.result
        result_request_id = getattr(result, "request_id", None)
        if (
            not isinstance(result_request_id, int)
            or result_request_id != int(completion.intent_id)
        ):
            return CoordinateTransition(self.snapshot()), ()
        if isinstance(result, CoordinateFrameLoadResult):
            return self._complete_load(result), ()
        if (
            isinstance(result, CoordinateFrameStoreFailure)
            and result.operation == "load"
        ):
            return self._complete_load_failure(result), ()
        if isinstance(result, CoordinateFrameStoreSuccess) and result.operation == "save":
            return self._complete_save(result_request_id, succeeded=True)
        if isinstance(result, CoordinateFrameStoreFailure) and result.operation == "save":
            return self._complete_save(result_request_id, succeeded=False)
        return CoordinateTransition(self.snapshot()), ()

    def _complete_load(
        self,
        result: CoordinateFrameLoadResult,
    ) -> CoordinateTransition:
        request_id = result.request_id
        document = result.document
        if request_id != self._current_load_intent_id or not hasattr(
            document,
            "with_records",
        ):
            return CoordinateTransition(self.snapshot())
        assert document is not None
        records = document.records if result.runtime_records is None else result.runtime_records
        effects = self._lifecycle.accept_load(
            FrameLoadResult(request_id, tuple(records))
        )
        if effects.replace_records is None:
            return CoordinateTransition(self.snapshot())
        self._current_load_intent_id = None
        self._document = document
        self._registry.reset(effects.replace_records)
        self._frames_loaded = True
        diagnostics = (
            *document.diagnostics,
            *result.provenance_diagnostics,
        )
        notices = (
            (
                CoordinateNotice(
                    "Some Design coordinate frames are unavailable.",
                    severity="warning",
                    duration_ms=6000,
                ),
            )
            if diagnostics
            else ()
        )
        return CoordinateTransition(self.snapshot(), notices=notices)

    def _complete_load_failure(
        self,
        result: CoordinateFrameStoreFailure,
    ) -> CoordinateTransition:
        if result.request_id != self._current_load_intent_id:
            return CoordinateTransition(self.snapshot())
        self._current_load_intent_id = None
        return CoordinateTransition(
            self.snapshot(),
            notices=(
                CoordinateNotice(
                    "Design coordinate frames could not be loaded.",
                    severity="error",
                    duration_ms=6000,
                ),
            ),
        )

    def _complete_save(
        self,
        request_id: int,
        *,
        succeeded: bool,
    ) -> _Completion:
        if request_id not in self._journal:
            return CoordinateTransition(self.snapshot()), ()
        effects = self._lifecycle.finish_publication(
            FramePublicationResult(request_id=request_id, succeeded=succeeded)
        )
        if effects.failure_deferred:
            return CoordinateTransition(self.snapshot()), ()
        completed = tuple(
            self._journal[candidate]
            for candidate in sorted(self._journal)
            if candidate <= request_id
        )
        for entry in completed:
            self._journal.pop(entry.request_id, None)
        if succeeded:
            notices = tuple(
                notice
                for entry in completed
                if (notice := entry.publication.success_notice) is not None
                and self._success_notice_is_current(entry.publication, notice)
            )
            return CoordinateTransition(self.snapshot(), notices=notices), ()
        self._roll_back(completed)
        failure_code = (
            "operator_alignment_rollback"
            if any(
                entry.publication.success_notice is not None
                and entry.publication.success_notice.code
                == "operator_alignment_saved"
                for entry in completed
            )
            else None
        )
        publication_outcomes = tuple(
            outcome
            for entry in completed
            if (outcome := entry.contact_rollback) is not None
        )
        return (
            CoordinateTransition(
                self.snapshot(),
                notices=(
                    CoordinateNotice(
                        "Design coordinate frames could not be saved.",
                        severity="error",
                        duration_ms=6000,
                        code=failure_code,
                    ),
                ),
                view_changed=True,
                ui_effects=tuple(
                    effect
                    for entry in completed
                    for effect in entry.publication.rollback_ui_effects
                ),
            ),
            publication_outcomes,
        )

    def _roll_back(self, completed: tuple[_JournalEntry, ...]) -> None:
        checkpoint = None
        applied_session_lease = None
        for entry in completed:
            publication = entry.publication
            if publication.proposed_session_link is not None:
                if checkpoint is None and publication.previous_session is not None:
                    checkpoint = publication.previous_session
                applied_session_lease = entry.applied_session_lease
        previous_records = completed[0].previous_records if completed else ()
        self._registry.reset(previous_records)
        if (
            checkpoint is not None
            and applied_session_lease is not None
            and self._session_rollback_lease_is_current(applied_session_lease)
        ):
            if self._checkpoint_matches_applied_document(
                checkpoint,
                applied_session_lease,
            ):
                checkpoint.restore_coordinate_state(self._session)
            else:
                checkpoint.restore(self._session)
        assert self._document is not None
        self._document = self._document.with_records(
            self._registry.snapshot().records
        )

    def _capture_session_rollback_lease(
        self,
        publication: FrameRecordsPublication,
    ) -> _SessionRollbackLease | None:
        if publication.proposed_session_link is None:
            return None
        document = self._session.document
        frame_id = self._session.active_frame_id
        if document is None or frame_id is None:
            return None
        try:
            source_path = str(document.path.expanduser().resolve())
        except OSError:
            return None
        return _SessionRollbackLease(
            session_identity=id(self._session),
            source_path=source_path,
            source_load_id=str(document.source_load_id),
            top_cell_name=str(document.top_cell_name),
            visible_layers=tuple(sorted(document.visible_layers)),
            rotation_quarter_turns=int(document.rotation_quarter_turns) % 4,
            active_frame_id=str(frame_id),
            coordinate_fingerprint=self._coordinate_state_fingerprint(
                self._session
            ),
        )

    def _session_rollback_lease_is_current(
        self,
        lease: _SessionRollbackLease,
    ) -> bool:
        document = self._session.document
        if document is None:
            return False
        try:
            source_path = str(document.path.expanduser().resolve())
        except OSError:
            return False
        return bool(
            id(self._session) == lease.session_identity
            and source_path == lease.source_path
            and str(document.source_load_id) == lease.source_load_id
            and str(document.top_cell_name) == lease.top_cell_name
            and tuple(sorted(document.visible_layers)) == lease.visible_layers
            and int(document.rotation_quarter_turns) % 4
            == lease.rotation_quarter_turns
            and self._session.active_frame_id == lease.active_frame_id
            and self._coordinate_state_fingerprint(self._session)
            == lease.coordinate_fingerprint
        )

    @classmethod
    def _coordinate_state_fingerprint(
        cls,
        session: DesignSession,
    ) -> tuple[object, ...]:
        return (
            session.active_frame_id,
            cls._point_fingerprint(session.source_design_marks),
            cls._point_fingerprint(session.source_stage_marks),
            cls._point_fingerprint(session.check_design_marks),
            cls._point_fingerprint(session.check_stage_marks),
            cls._registration_fingerprint(session.registration),
            str(session.registration_status),
            cls._json_fingerprint(session._runtime_blocked_persisted_state),
            cls._json_fingerprint(session._legacy_stage_coordinate_provenance),
            bool(session._legacy_stage_coordinate_provenance_present),
        )

    @staticmethod
    def _point_fingerprint(points) -> tuple[tuple[float, float], ...]:
        return tuple((float(point[0]), float(point[1])) for point in points)

    @classmethod
    def _registration_fingerprint(cls, registration) -> object:
        if registration is None:
            return None
        try:
            source_summary = registration.source_residual_summary
            check_summary = registration.residual_summary
            return (
                cls._point_fingerprint(registration.source_design_marks),
                cls._point_fingerprint(registration.source_stage_marks),
                cls._point_fingerprint(registration.check_design_marks),
                cls._point_fingerprint(registration.check_stage_marks),
                tuple(
                    tuple(float(value) for value in row)
                    for row in registration.matrix
                ),
                tuple(float(value) for value in registration.offset),
                float(registration.design_unit_mm),
                float(registration.distance_scale_ratio),
                tuple(float(value) for value in registration.source_residuals),
                (
                    int(source_summary.count),
                    float(source_summary.rms),
                    float(source_summary.max_error),
                ),
                tuple(float(value) for value in registration.residuals),
                (
                    int(check_summary.count),
                    float(check_summary.rms),
                    float(check_summary.max_error),
                ),
                bool(registration.valid),
                str(registration.stale_reason),
            )
        except (AttributeError, TypeError, ValueError):
            return (type(registration).__qualname__, repr(registration))

    @staticmethod
    def _json_fingerprint(value: object) -> str:
        try:
            return json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError):
            return repr(value)

    @staticmethod
    def _checkpoint_matches_applied_document(
        checkpoint: DesignSessionCheckpoint,
        lease: _SessionRollbackLease,
    ) -> bool:
        state = checkpoint.state
        document = None if state is None else state.document
        if document is None:
            return False
        try:
            source_path = str(document.path.expanduser().resolve())
        except OSError:
            return False
        return bool(
            source_path == lease.source_path
            and str(document.source_load_id) == lease.source_load_id
            and str(document.top_cell_name) == lease.top_cell_name
            and tuple(sorted(document.visible_layers)) == lease.visible_layers
            and int(document.rotation_quarter_turns) % 4
            == lease.rotation_quarter_turns
        )

    def _success_notice_is_current(
        self,
        publication: FrameRecordsPublication,
        notice: CoordinateNotice,
    ) -> bool:
        if notice.code == "legacy_migration_saved":
            return True
        committed = publication.committed_record
        if committed is None:
            return False
        if str(notice.code or "").startswith("legacy_migration_saved:"):
            current = self._registry.get(committed.frame_id)
            return bool(
                current is not None
                and current.version >= committed.version
                and self._session.active_frame_id == committed.frame_id
            )
        return bool(
            self._registry.get(committed.frame_id) == committed
            and self._session.active_frame_id == committed.frame_id
        )

    def _apply_session_link(self, publication: FrameRecordsPublication) -> None:
        link = publication.proposed_session_link
        if link is None:
            return
        record = next(
            (
                candidate
                for candidate in publication.records
                if candidate.frame_id == link.frame_id
            ),
            None,
        )
        if record is None:
            raise ValueError("Session link frame is absent from the publication.")
        runtime_record = link.runtime_record or record
        if runtime_record.frame_id != record.frame_id:
            raise ValueError("Runtime session frame does not match the publication.")
        self._session.apply_active_frame_link(runtime_record, link.projection)

    def _contact_commit_rollback(
        self,
        publication: FrameRecordsPublication,
    ) -> _ContactCommitRollback | None:
        previous = publication.previous_record
        committed = publication.committed_record
        if (
            previous is None
            or committed is None
            or previous.frame_id != committed.frame_id
            or previous.transform.a_zero_machine_mm is not None
            or committed.transform.a_zero_machine_mm is None
        ):
            return None
        return _ContactCommitRollback(
            session_identity=id(self._session),
            frame_id=previous.frame_id,
            frame_version=previous.version,
        )

    def _allocate_intent_id(self) -> int:
        self._next_intent_id += 1
        return self._next_intent_id

    @staticmethod
    def _validate_records(records: tuple[CoordinateFrameRecord, ...]) -> None:
        if any(not isinstance(record, CoordinateFrameRecord) for record in records):
            raise TypeError("Frame publications require CoordinateFrameRecord values.")
        frame_ids = [record.frame_id for record in records]
        if len(set(frame_ids)) != len(frame_ids):
            raise ValueError("A frame publication contains duplicate frame IDs.")


__all__ = ["CoordinatePersistenceReducer"]
