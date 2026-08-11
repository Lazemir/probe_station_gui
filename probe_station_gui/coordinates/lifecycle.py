"""Pure load and publication lifecycle decisions for coordinate frames."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .model import CoordinateFrameRecord


if TYPE_CHECKING:
    from probe_station_gui.design.registration_lifecycle import RegistrationEffects


MACHINE_FRAME_ID = "machine"


@dataclass(frozen=True)
class FrameLifecycleEffects:
    replace_records: tuple[CoordinateFrameRecord, ...] | None = None
    invalidate_session_reason: str | None = None
    refresh_display: bool = False
    acknowledged_publications: tuple[FramePublication, ...] = ()
    rollback_publications: tuple[FramePublication, ...] = ()
    failure_deferred: bool = False


@dataclass(frozen=True)
class FrameLoadResult:
    request_id: int
    records: tuple[CoordinateFrameRecord, ...]


@dataclass(frozen=True)
class FramePublication:
    request_id: int
    records: tuple[CoordinateFrameRecord, ...]
    previous_record: CoordinateFrameRecord | None = None
    committed_record: CoordinateFrameRecord | None = None
    machine_b_deg: float | None = None
    pivot_machine_xy: tuple[float, float] | None = None
    success_message: str | None = None
    operator_alignment: bool = False
    registration_rollback_effects: RegistrationEffects | None = None


@dataclass(frozen=True)
class FramePublicationResult:
    request_id: int
    succeeded: bool


class CoordinateFrameLifecycle:
    """Own load generations and publication rollback bookkeeping."""

    def __init__(self) -> None:
        self._load_request_id: int | None = None
        self._publications: dict[int, FramePublication] = {}
        self._latest_publication_request_id: int | None = None
        self._resolved_publication_request_id: int | None = None

    def begin_load(self, request_id: int) -> FrameLifecycleEffects:
        self._load_request_id = int(request_id)
        return FrameLifecycleEffects(
            invalidate_session_reason=(
                "Design coordinate provenance is being checked."
            ),
            refresh_display=True,
        )

    def accept_load(self, result: FrameLoadResult) -> FrameLifecycleEffects:
        if int(result.request_id) != self._load_request_id:
            return FrameLifecycleEffects()
        self._load_request_id = None
        return FrameLifecycleEffects(
            replace_records=tuple(result.records),
            refresh_display=True,
        )

    def track_publication(self, publication: FramePublication) -> None:
        request_id = int(publication.request_id)
        resolved = self._resolved_publication_request_id
        if resolved is not None and request_id <= resolved:
            return
        self._publications[request_id] = publication
        latest = self._latest_publication_request_id
        if latest is None or request_id > latest:
            self._latest_publication_request_id = request_id

    def finish_publication(
        self,
        result: FramePublicationResult,
    ) -> FrameLifecycleEffects:
        request_id = int(result.request_id)
        latest = self._latest_publication_request_id
        if not result.succeeded and latest is not None and request_id < latest:
            return FrameLifecycleEffects(failure_deferred=True)

        completed = tuple(
            self._publications.pop(candidate_id)
            for candidate_id in sorted(tuple(self._publications))
            if candidate_id <= request_id
        )
        if completed:
            self._resolved_publication_request_id = max(
                publication.request_id for publication in completed
            )
        if result.succeeded:
            return FrameLifecycleEffects(
                acknowledged_publications=tuple(
                    publication
                    for publication in completed
                    if publication.committed_record is not None
                )
            )

        rollback_by_frame: dict[str, FramePublication] = {}
        for publication in completed:
            previous = publication.previous_record
            committed = publication.committed_record
            if previous is None or committed is None:
                continue
            chain = rollback_by_frame.get(committed.frame_id)
            if chain is not None:
                publication = replace(
                    publication,
                    previous_record=chain.previous_record,
                    operator_alignment=(
                        chain.operator_alignment or publication.operator_alignment
                    ),
                    registration_rollback_effects=(
                        chain.registration_rollback_effects
                        or publication.registration_rollback_effects
                    ),
                )
            rollback_by_frame[committed.frame_id] = publication
        rollback_publications = tuple(
            rollback_by_frame[frame_id]
            for frame_id in sorted(rollback_by_frame)
        )
        return FrameLifecycleEffects(
            refresh_display=bool(rollback_publications),
            rollback_publications=rollback_publications,
        )

__all__ = [
    "MACHINE_FRAME_ID",
    "CoordinateFrameLifecycle",
    "FrameLifecycleEffects",
    "FrameLoadResult",
    "FramePublication",
    "FramePublicationResult",
]
