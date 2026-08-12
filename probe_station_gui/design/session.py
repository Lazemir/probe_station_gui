"""Aggregate mutable state identity for optional Design navigation workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from probe_station_gui.design import session_state

if TYPE_CHECKING:
    from probe_station_gui.design.model import (
        DesignDocument,
        MeasurementTarget,
        Point2D,
    )
    from probe_station_gui.design.rigid_registration import DesignRegistration
    from probe_station_gui.route.model import MeasurementRoute


@dataclass
class DesignSession:
    """Retain one stable identity for the adopted Design workspace state."""

    document: DesignDocument | None = None
    registration: DesignRegistration | None = None
    source_design_marks: tuple[Point2D, ...] = ()
    source_stage_marks: tuple[Point2D, ...] = ()
    check_design_marks: list[Point2D] = field(default_factory=list)
    check_stage_marks: list[Point2D] = field(default_factory=list)
    targets: list[MeasurementTarget] = field(default_factory=list)
    selected_target_index: int = -1
    route: MeasurementRoute | None = None
    selected_route_point_index: int = -1
    registration_status: str = "No design registration."
    active_frame_id: str | None = None
    _runtime_blocked_persisted_state: dict[str, object] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _legacy_stage_coordinate_provenance: object = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _legacy_stage_coordinate_provenance_present: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def snapshot_state(self) -> session_state.DesignSessionState:
        """Return a detached snapshot without replacing this session."""

        return session_state.snapshot_session_state(self)

    def apply_state(self, state: session_state.DesignSessionState) -> None:
        """Replace owned state atomically while retaining this session's identity."""

        session_state.restore_session_state(self, state)


__all__ = ["DesignSession"]
