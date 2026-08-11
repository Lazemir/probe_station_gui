from __future__ import annotations

from dataclasses import replace
import pytest

from probe_station_gui.coordinates.lifecycle import (
    CoordinateFrameLifecycle,
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
from probe_station_gui.design.registration_lifecycle import RegistrationEffects


DESIGN_ID = "11111111-1111-4111-8111-111111111111"
SECOND_DESIGN_ID = "22222222-2222-4222-8222-222222222222"


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


def _record_version(
    version: int,
    *,
    frame_id: str = DESIGN_ID,
) -> CoordinateFrameRecord:
    return replace(_ready_design_record(), frame_id=frame_id, version=version)


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
    frame_id: str = DESIGN_ID,
) -> FramePublication:
    previous = _record_version(before, frame_id=frame_id)
    committed = _record_version(after, frame_id=frame_id)
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

    assert len(effects.rollback_publications) == 1
    assert effects.rollback_publications[0].previous_record is not None
    assert effects.rollback_publications[0].previous_record.version == 0


def test_failed_document_returns_every_frame_rollback_in_frame_id_order() -> None:
    lifecycle = CoordinateFrameLifecycle()
    lifecycle.track_publication(
        _registration_publication(
            41,
            before=0,
            after=1,
            frame_id=SECOND_DESIGN_ID,
        )
    )
    lifecycle.track_publication(
        _registration_publication(42, before=0, after=1)
    )
    lifecycle.track_publication(_ordinary_publication(43, version=2))
    latest_second = _registration_publication(
        44,
        before=1,
        after=2,
        frame_id=SECOND_DESIGN_ID,
    )
    latest_first = _registration_publication(45, before=1, after=2)
    lifecycle.track_publication(latest_second)
    lifecycle.track_publication(latest_first)

    effects = lifecycle.finish_publication(FramePublicationResult(45, False))

    assert effects.rollback_publications == (
        replace(latest_first, previous_record=_record_version(0)),
        replace(
            latest_second,
            previous_record=_record_version(0, frame_id=SECOND_DESIGN_ID),
        ),
    )


def test_older_failure_defers_while_newer_publication_is_pending() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    lifecycle.track_publication(
        _registration_publication(41, before=0, after=1)
    )
    lifecycle.track_publication(_ordinary_publication(42, version=2))

    deferred = lifecycle.finish_publication(FramePublicationResult(41, False))
    assert deferred.rollback_publications == ()
    assert deferred.failure_deferred is True
    assert lifecycle.finish_publication(
        FramePublicationResult(42, True)
    ).rollback_publications == ()


def test_deferred_older_failure_rolls_back_if_newest_publication_fails() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    lifecycle.track_publication(
        _registration_publication(41, before=0, after=1)
    )
    lifecycle.track_publication(_ordinary_publication(42, version=2))

    lifecycle.finish_publication(FramePublicationResult(41, False))
    effects = lifecycle.finish_publication(FramePublicationResult(42, False))

    assert len(effects.rollback_publications) == 1
    assert effects.rollback_publications[0].previous_record is not None
    assert effects.rollback_publications[0].previous_record.version == 0


def test_deferred_failure_returns_exact_registration_rollback_once() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    rollback = RegistrationEffects(
        restore_baseline=True,
        baseline_session_identity=17,
        baseline_frame_id=DESIGN_ID,
        baseline_source_stage_marks=((8.0, 9.0),),
        baseline_check_stage_marks=((10.0, 11.0),),
        baseline_registration_status="Exact durable baseline.",
    )
    lifecycle.track_publication(
        replace(
            _registration_publication(41, before=0, after=1),
            registration_rollback_effects=rollback,
        )
    )
    lifecycle.track_publication(_ordinary_publication(42, version=2))

    deferred = lifecycle.finish_publication(FramePublicationResult(41, False))
    terminal = lifecycle.finish_publication(FramePublicationResult(42, False))
    repeated = lifecycle.finish_publication(FramePublicationResult(42, False))

    assert deferred.failure_deferred is True
    assert deferred.rollback_publications == ()
    assert len(terminal.rollback_publications) == 1
    assert (
        terminal.rollback_publications[0].registration_rollback_effects
        is rollback
    )
    assert repeated.rollback_publications == ()


def test_newer_success_acknowledges_coalesced_registration_publications() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    registration = _registration_publication(41, before=0, after=1)
    lifecycle.track_publication(registration)
    lifecycle.track_publication(_ordinary_publication(42, version=2))

    effects = lifecycle.finish_publication(FramePublicationResult(42, True))

    assert effects.acknowledged_publications == (registration,)


def test_success_acknowledges_then_discards_exact_registration_rollback() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    rollback = RegistrationEffects(
        restore_baseline=True,
        baseline_session_identity=17,
        baseline_frame_id=DESIGN_ID,
    )
    registration = replace(
        _registration_publication(41, before=0, after=1),
        registration_rollback_effects=rollback,
    )
    lifecycle.track_publication(registration)

    acknowledged = lifecycle.finish_publication(FramePublicationResult(41, True))
    repeated = lifecycle.finish_publication(FramePublicationResult(41, False))

    assert acknowledged.acknowledged_publications == (registration,)
    assert repeated.rollback_publications == ()


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

    assert effects.rollback_publications == (
        replace(
            latest,
            previous_record=_record_version(0),
            operator_alignment=True,
        ),
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
