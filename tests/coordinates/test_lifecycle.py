from __future__ import annotations

from dataclasses import replace

from probe_station_gui.coordinates import (
    MACHINE_FRAME_ID,
    CoordinateFrameLifecycle,
    DesignFrameUsabilitySnapshot,
    DesignUsabilityContext,
    FrameSelectionContext,
    FrameSelectionDecision,
)
from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
)
from probe_station_gui.coordinates.transforms import BFrameTransform


DESIGN_ID = "11111111-1111-4111-8111-111111111111"


def _readiness(*ready_axes: str) -> dict[str, AxisReadiness]:
    ready = set(ready_axes)
    return {
        axis: AxisReadiness(
            ReadinessStatus.READY if axis in ready else ReadinessStatus.MISSING,
            "" if axis in ready else f"Register {axis}.",
        )
        for axis in ("X", "Y", "Z", "A", "B")
    }


def _ready_design_record() -> CoordinateFrameRecord:
    return CoordinateFrameRecord(
        frame_id=DESIGN_ID,
        kind=FrameKind.DESIGN,
        name="chip-a",
        version=3,
        transform=BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=1.0,
            a_zero_machine_mm=2.0,
        ),
        readiness=_readiness("X", "Y", "Z", "A", "B"),
        metadata={},
    )


def _draft_design_record() -> CoordinateFrameRecord:
    record = _ready_design_record()
    return replace(
        record,
        transform=replace(
            record.transform,
            z_zero_machine_mm=None,
            a_zero_machine_mm=None,
        ),
        readiness={
            axis: AxisReadiness(ReadinessStatus.MISSING)
            for axis in ("X", "Y", "Z", "A", "B")
        },
    )


def test_selection_decision_separates_intent_from_temporary_usability() -> None:
    lifecycle = CoordinateFrameLifecycle(selected_frame_id=DESIGN_ID)

    decision = lifecycle.plan_selection(
        FrameSelectionContext(
            records=(_ready_design_record(),),
            requested_frame_id=DESIGN_ID,
            explicit=False,
            homed_axes=frozenset({"X", "Y"}),
            authority_axes=frozenset({"X", "Y", "Z", "A"}),
        )
    )

    assert isinstance(decision, FrameSelectionDecision)
    assert decision.selected_frame_id == DESIGN_ID
    assert decision.available is False
    assert decision.persist_selection is False


def test_usability_snapshot_requires_ready_current_durable_record() -> None:
    result = CoordinateFrameLifecycle().design_usability(
        DesignUsabilityContext(
            frames_loaded=True,
            record=_draft_design_record(),
            selected_frame_id=DESIGN_ID,
            authority_blocked_axes=frozenset(),
            pivot_machine_xy=(0.0, 0.0),
        )
    )

    assert isinstance(result, DesignFrameUsabilitySnapshot)
    assert result.usable is False
    assert result.rejection_reason is not None
    assert "registration" in result.rejection_reason.lower()


def test_permanent_semantic_rejection_falls_back_and_persists_machine() -> None:
    lifecycle = CoordinateFrameLifecycle(selected_frame_id=DESIGN_ID)
    corrupt = replace(_ready_design_record(), transform=BFrameTransform.identity())

    decision = lifecycle.plan_selection(
        FrameSelectionContext(
            records=(corrupt,),
            requested_frame_id=DESIGN_ID,
            explicit=False,
            homed_axes=frozenset({"X", "Y"}),
            authority_axes=frozenset({"X", "Y", "Z", "A", "B"}),
        )
    )

    assert decision.selected_frame_id == MACHINE_FRAME_ID
    assert decision.available is True
    assert decision.reason is None
    assert decision.persist_selection is True


def test_runtime_provenance_block_is_recoverable_selection_unavailability() -> None:
    lifecycle = CoordinateFrameLifecycle(selected_frame_id=DESIGN_ID)
    blocked = replace(
        _ready_design_record(),
        metadata={
            "_runtime_provenance_status": "blocked",
            "_runtime_provenance_reason": "Design source changed.",
        },
    )

    decision = lifecycle.plan_selection(
        FrameSelectionContext(
            records=(blocked,),
            requested_frame_id=DESIGN_ID,
            explicit=False,
            homed_axes=frozenset({"X", "Y"}),
            authority_axes=frozenset({"X", "Y", "Z", "A", "B"}),
        )
    )

    assert decision.selected_frame_id == DESIGN_ID
    assert decision.available is False
    assert decision.reason == "Design source changed."
    assert decision.persist_selection is False


def test_explicit_permanently_rejected_frame_keeps_previous_selection() -> None:
    lifecycle = CoordinateFrameLifecycle(selected_frame_id=MACHINE_FRAME_ID)
    corrupt = replace(_ready_design_record(), transform=BFrameTransform.identity())

    decision = lifecycle.plan_selection(
        FrameSelectionContext(
            records=(corrupt,),
            requested_frame_id=DESIGN_ID,
            explicit=True,
            homed_axes=frozenset({"X", "Y"}),
            authority_axes=frozenset({"X", "Y", "Z", "A", "B"}),
        )
    )

    assert decision.selected_frame_id == MACHINE_FRAME_ID
    assert decision.available is False
    assert decision.reason is not None
    assert "READY Z requires" in decision.reason
    assert decision.persist_selection is False
