from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import probe_station_gui.coordinates.coordinator_model as coordinator_model
from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    FinishOperatorAlignmentUiEffect,
    FrameRecordsPublication,
    MachinePoseCaptureResult,
    RegistrationCaptureRequest,
    RestoreOperatorAlignmentUiEffect,
    SaveCoordinateFramesIntent,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameStoreFailure,
    CoordinateFrameStoreSuccess,
)
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
    commit_xyb_registration,
)
from probe_station_gui.design.registration_lifecycle import RegistrationCancellation
from probe_station_gui.design.session import DesignSession
from tests.coordinates.coordinator_registration_support import (
    _ConversionFailureSnapshot,
    _ExplodingSnapshot,
    _document,
    _draft,
    _loaded_coordinator,
    _machine_snapshot,
)


@pytest.mark.parametrize("succeeded", (True, False))
def test_cancelled_capture_is_inert_before_snapshot_conversion(
    tmp_path: Path,
    succeeded: bool,
) -> None:
    document = _document(tmp_path)
    draft = _draft(document)
    registry = CoordinateFrameRegistry()
    registry.add(draft)
    session = DesignSession(document=document)
    session.active_frame_id = draft.frame_id
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    coordinator = CoordinateSystemCoordinator(registry=registry, session=session)
    request_type = getattr(coordinator_model, "RegistrationCaptureRequest")
    intent_type = getattr(coordinator_model, "CaptureMachinePoseIntent")
    result_type = getattr(coordinator_model, "MachinePoseCaptureResult")

    started = coordinator.capture_registration_mark(
        request_type(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    assert len(started.intents) == 1
    intent = started.intents[0]
    assert isinstance(intent, intent_type)
    assert intent.axes == ("X", "Y", "B")

    coordinator.cancel_registration(RegistrationCancellation.FRAME_CHANGED)
    stale = coordinator.complete(
        coordinator_model.CoordinateAdapterCompletion(
            intent.intent_id,
            result_type(
                intent_id=intent.intent_id,
                succeeded=succeeded,
                snapshot=_ExplodingSnapshot(),
            ),
        )
    )

    assert stale.intents == ()
    assert stale.notices == ()
    assert stale.snapshot.registration.source_stage_marks == ()


def test_stale_second_capture_restores_exact_pre_batch_baseline_before_conversion(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    draft = _draft(document)
    coordinator, registry, session = _loaded_coordinator(document, draft)
    baseline = session.snapshot_state()
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )
    first = coordinator.capture_registration_mark(request).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            first.intent_id,
            MachinePoseCaptureResult(
                first.intent_id,
                True,
                _machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )
    assert session.source_stage_marks_compact() == [(3.0, 4.0)]
    second = coordinator.capture_registration_mark(request).intents[0]
    bumped = replace(draft, version=draft.version + 1)
    coordinator.publish_frame_records(
        FrameRecordsPublication.for_committed_record(
            registry.snapshot().records,
            bumped,
            previous_record=draft,
        )
    )

    stale = coordinator.complete(
        CoordinateAdapterCompletion(
            second.intent_id,
            MachinePoseCaptureResult(
                second.intent_id,
                True,
                _ExplodingSnapshot(),
            ),
        )
    )

    assert stale.intents == ()
    assert stale.notices == ()
    assert session.snapshot_state() == baseline
    replayed = coordinator.complete(
        CoordinateAdapterCompletion(
            second.intent_id,
            MachinePoseCaptureResult(
                second.intent_id,
                True,
                _ExplodingSnapshot(),
            ),
        )
    )
    assert replayed.intents == ()
    assert session.snapshot_state() == baseline


def test_replacement_capture_restores_superseded_domain_evidence(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    draft = _draft(document)
    coordinator, _registry, session = _loaded_coordinator(document, draft)
    baseline = session.snapshot_state()
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )
    first = coordinator.capture_registration_mark(request).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            first.intent_id,
            MachinePoseCaptureResult(
                first.intent_id,
                True,
                _machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )
    assert session.source_stage_marks_compact() == [(3.0, 4.0)]
    bumped = replace(draft, version=draft.version + 1)
    coordinator.publish_frame_records(FrameRecordsPublication(records=(bumped,)))

    replacement = coordinator.capture_registration_mark(request)

    assert replacement.view_changed
    assert session.source_stage_marks == baseline.source_stage_marks
    current = replacement.intents[0]
    completed = coordinator.complete(
        CoordinateAdapterCompletion(
            current.intent_id,
            MachinePoseCaptureResult(
                current.intent_id,
                True,
                _machine_snapshot(5.0, 6.0, 0.0),
            ),
        )
    )
    assert completed.snapshot.registration.source_stage_marks == ((5.0, 6.0),)
    assert session.source_stage_marks_compact() == [(5.0, 6.0)]


def test_context_stale_capture_is_inert_before_snapshot_conversion(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    draft = _draft(document)
    coordinator, _registry, session = _loaded_coordinator(document, draft)
    started = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    intent = started.intents[0]
    session.source_design_marks = ((0.0, 0.0), (500.0, 0.0))
    stale_state = session.snapshot_state()

    stale = coordinator.complete(
        CoordinateAdapterCompletion(
            intent.intent_id,
            MachinePoseCaptureResult(
                intent.intent_id,
                succeeded=True,
                snapshot=_ExplodingSnapshot(),
            ),
        )
    )

    assert stale.intents == ()
    assert stale.notices == ()
    assert session.snapshot_state() == stale_state


def test_capture_conversion_failure_rolls_back_exact_baseline_and_warns(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, _draft(document))
    baseline = session.snapshot_state()
    record_before = registry.snapshot().records
    intent = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    ).intents[0]

    failed = coordinator.complete(
        CoordinateAdapterCompletion(
            intent.intent_id,
            MachinePoseCaptureResult(
                intent.intent_id,
                succeeded=True,
                snapshot=_ConversionFailureSnapshot(),
            ),
        )
    )

    assert failed.intents == ()
    assert failed.notices and failed.notices[0].severity == "warning"
    assert session.snapshot_state() == baseline
    assert registry.snapshot().records == record_before


def test_registration_fit_failure_restores_exact_capture_baseline(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, _draft(document))
    baseline = session.snapshot_state()
    record_before = registry.snapshot().records
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )

    first = coordinator.capture_registration_mark(request).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            first.intent_id,
            MachinePoseCaptureResult(
                first.intent_id,
                True,
                _machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )
    second = coordinator.capture_registration_mark(request).intents[0]
    failed = coordinator.complete(
        CoordinateAdapterCompletion(
            second.intent_id,
            MachinePoseCaptureResult(
                second.intent_id,
                True,
                _machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )

    assert failed.intents == ()
    assert failed.notices
    assert session.snapshot_state() == baseline
    assert registry.snapshot().records == record_before


def test_registration_projection_failure_restores_exact_capture_baseline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, _draft(document))
    baseline = session.snapshot_state()
    record_before = registry.snapshot().records
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )

    first = coordinator.capture_registration_mark(request).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            first.intent_id,
            MachinePoseCaptureResult(
                first.intent_id,
                True,
                _machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )
    monkeypatch.setattr(
        DesignSession,
        "prepare_active_frame_link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection")),
    )
    second = coordinator.capture_registration_mark(request).intents[0]
    failed = coordinator.complete(
        CoordinateAdapterCompletion(
            second.intent_id,
            MachinePoseCaptureResult(
                second.intent_id,
                True,
                _machine_snapshot(4.0, 4.0, 0.0),
            ),
        )
    )

    assert failed.intents == ()
    assert failed.notices and "projection" in failed.notices[0].message
    assert session.snapshot_state() == baseline
    assert registry.snapshot().records == record_before


def test_indexed_mixed_b_registration_commits_once_and_notices_after_save_ack(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, _session = _loaded_coordinator(
        document,
        _draft(document),
    )

    second_started = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(10.0, 20.0),
            objective_xy_offset=(0.25, -0.5),
            operator_alignment=True,
            mark_index=1,
            capture_source="image",
            operator_pick_generation=4,
        )
    )
    second_intent = second_started.intents[0]
    second = coordinator.complete(
        CoordinateAdapterCompletion(
            second_intent.intent_id,
            MachinePoseCaptureResult(
                second_intent.intent_id,
                True,
                _machine_snapshot(9.0, 20.0, 90.0),
                active_operator_pick_slot=1,
                active_operator_pick_generation=4,
            ),
        )
    )
    assert second.intents == ()

    first_started = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(1.0, 2.0),
            operator_alignment=True,
            mark_index=0,
            capture_source="center",
        )
    )
    first_intent = first_started.intents[0]
    committed = coordinator.complete(
        CoordinateAdapterCompletion(
            first_intent.intent_id,
            MachinePoseCaptureResult(
                first_intent.intent_id,
                True,
                _machine_snapshot(10.0, 20.0, 0.0),
            ),
        )
    )

    assert len(committed.intents) == 1
    save = committed.intents[0]
    assert isinstance(save, SaveCoordinateFramesIntent)
    assert [notice.code for notice in committed.notices] == [
        "registration_save_pending"
    ]
    assert committed.ui_effects == (FinishOperatorAlignmentUiEffect(),)
    current = registry.get(committed.snapshot.registration.active_frame_id)
    assert current is not None
    metadata = DesignFrameMetadata.from_mapping(current.metadata)
    assert metadata.source_machine_marks[0] == pytest.approx((10.0, 20.0))
    assert metadata.source_machine_marks[1] == pytest.approx((10.0, 21.0))

    acknowledged = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreSuccess(save.intent_id, "save"),
        )
    )
    assert [notice.code for notice in acknowledged.notices] == [
        "operator_alignment_saved"
    ]
    assert acknowledged.ui_effects == ()


def test_operator_alignment_save_failure_restores_exact_ui_draft_once(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, _draft(document))
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
        operator_alignment=True,
        mark_index=0,
    )
    first = coordinator.capture_registration_mark(request).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            first.intent_id,
            MachinePoseCaptureResult(
                first.intent_id,
                True,
                _machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )
    second = coordinator.capture_registration_mark(
        replace(request, mark_index=1)
    ).intents[0]
    committed = coordinator.complete(
        CoordinateAdapterCompletion(
            second.intent_id,
            MachinePoseCaptureResult(
                second.intent_id,
                True,
                _machine_snapshot(4.0, 4.0, 0.0),
            ),
        )
    )
    save = committed.intents[0]

    failed = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk full"),
        )
    )

    assert failed.ui_effects == (
        RestoreOperatorAlignmentUiEffect(
            design_marks=((0.0, 0.0), (1000.0, 0.0)),
            stage_marks=(),
        ),
    )
    assert session.source_stage_marks == ()
    replay = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "again"),
        )
    )
    assert replay.ui_effects == ()


def test_new_source_at_changed_b_rotates_existing_source_and_check_evidence(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    registered = commit_xyb_registration(
        _draft(document),
        design_points=((0.0, 0.0), (1.0, 0.0)),
        physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
        physical_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
        check_design_points=((0.0, 1.0),),
        check_machine_points=((1.0, 3.0),),
    )
    coordinator, registry, session = _loaded_coordinator(document, registered)
    session.link_active_frame(
        registered,
        machine_point_for_navigation=lambda point: point,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    session.set_source_design_mark(2, (1.0, 1.0))

    capture = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    ).intents[0]
    committed = coordinator.complete(
        CoordinateAdapterCompletion(
            capture.intent_id,
            MachinePoseCaptureResult(
                capture.intent_id,
                True,
                _machine_snapshot(-3.0, 2.0, 90.0),
            ),
        )
    )

    assert len(committed.intents) == 1
    assert isinstance(committed.intents[0], SaveCoordinateFramesIntent)
    current = registry.get(registered.frame_id)
    assert current is not None and current.transform is not None
    metadata = DesignFrameMetadata.from_mapping(current.metadata)
    assert current.transform.reference_b_deg == pytest.approx(90.0)
    assert len(metadata.source_machine_marks) == 3
    for actual, expected in zip(
        metadata.source_machine_marks,
        ((-2.0, 1.0), (-2.0, 2.0), (-3.0, 2.0)),
        strict=True,
    ):
        assert actual == pytest.approx(expected)
    assert len(metadata.check_machine_marks) == 1
    assert metadata.check_machine_marks[0] == pytest.approx((-3.0, 1.0))
    assert metadata.rms_residual_mm == pytest.approx(0.0, abs=1e-12)
    assert metadata.max_residual_mm == pytest.approx(0.0, abs=1e-12)


def test_check_only_capture_preserves_transform_source_and_readiness(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    registered = commit_xyb_registration(
        _draft(document),
        design_points=((0.0, 0.0), (1.0, 0.0)),
        physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
        physical_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    coordinator, registry, session = _loaded_coordinator(document, registered)
    session.link_active_frame(
        registered,
        machine_point_for_navigation=lambda point: point,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    session.check_design_marks.append((1.0, 1.0))
    capture = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
            check_mark=True,
        )
    ).intents[0]

    committed = coordinator.complete(
        CoordinateAdapterCompletion(
            capture.intent_id,
            MachinePoseCaptureResult(
                capture.intent_id,
                True,
                _machine_snapshot(-3.0, 2.0, 90.0),
            ),
        )
    )

    assert len(committed.intents) == 1
    current = registry.get(registered.frame_id)
    assert current is not None
    assert current.transform == registered.transform
    assert current.readiness == registered.readiness
    metadata = DesignFrameMetadata.from_mapping(current.metadata)
    original = DesignFrameMetadata.from_mapping(registered.metadata)
    assert metadata.source_design_marks == original.source_design_marks
    assert metadata.source_machine_marks == original.source_machine_marks
    assert metadata.check_design_marks == ((1.0, 1.0),)
    assert metadata.check_machine_marks[0] == pytest.approx((2.0, 3.0))


def test_registration_save_failure_restores_exact_baseline_once(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    draft = _draft(document)
    coordinator, registry, session = _loaded_coordinator(document, draft)
    baseline = session.snapshot_state()
    records = registry.snapshot().records
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )
    first = coordinator.capture_registration_mark(request).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            first.intent_id,
            MachinePoseCaptureResult(
                first.intent_id,
                True,
                _machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )
    second = coordinator.capture_registration_mark(request).intents[0]
    publication = coordinator.complete(
        CoordinateAdapterCompletion(
            second.intent_id,
            MachinePoseCaptureResult(
                second.intent_id,
                True,
                _machine_snapshot(4.0, 4.0, 0.0),
            ),
        )
    )
    save = publication.intents[0]

    failed = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk full"),
        )
    )

    assert failed.notices
    assert registry.snapshot().records == records
    assert session.snapshot_state() == baseline
    session.registration_status = "after exact rollback"
    replay = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk full again"),
        )
    )
    assert replay.notices == ()
    assert session.registration_status == "after exact rollback"


def test_operator_capture_releases_only_the_exact_armed_pick_generation(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, _session = _loaded_coordinator(
        document,
        _draft(document),
    )
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
        operator_alignment=True,
        mark_index=0,
        operator_pick_generation=4,
    )
    stale_pick_intent = coordinator.capture_registration_mark(request).intents[0]

    stale_pick = coordinator.complete(
        CoordinateAdapterCompletion(
            stale_pick_intent.intent_id,
            MachinePoseCaptureResult(
                stale_pick_intent.intent_id,
                True,
                _machine_snapshot(1.0, 2.0, 0.0),
                active_operator_pick_slot=0,
                active_operator_pick_generation=5,
            ),
        )
    )
    assert stale_pick.snapshot.registration.operator_pick_release is None

    current_intent = coordinator.capture_registration_mark(
        replace(request, mark_index=1, operator_pick_generation=6)
    ).intents[0]
    current_pick = coordinator.complete(
        CoordinateAdapterCompletion(
            current_intent.intent_id,
            MachinePoseCaptureResult(
                current_intent.intent_id,
                True,
                _machine_snapshot(2.0, 2.0, 0.0),
                active_operator_pick_slot=1,
                active_operator_pick_generation=6,
            ),
        )
    )
    release = current_pick.snapshot.registration.operator_pick_release
    assert release is not None
    assert (release.slot, release.generation) == (1, 6)
