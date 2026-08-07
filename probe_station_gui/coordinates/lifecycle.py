"""Pure lifecycle decisions for software coordinate-frame selection and use."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.model import DesignDocument, DesignModelError
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot

from .model import AxisReadiness, CoordinateFrameRecord
from .provenance import design_frame_provenance_error
from .transforms import BFrameTransform


MACHINE_FRAME_ID = "machine"


@dataclass(frozen=True)
class FrameSelectionContext:
    records: tuple[CoordinateFrameRecord, ...]
    requested_frame_id: str
    explicit: bool
    homed_axes: frozenset[str]
    authority_axes: frozenset[str]


@dataclass(frozen=True)
class FrameSelectionDecision:
    selected_frame_id: str
    available: bool
    reason: str | None
    persist_selection: bool


@dataclass(frozen=True)
class DesignUsabilityContext:
    frames_loaded: bool
    record: CoordinateFrameRecord | None
    selected_frame_id: str | None
    authority_blocked_axes: frozenset[str]
    pivot_machine_xy: tuple[float, float] | None
    document: DesignDocument | None = None
    machine_coordinate_snapshot: MachineCoordinateSnapshot | None = None
    objective_xy_offset: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class DesignFrameUsabilitySnapshot:
    load_complete: bool
    frame_id: str | None
    frame_version: int | None
    transform: BFrameTransform | None
    readiness: tuple[tuple[str, AxisReadiness], ...]
    provenance_error: str | None
    authority_blocked_axes: frozenset[str]
    rejection_reason: str | None
    document: DesignDocument | None
    metadata: DesignFrameMetadata | None
    machine_coordinate_snapshot: MachineCoordinateSnapshot | None
    pivot_machine_xy: tuple[float, float] | None
    objective_xy_offset: tuple[float, float]

    @property
    def usable(self) -> bool:
        return self.rejection_reason is None

    @property
    def reason(self) -> str | None:
        return self.rejection_reason

    def readiness_for(self, axis: str) -> AxisReadiness | None:
        normalized = str(axis).strip().upper()
        return dict(self.readiness).get(normalized)


def _normalized_axes(axes: frozenset[str]) -> frozenset[str]:
    return frozenset(str(axis).strip().upper() for axis in axes)


def _temporary_selection_reason(
    record: CoordinateFrameRecord,
    *,
    homed_axes: frozenset[str],
    authority_axes: frozenset[str],
) -> str | None:
    provenance_error = design_frame_provenance_error(record)
    if provenance_error is not None:
        return provenance_error
    if not {"X", "Y"}.issubset(homed_axes):
        return "Home X and Y to use this coordinate system."
    if "B" not in authority_axes:
        return "B coordinate is unavailable."
    return None


class CoordinateFrameLifecycle:
    """Own selection intent and Design-frame usability policy."""

    def __init__(self, *, selected_frame_id: str = MACHINE_FRAME_ID) -> None:
        self._selected_frame_id = str(selected_frame_id or MACHINE_FRAME_ID)

    def plan_selection(
        self,
        context: FrameSelectionContext,
    ) -> FrameSelectionDecision:
        records = {record.frame_id: record for record in context.records}
        requested = str(context.requested_frame_id or MACHINE_FRAME_ID)
        if requested == MACHINE_FRAME_ID:
            if context.explicit:
                self._selected_frame_id = MACHINE_FRAME_ID
            return FrameSelectionDecision(
                selected_frame_id=MACHINE_FRAME_ID,
                available=True,
                reason=None,
                persist_selection=bool(context.explicit),
            )

        record = records.get(requested)
        permanent_reason = (
            "Coordinate frame is unavailable."
            if record is None
            else record.semantic_validation_error()
        )
        if permanent_reason is not None:
            if context.explicit and requested != self._selected_frame_id:
                return FrameSelectionDecision(
                    selected_frame_id=self._selected_frame_id,
                    available=False,
                    reason=permanent_reason,
                    persist_selection=False,
                )
            persist = self._selected_frame_id != MACHINE_FRAME_ID
            self._selected_frame_id = MACHINE_FRAME_ID
            return FrameSelectionDecision(
                selected_frame_id=MACHINE_FRAME_ID,
                available=True,
                reason=None,
                persist_selection=persist,
            )

        assert record is not None
        reason = _temporary_selection_reason(
            record,
            homed_axes=_normalized_axes(context.homed_axes),
            authority_axes=_normalized_axes(context.authority_axes),
        )
        if context.explicit and reason is not None:
            return FrameSelectionDecision(
                selected_frame_id=self._selected_frame_id,
                available=False,
                reason=reason,
                persist_selection=False,
            )
        if context.explicit:
            self._selected_frame_id = requested
        return FrameSelectionDecision(
            selected_frame_id=requested,
            available=reason is None,
            reason=reason,
            persist_selection=bool(context.explicit),
        )

    def design_usability(
        self,
        context: DesignUsabilityContext,
    ) -> DesignFrameUsabilitySnapshot:
        load_complete = bool(context.frames_loaded)
        record = context.record
        frame_id = (
            None
            if context.selected_frame_id is None
            else str(context.selected_frame_id)
        )
        record_readiness = getattr(record, "readiness", {}) if record is not None else {}
        readiness = (
            ()
            if record is None
            else tuple(
                (axis, record_readiness[axis])
                for axis in ("X", "Y", "Z", "A", "B")
                if axis in record_readiness
            )
        )
        transform = None if record is None else getattr(record, "transform", None)
        record_version = None if record is None else getattr(record, "version", None)
        frame_version = None if record_version is None else int(record_version)
        provenance_error = (
            "Design coordinate provenance is being checked."
            if not load_complete
            else None if record is None else design_frame_provenance_error(record)
        )
        blocked_axes = _normalized_axes(context.authority_blocked_axes)
        reason: str | None = None
        metadata: DesignFrameMetadata | None = None
        machine_snapshot: MachineCoordinateSnapshot | None = None
        pivot: tuple[float, float] | None = None
        objective_offset = (0.0, 0.0)

        if not load_complete:
            reason = provenance_error
        elif frame_id is None or record is None:
            reason = "A durable Design coordinate frame is required."
        elif provenance_error is not None:
            reason = provenance_error
        elif transform is None:
            reason = "Design coordinate transform is unavailable."
        else:
            unavailable = [
                record_readiness[axis]
                for axis in ("X", "Y", "B")
                if axis in record_readiness and not record_readiness[axis].available
            ]
            if not all(axis in record_readiness for axis in ("X", "Y", "B")):
                reason = "Design X/Y/B readiness is unavailable."
            elif unavailable:
                reason = next(
                    (state.reason for state in unavailable if state.reason),
                    "Design X/Y/B registration is required.",
                )
            elif {"X", "Y", "B"}.intersection(blocked_axes):
                reason = "Controller coordinate authority is unavailable."
            elif context.document is None:
                reason = "Load a design before using Design coordinates."

        if reason is None:
            try:
                assert record is not None
                assert context.document is not None
                metadata = DesignFrameMetadata.from_mapping(record.metadata)
                document = context.document
                if (
                    Path(metadata.source_path).expanduser().resolve()
                    != document.path.expanduser().resolve()
                    or metadata.top_cell_name != document.top_cell_name
                ):
                    raise DesignModelError(
                        "Design coordinate frame belongs to a different document context."
                    )
                machine_snapshot = context.machine_coordinate_snapshot
                if machine_snapshot is None:
                    raise DesignModelError(
                        "A synchronized Machine-coordinate snapshot is unavailable."
                    )
                machine_snapshot.physical_machine_pose.require("B")
                if context.pivot_machine_xy is None:
                    raise DesignModelError("Rotation pivot is unavailable.")
                pivot = (
                    float(context.pivot_machine_xy[0]),
                    float(context.pivot_machine_xy[1]),
                )
                objective_offset = (
                    float(context.objective_xy_offset[0]),
                    float(context.objective_xy_offset[1]),
                )
                if not all(math.isfinite(value) for value in objective_offset):
                    raise DesignModelError("Active objective offset is invalid.")
            except Exception as exc:
                reason = str(exc) or type(exc).__name__

        return DesignFrameUsabilitySnapshot(
            load_complete=load_complete,
            frame_id=frame_id,
            frame_version=frame_version,
            transform=transform,
            readiness=readiness,
            provenance_error=provenance_error,
            authority_blocked_axes=blocked_axes,
            rejection_reason=reason,
            document=context.document,
            metadata=metadata,
            machine_coordinate_snapshot=machine_snapshot,
            pivot_machine_xy=pivot,
            objective_xy_offset=objective_offset,
        )


__all__ = [
    "MACHINE_FRAME_ID",
    "CoordinateFrameLifecycle",
    "DesignFrameUsabilitySnapshot",
    "DesignUsabilityContext",
    "FrameSelectionContext",
    "FrameSelectionDecision",
]
