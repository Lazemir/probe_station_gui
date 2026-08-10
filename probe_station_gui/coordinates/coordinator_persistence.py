"""Private persistence reducer for the coordinate-system coordinator."""

from __future__ import annotations

from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class _ContactCommitRollback:
    session_identity: int
    frame_id: str
    frame_version: int


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
            ),
            publication_outcomes,
        )

    def _roll_back(self, completed: tuple[_JournalEntry, ...]) -> None:
        checkpoint = None
        for entry in completed:
            publication = entry.publication
            if (
                checkpoint is None
                and publication.proposed_session_link is not None
                and publication.previous_session is not None
            ):
                checkpoint = publication.previous_session
        previous_records = completed[0].previous_records if completed else ()
        self._registry.reset(previous_records)
        if checkpoint is not None:
            checkpoint.restore(self._session)
        assert self._document is not None
        self._document = self._document.with_records(
            self._registry.snapshot().records
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
