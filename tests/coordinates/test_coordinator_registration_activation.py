from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import probe_station_gui.coordinates.coordinator_model as coordinator_model
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    DesignSessionCheckpoint,
    FrameRecordsPublication,
    MachinePoseCaptureResult,
    RegistrationCaptureRequest,
    SaveCoordinateFramesIntent,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameStoreFailure,
    CoordinateFrameStoreSuccess,
)
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
    commit_xyb_registration,
)
from probe_station_gui.design.session import DesignSession
from tests.coordinates.coordinator_registration_support import (
    _document,
    _draft,
    _loaded_coordinator,
    _machine_snapshot,
)


def test_design_activation_adopts_candidate_state_without_replacing_session(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, None)
    candidate = DesignSession(document=document)
    candidate.registration_status = "Candidate state."
    identity = id(session)
    request_type = getattr(coordinator_model, "DesignActivationRequest")

    activated = coordinator.activate_design(
        request_type(
            session_state=candidate.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert id(session) == identity
    assert session.document is document
    assert session.active_frame_id is not None
    record = registry.get(session.active_frame_id)
    assert record is not None
    assert activated.snapshot.registration.active_frame_id == record.frame_id
    assert activated.snapshot.registration.registration_instances == (
        (record.frame_id, record.name),
    )
    assert len(activated.intents) == 1
    assert isinstance(activated.intents[0], SaveCoordinateFramesIntent)


def test_design_activation_uses_owned_session_when_adapter_omits_state(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, None)

    activated = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=None,
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert activated.accepted
    assert session.active_frame_id is not None
    assert registry.get(session.active_frame_id) is not None


def test_normal_activation_uses_preobserved_source_identity_without_resolve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, None)
    metadata = DesignFrameMetadata.from_document(document)

    with monkeypatch.context() as filesystem:
        filesystem.setattr(
            Path,
            "resolve",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("coordinator activation resolved a source path")
            ),
        )
        activated = coordinator.activate_design(
            coordinator_model.DesignActivationRequest(
                session_state=session.snapshot_state(),
                frame_metadata=metadata,
                machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
                pivot_machine_xy=(0.0, 0.0),
                objective_xy_offset=(0.0, 0.0),
            )
        )

    assert activated.accepted


def test_normal_activation_without_preobserved_metadata_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, None)

    with monkeypatch.context() as filesystem:
        filesystem.setattr(
            Path,
            "stat",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("coordinator activation inspected a source file")
            ),
        )
        filesystem.setattr(
            Path,
            "resolve",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("coordinator activation resolved a source path")
            ),
        )
        activated = coordinator.activate_design(
            coordinator_model.DesignActivationRequest(
                session_state=session.snapshot_state(),
                frame_metadata=None,
                machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
                pivot_machine_xy=(0.0, 0.0),
                objective_xy_offset=(0.0, 0.0),
            )
        )

    assert not activated.accepted
    assert activated.notices
    assert "metadata" in activated.notices[0].message.lower()


def test_legacy_missing_b_block_uses_preobserved_state_without_serializing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((3.0, 4.0), (4.0, 4.0))
    metadata = DesignFrameMetadata.from_document(document)
    monkeypatch.setattr(
        DesignSession,
        "export_persisted_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy block serialized on the coordinator thread")
        ),
    )

    activated = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=session.snapshot_state(),
            frame_metadata=metadata,
            machine_snapshot=None,
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert activated.accepted
    assert activated.intents == ()
    assert session.legacy_registration_waiting_for_b


def test_legacy_with_b_without_preobserved_metadata_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((3.0, 4.0), (4.0, 4.0))
    session._legacy_stage_coordinate_provenance = {
        "position_reporting_mode": "machine",
        "coordinate_system": None,
        "work_offset": [0.0] * 6,
    }
    session._legacy_stage_coordinate_provenance_present = True

    with monkeypatch.context() as filesystem:
        filesystem.setattr(
            Path,
            "stat",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy activation inspected a source file")
            ),
        )
        filesystem.setattr(
            Path,
            "resolve",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy activation resolved a source path")
            ),
        )
        activated = coordinator.activate_design(
            coordinator_model.DesignActivationRequest(
                session_state=session.snapshot_state(),
                frame_metadata=None,
                machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
                pivot_machine_xy=(0.0, 0.0),
                objective_xy_offset=(0.0, 0.0),
            )
        )

    assert not activated.accepted
    assert activated.notices
    assert "metadata" in activated.notices[0].message.lower()


def test_missing_active_frame_is_not_exposed_and_reconciles_to_existing_sibling(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    sibling = _draft(document)
    coordinator, registry, session = _loaded_coordinator(document, sibling)
    session.active_frame_id = "missing-frame"
    identity = id(session)

    assert coordinator.snapshot().registration.active_frame_id is None
    activated = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=session.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert activated.notices == ()
    assert id(session) == identity
    assert session.active_frame_id == sibling.frame_id
    assert registry.snapshot().records == (sibling,)


def test_unchanged_existing_frame_activation_explicitly_changes_design_view(
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

    activated = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=session.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert activated.accepted
    assert activated.intents == ()
    assert activated.view_changed
    assert registry.snapshot().records == (registered,)


def test_failed_activation_preparation_preserves_live_capture(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, _draft(document))
    capture = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    ).intents[0]
    baseline = session.snapshot_state()
    records = registry.snapshot().records

    rejected = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=baseline,
            frame_metadata=object(),
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert not rejected.accepted
    assert rejected.notices and rejected.notices[0].severity == "warning"
    assert session.snapshot_state() == baseline
    assert registry.snapshot().records == records
    completed = coordinator.complete(
        CoordinateAdapterCompletion(
            capture.intent_id,
            MachinePoseCaptureResult(
                capture.intent_id,
                True,
                _machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )
    assert [notice.code for notice in completed.notices] == [
        "registration_capture_updated"
    ]
    assert session.source_stage_marks_compact() == [(3.0, 4.0)]


def test_legacy_capture_records_exact_same_generation_wco_provenance(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    intent = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    ).intents[0]
    offset = (10.0, 20.0, 0.0, 0.0, 0.0, 0.0)

    coordinator.complete(
        CoordinateAdapterCompletion(
            intent.intent_id,
            MachinePoseCaptureResult(
                intent.intent_id,
                True,
                _machine_snapshot(13.0, 24.0, 0.0, work_offset=offset),
            ),
        )
    )

    persisted = session.export_persisted_state()
    assert persisted is not None
    assert persisted["stage_coordinate_provenance"] == {
        "position_reporting_mode": "work",
        "coordinate_system": "G54",
        "work_offset": list(offset),
    }


def test_legacy_activation_uses_capture_wco_with_current_calibration_mapper(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )
    captured_offset = (10.0, 20.0, 0.0, 0.0, 0.0, 0.0)
    for x_value in (13.0, 14.0):
        intent = coordinator.capture_registration_mark(request).intents[0]
        coordinator.complete(
            CoordinateAdapterCompletion(
                intent.intent_id,
                MachinePoseCaptureResult(
                    intent.intent_id,
                    True,
                    _machine_snapshot(
                        x_value,
                        24.0,
                        0.0,
                        work_offset=captured_offset,
                    ),
                ),
            )
        )
    current_offset = (100.0, 200.0, 0.0, 0.0, 0.0, 0.0)
    request_type = getattr(coordinator_model, "DesignActivationRequest")

    activated = coordinator.activate_design(
        request_type(
            session_state=session.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(
                101.0,
                202.0,
                0.0,
                work_offset=current_offset,
            ),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert len(activated.intents) == 1
    assert isinstance(activated.intents[0], SaveCoordinateFramesIntent)
    record = registry.get(session.active_frame_id)
    assert record is not None
    metadata = DesignFrameMetadata.from_mapping(record.metadata)
    assert metadata.source_machine_marks[0] == pytest.approx((13.0, 24.0))
    assert metadata.source_machine_marks[1] == pytest.approx((14.0, 24.0))


@pytest.mark.parametrize("retry_succeeds", (True, False))
def test_legacy_migration_finishes_only_after_typed_controller_state_ack(
    tmp_path: Path,
    retry_succeeds: bool,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((13.0, 24.0), (14.0, 24.0))
    session._legacy_stage_coordinate_provenance = {
        "position_reporting_mode": "work",
        "coordinate_system": "G54",
        "work_offset": [10.0, 20.0, 0.0, 0.0, 0.0, 0.0],
    }
    session._legacy_stage_coordinate_provenance_present = True
    legacy_state = session.export_persisted_state()
    assert legacy_state is not None
    rewrite_intent_type = getattr(
        coordinator_model,
        "RewriteLegacyDesignStateIntent",
    )
    rewrite_result_type = getattr(
        coordinator_model,
        "LegacyDesignStateRewriteResult",
    )

    activated = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=session.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(
                101.0,
                202.0,
                0.0,
                work_offset=(100.0, 200.0, 0.0, 0.0, 0.0, 0.0),
            ),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    save = activated.intents[0]
    migrated_state = session.export_persisted_state()
    assert migrated_state is not None

    frame_saved = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreSuccess(save.intent_id, "save"),
        )
    )

    assert frame_saved.notices == ()
    assert len(frame_saved.intents) == 1
    rewrite = frame_saved.intents[0]
    assert isinstance(rewrite, rewrite_intent_type)
    assert rewrite.persisted_design_state == migrated_state
    assert frame_saved.snapshot.registration.legacy_migration_state == legacy_state

    retrying = coordinator.complete(
        CoordinateAdapterCompletion(
            rewrite.intent_id,
            rewrite_result_type(
                rewrite.intent_id,
                succeeded=False,
                message="disk full",
            ),
        )
    )

    assert retrying.notices == ()
    assert len(retrying.intents) == 1
    retry = retrying.intents[0]
    assert isinstance(retry, rewrite_intent_type)
    assert retry.intent_id != rewrite.intent_id
    assert retry.persisted_design_state == migrated_state
    assert retrying.snapshot.registration.legacy_migration_state == legacy_state

    replayed = coordinator.complete(
        CoordinateAdapterCompletion(
            rewrite.intent_id,
            rewrite_result_type(
                rewrite.intent_id,
                succeeded=True,
            ),
        )
    )
    assert replayed.intents == ()
    assert replayed.notices == ()
    assert replayed.snapshot.registration.legacy_migration_state == legacy_state

    completed = coordinator.complete(
        CoordinateAdapterCompletion(
            retry.intent_id,
            rewrite_result_type(
                retry.intent_id,
                succeeded=retry_succeeds,
                message="still full" if not retry_succeeds else "",
            ),
        )
    )

    assert completed.intents == ()
    if retry_succeeds:
        assert [notice.code for notice in completed.notices] == [
            "legacy_migration_completed"
        ]
        assert completed.snapshot.registration.legacy_migration_state is None
    else:
        assert [notice.code for notice in completed.notices] == [
            "legacy_migration_rewrite_failed"
        ]
        assert completed.snapshot.registration.legacy_migration_state == legacy_state
        terminal_replay = coordinator.complete(
            CoordinateAdapterCompletion(
                retry.intent_id,
                rewrite_result_type(retry.intent_id, succeeded=True),
            )
        )
        assert terminal_replay.intents == ()
        assert terminal_replay.notices == ()
        assert (
            terminal_replay.snapshot.registration.legacy_migration_state
            == legacy_state
        )


def test_legacy_rewrite_rebases_on_route_saved_before_frame_ack(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((13.0, 24.0), (14.0, 24.0))
    session._legacy_stage_coordinate_provenance = {
        "position_reporting_mode": "work",
        "coordinate_system": "G54",
        "work_offset": [10.0, 20.0, 0.0, 0.0, 0.0, 0.0],
    }
    session._legacy_stage_coordinate_provenance_present = True

    activated = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=session.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(101.0, 202.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    save = activated.intents[0]
    migrated = registry.get(session.active_frame_id)
    assert migrated is not None
    newer = replace(
        migrated,
        name="newer same-frame registration",
        version=migrated.version + 1,
    )
    newer_projection = session.prepare_active_frame_link(
        newer,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    newer_save = coordinator._publish_frame_records(
        FrameRecordsPublication.for_committed_record(
            registry.snapshot().records,
            newer,
            previous_record=migrated,
            previous_session=DesignSessionCheckpoint.capture(session),
            projection=newer_projection,
        )
    ).intents[0]
    route = session.create_route(name="latest route")
    session.add_route_point((10.0, 20.0))
    session.add_route_point((30.0, 40.0))
    session.select_route_point(0)
    route.save(tmp_path / "latest-route.json")
    latest_state = session.export_persisted_state()
    assert latest_state is not None
    assert "route" in latest_state

    frame_saved = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreSuccess(save.intent_id, "save"),
        )
    )

    assert len(frame_saved.intents) == 1
    rewrite = frame_saved.intents[0]
    assert isinstance(rewrite, coordinator_model.RewriteLegacyDesignStateIntent)
    assert rewrite.persisted_design_state == latest_state
    assert rewrite.persisted_design_state["route"] == latest_state["route"]
    newer_saved = coordinator.complete(
        CoordinateAdapterCompletion(
            newer_save.intent_id,
            CoordinateFrameStoreSuccess(newer_save.intent_id, "save"),
        )
    )
    assert not any(
        isinstance(intent, coordinator_model.RewriteLegacyDesignStateIntent)
        for intent in newer_saved.intents
    )


@pytest.mark.parametrize("current_state", (None, {"version": 3}))
def test_unavailable_current_legacy_rewrite_state_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_state: object,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((13.0, 24.0), (14.0, 24.0))
    session._legacy_stage_coordinate_provenance = {
        "position_reporting_mode": "work",
        "coordinate_system": "G54",
        "work_offset": [10.0, 20.0, 0.0, 0.0, 0.0, 0.0],
    }
    session._legacy_stage_coordinate_provenance_present = True
    legacy_state = session.export_persisted_state()
    assert legacy_state is not None
    activated = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=session.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(101.0, 202.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    save = activated.intents[0]
    monkeypatch.setattr(
        coordinator._registration._activation,
        "_persisted_state",
        lambda *_args, **_kwargs: current_state,
    )

    frame_saved = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreSuccess(save.intent_id, "save"),
        )
    )

    assert frame_saved.intents == ()
    assert any(
        notice.code == "legacy_migration_rewrite_unavailable"
        for notice in frame_saved.notices
    )
    assert frame_saved.snapshot.registration.legacy_migration_state == legacy_state


def test_stale_legacy_save_ack_cannot_rewrite_a_newly_activated_design(
    tmp_path: Path,
) -> None:
    document_a = _document(tmp_path)
    document_b_path = tmp_path / "chip-b.gds"
    document_b_path.write_bytes(b"layout-b")
    document_b = replace(document_a, path=document_b_path)
    coordinator, _registry, session = _loaded_coordinator(document_a, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((13.0, 24.0), (14.0, 24.0))
    session._legacy_stage_coordinate_provenance = {
        "position_reporting_mode": "work",
        "coordinate_system": "G54",
        "work_offset": [10.0, 20.0, 0.0, 0.0, 0.0, 0.0],
    }
    session._legacy_stage_coordinate_provenance_present = True
    activated_a = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=session.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document_a),
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    save_a = activated_a.intents[0]
    frame_a = session.active_frame_id
    assert frame_a is not None

    candidate_b = DesignSession(document=document_b)
    activated_b = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=candidate_b.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document_b),
            machine_snapshot=_machine_snapshot(3.0, 4.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    assert activated_b.accepted
    assert len(activated_b.intents) == 1
    assert isinstance(activated_b.intents[0], SaveCoordinateFramesIntent)
    assert session.document is document_b
    assert session.active_frame_id is not None
    assert session.active_frame_id != frame_a

    stale_ack = coordinator.complete(
        CoordinateAdapterCompletion(
            save_a.intent_id,
            CoordinateFrameStoreSuccess(save_a.intent_id, "save"),
        )
    )

    assert stale_ack.intents == ()
    assert stale_ack.notices == ()
    assert stale_ack.snapshot.registration.legacy_migration_state is None
    assert session.document is document_b
    assert session.active_frame_id != frame_a


def test_old_save_failure_cannot_restore_session_over_newer_activation(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    frame_a = _draft(document)
    frame_b = _draft(document)
    assert frame_a.frame_id != frame_b.frame_id
    coordinator, registry, session = _loaded_coordinator(document, frame_a)
    add_sibling = coordinator._publish_frame_records(
        FrameRecordsPublication(records=(frame_a, frame_b))
    ).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            add_sibling.intent_id,
            CoordinateFrameStoreSuccess(add_sibling.intent_id, "save"),
        )
    )
    changed_a = replace(
        frame_a,
        name="pending A",
        version=frame_a.version + 1,
    )
    projection = session.prepare_active_frame_link(
        changed_a,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    pending = coordinator._publish_frame_records(
        FrameRecordsPublication.for_committed_record(
            registry.snapshot().records,
            changed_a,
            previous_record=frame_a,
            previous_session=DesignSessionCheckpoint.capture(session),
            projection=projection,
        )
    ).intents[0]

    activated_b = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=session.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(3.0, 4.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
            requested_frame_id=frame_b.frame_id,
        )
    )
    assert activated_b.accepted
    assert activated_b.intents == ()
    assert session.active_frame_id == frame_b.frame_id
    state_b = session.snapshot_state()

    failed_a = coordinator.complete(
        CoordinateAdapterCompletion(
            pending.intent_id,
            CoordinateFrameStoreFailure(
                pending.intent_id,
                "save",
                "disk full",
            ),
        )
    )

    assert failed_a.notices
    assert registry.snapshot().records == (frame_a, frame_b)
    assert session.snapshot_state() == state_b


def test_legacy_capture_under_different_wcos_fails_closed(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )
    for x_value, offset in (
        (13.0, (10.0, 20.0, 0.0, 0.0, 0.0, 0.0)),
        (24.0, (20.0, 20.0, 0.0, 0.0, 0.0, 0.0)),
    ):
        intent = coordinator.capture_registration_mark(request).intents[0]
        coordinator.complete(
            CoordinateAdapterCompletion(
                intent.intent_id,
                MachinePoseCaptureResult(
                    intent.intent_id,
                    True,
                    _machine_snapshot(x_value, 24.0, 0.0, work_offset=offset),
                ),
            )
        )
    request_type = getattr(coordinator_model, "DesignActivationRequest")

    blocked = coordinator.activate_design(
        request_type(
            session_state=session.snapshot_state(),
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert blocked.intents == ()
    assert blocked.notices and "capture-time" in blocked.notices[0].message
    assert registry.snapshot().records == ()
    assert session.active_frame_id is None


def test_legacy_activation_rejects_boolean_wco_without_mutating_live_state(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, None)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((3.0, 4.0), (4.0, 4.0))
    session._legacy_stage_coordinate_provenance = {
        "position_reporting_mode": "work",
        "coordinate_system": "G54",
        "work_offset": [True, 20.0, 0.0, 0.0, 0.0, 0.0],
    }
    session._legacy_stage_coordinate_provenance_present = True
    baseline = session.snapshot_state()

    rejected = coordinator.activate_design(
        coordinator_model.DesignActivationRequest(
            session_state=baseline,
            frame_metadata=DesignFrameMetadata.from_document(document),
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert not rejected.accepted
    assert rejected.intents == ()
    assert rejected.notices and "capture-time" in rejected.notices[0].message
    assert registry.snapshot().records == ()
    assert session.snapshot_state() == baseline
