from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from probe_station_gui.coordinates import (
    MACHINE_FRAME_ID,
    CoordinateFrameLifecycle,
    DesignFrameUsabilitySnapshot,
    DesignUsabilityContext,
    FrameSelectionContext,
    FrameSelectionDecision,
)
from probe_station_gui.coordinates.lifecycle import (
    FrameLifecycleEffects,
    FrameLoadResult,
    FramePublication,
    FramePublicationResult,
)
from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
)
from probe_station_gui.coordinates.transforms import BFrameTransform
from probe_station_gui.design.model import DesignDocument


DESIGN_ID = "11111111-1111-4111-8111-111111111111"


def _design_document() -> DesignDocument:
    return DesignDocument(
        path=Path("chip-a.gds"),
        library=object(),
        top_cell=object(),
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=0.001,
        user_unit=1.0,
        bounds=(0.0, 0.0, 1.0, 1.0),
        polygons_by_layer={},
        visible_layers=frozenset(),
    )


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


def _record_version(version: int) -> CoordinateFrameRecord:
    return replace(_ready_design_record(), version=version)


def _lifecycle_with_durable_v0() -> CoordinateFrameLifecycle:
    lifecycle = CoordinateFrameLifecycle()
    lifecycle.begin_load(1)
    lifecycle.accept_load(FrameLoadResult(1, (_record_version(0),)))
    return lifecycle


def _registration_publication(
    request_id: int,
    *,
    before: int,
    after: int,
) -> FramePublication:
    previous = _record_version(before)
    committed = _record_version(after)
    return FramePublication(
        request_id=request_id,
        records=(committed,),
        previous_record=previous,
        committed_record=committed,
        machine_b_deg=5.0,
        pivot_machine_xy=(0.0, 0.0),
        success_message=f"registration {after} saved",
    )


def _ordinary_publication(request_id: int, *, version: int) -> FramePublication:
    return FramePublication(
        request_id=request_id,
        records=(_record_version(version),),
    )


def _track_registration_and_intervening_publications(
    lifecycle: CoordinateFrameLifecycle,
    intervening: str,
) -> None:
    lifecycle.track_publication(
        _registration_publication(41, before=0, after=1)
    )
    if intervening == "none":
        lifecycle.track_publication(
            _registration_publication(43, before=1, after=2)
        )
    elif intervening == "focus":
        lifecycle.track_publication(_ordinary_publication(42, version=2))
        lifecycle.track_publication(
            _registration_publication(43, before=2, after=3)
        )
    elif intervening == "trailing_focus":
        lifecycle.track_publication(_ordinary_publication(43, version=2))
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"Unknown publication sequence {intervening!r}")


def test_begin_load_invalidates_the_active_session_while_provenance_is_pending() -> None:
    effects = CoordinateFrameLifecycle().begin_load(10)

    assert effects.invalidate_session_reason == (
        "Design coordinate provenance is being checked."
    )


def test_only_current_load_result_produces_registry_replacement() -> None:
    lifecycle = CoordinateFrameLifecycle()
    lifecycle.begin_load(10)
    lifecycle.begin_load(11)

    stale = lifecycle.accept_load(
        FrameLoadResult(request_id=10, records=(_ready_design_record(),))
    )
    current = lifecycle.accept_load(
        FrameLoadResult(request_id=11, records=(_ready_design_record(),))
    )

    assert stale == FrameLifecycleEffects()
    assert current.replace_records == (_ready_design_record(),)


@pytest.mark.parametrize("intervening", ["none", "focus", "trailing_focus"])
def test_failed_coalesced_registration_chain_rolls_to_earliest_predecessor(
    intervening: str,
) -> None:
    lifecycle = _lifecycle_with_durable_v0()
    _track_registration_and_intervening_publications(lifecycle, intervening)

    effects = lifecycle.finish_publication(
        FramePublicationResult(request_id=43, succeeded=False)
    )

    assert effects.rollback_record is not None
    assert effects.rollback_record.version == 0


def test_older_failure_defers_while_newer_publication_is_pending() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    lifecycle.track_publication(
        _registration_publication(41, before=0, after=1)
    )
    lifecycle.track_publication(_ordinary_publication(42, version=2))

    deferred = lifecycle.finish_publication(FramePublicationResult(41, False))
    assert deferred.rollback_record is None
    assert deferred.failure_deferred is True
    assert lifecycle.finish_publication(
        FramePublicationResult(42, True)
    ).rollback_record is None


def test_deferred_older_failure_rolls_back_if_newest_publication_fails() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    lifecycle.track_publication(
        _registration_publication(41, before=0, after=1)
    )
    lifecycle.track_publication(_ordinary_publication(42, version=2))

    lifecycle.finish_publication(FramePublicationResult(41, False))
    effects = lifecycle.finish_publication(FramePublicationResult(42, False))

    assert effects.rollback_record is not None
    assert effects.rollback_record.version == 0


def test_newer_success_acknowledges_coalesced_registration_publications() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    registration = _registration_publication(41, before=0, after=1)
    lifecycle.track_publication(registration)
    lifecycle.track_publication(_ordinary_publication(42, version=2))

    effects = lifecycle.finish_publication(FramePublicationResult(42, True))

    assert effects.acknowledged_publications == (registration,)


def test_failed_chain_returns_latest_registration_context_with_earliest_record() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    first = replace(
        _registration_publication(41, before=0, after=1),
        operator_alignment=True,
    )
    latest = _registration_publication(43, before=2, after=3)
    lifecycle.track_publication(first)
    lifecycle.track_publication(_ordinary_publication(42, version=2))
    lifecycle.track_publication(latest)

    effects = lifecycle.finish_publication(FramePublicationResult(43, False))

    assert effects.rollback_publication == replace(
        latest,
        previous_record=_record_version(0),
        operator_alignment=True,
    )


def test_late_registration_enrichment_cannot_reopen_finished_publication() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    lifecycle.track_publication(_ordinary_publication(41, version=1))
    lifecycle.finish_publication(FramePublicationResult(41, True))

    lifecycle.track_publication(
        _registration_publication(41, before=0, after=1)
    )
    lifecycle.track_publication(_ordinary_publication(42, version=2))
    effects = lifecycle.finish_publication(FramePublicationResult(42, True))

    assert effects.acknowledged_publications == ()


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
            document=_design_document(),
        )
    )

    assert isinstance(result, DesignFrameUsabilitySnapshot)
    assert result.usable is False
    assert result.rejection_reason is not None
    assert "registration" in result.rejection_reason.lower()


def test_missing_document_precedes_transform_readiness_and_authority_reasons() -> None:
    contexts = (
        DesignUsabilityContext(
            frames_loaded=True,
            record=replace(_ready_design_record(), transform=None),
            selected_frame_id=DESIGN_ID,
            authority_blocked_axes=frozenset(),
            pivot_machine_xy=(0.0, 0.0),
        ),
        DesignUsabilityContext(
            frames_loaded=True,
            record=_draft_design_record(),
            selected_frame_id=DESIGN_ID,
            authority_blocked_axes=frozenset(),
            pivot_machine_xy=(0.0, 0.0),
        ),
        DesignUsabilityContext(
            frames_loaded=True,
            record=_ready_design_record(),
            selected_frame_id=DESIGN_ID,
            authority_blocked_axes=frozenset({"X", "Y", "B"}),
            pivot_machine_xy=(0.0, 0.0),
        ),
    )

    for context in contexts:
        result = CoordinateFrameLifecycle().design_usability(context)
        assert result.rejection_reason == (
            "Load a design before using Design coordinates."
        )


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


def test_pending_restore_waits_without_changing_machine_intent() -> None:
    lifecycle = CoordinateFrameLifecycle()

    decision = lifecycle.plan_selection(
        FrameSelectionContext(
            records=(_ready_design_record(),),
            requested_frame_id=DESIGN_ID,
            explicit=False,
            homed_axes=frozenset({"X", "Y"}),
            authority_axes=frozenset({"X", "Y", "Z", "A"}),
        )
    )

    assert decision.selected_frame_id == MACHINE_FRAME_ID
    assert decision.available is False
    assert decision.persist_selection is False


def test_pending_restore_rejects_unverified_provenance_to_machine() -> None:
    lifecycle = CoordinateFrameLifecycle()
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

    assert decision.selected_frame_id == MACHINE_FRAME_ID
    assert decision.available is True
    assert decision.persist_selection is False


def test_pending_restore_rejects_missing_registration_to_machine() -> None:
    lifecycle = CoordinateFrameLifecycle()

    decision = lifecycle.plan_selection(
        FrameSelectionContext(
            records=(_draft_design_record(),),
            requested_frame_id=DESIGN_ID,
            explicit=False,
            homed_axes=frozenset({"X", "Y"}),
            authority_axes=frozenset({"X", "Y", "Z", "A", "B"}),
        )
    )

    assert decision.selected_frame_id == MACHINE_FRAME_ID
    assert decision.available is True
    assert decision.persist_selection is False
