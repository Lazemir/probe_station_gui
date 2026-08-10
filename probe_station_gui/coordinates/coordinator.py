"""Qt-free application boundary for Coordinate Systems."""

from __future__ import annotations

from typing import Protocol

from probe_station_gui.design.session import DesignSession

from .coordinator_model import (
    CoordinateAdapterCompletion,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    FrameRecordsPublication,
    MachineProfileObservation,
)
from .coordinator_persistence import (
    CoordinatePersistenceReducer,
    _ContactCommitRollback,
)
from .lifecycle import CoordinateFrameLifecycle
from .registry import CoordinateFrameRegistry


class _ContactCommitLifecycle(Protocol):
    def _release_contact_commit(
        self,
        *,
        session_identity: int,
        frame_id: str | None,
        frame_version: int | None,
    ) -> bool: ...


class CoordinateSystemCoordinator:
    """Coordinate-system workflows behind one immutable transition interface."""

    def __init__(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        lifecycle: CoordinateFrameLifecycle | None = None,
        registration_lifecycle: _ContactCommitLifecycle | None = None,
    ) -> None:
        self._registration_lifecycle = registration_lifecycle
        self._persistence = CoordinatePersistenceReducer(
            registry=registry,
            session=session,
            lifecycle=lifecycle,
        )

    def start(self, profile: MachineProfileObservation) -> CoordinateTransition:
        if not isinstance(profile, MachineProfileObservation):
            raise TypeError("profile must be a MachineProfileObservation")
        return self._persistence.start(profile)

    def complete(
        self,
        completion: CoordinateAdapterCompletion,
    ) -> CoordinateTransition:
        if not isinstance(completion, CoordinateAdapterCompletion):
            raise TypeError("completion must be a CoordinateAdapterCompletion")
        transition, publication_outcomes = self._persistence.complete(completion)
        lifecycle = self._registration_lifecycle
        if lifecycle is not None:
            for outcome in publication_outcomes:
                if isinstance(outcome, _ContactCommitRollback):
                    lifecycle._release_contact_commit(
                        session_identity=outcome.session_identity,
                        frame_id=outcome.frame_id,
                        frame_version=outcome.frame_version,
                    )
        return transition

    def publish_frame_records(
        self,
        publication: FrameRecordsPublication,
    ) -> CoordinateTransition:
        if not isinstance(publication, FrameRecordsPublication):
            raise TypeError("publication must be a FrameRecordsPublication")
        return self._persistence.publish(publication)

    def snapshot(self) -> CoordinateSystemSnapshot:
        return self._persistence.snapshot()


__all__ = ["CoordinateSystemCoordinator"]
