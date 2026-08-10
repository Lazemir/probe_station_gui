from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    CoordinateNotice,
    DesignSessionCheckpoint,
    DesignSessionFrameLink,
    FrameRecordsPublication,
    LoadCoordinateFramesIntent,
    MachineProfileObservation,
    SaveCoordinateFramesIntent,
)
from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    ReadinessStatus,
    VISIBLE_STAGE_AXES,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameLoadResult,
    CoordinateFrameStoreFailure,
    CoordinateFrameStoreSuccess,
)
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.coordinates.transforms import BFrameTransform
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.session import DesignFrameLinkProjection, DesignSession


_FRAME_A = "00000000-0000-0000-0000-00000000000a"
_FRAME_B = "00000000-0000-0000-0000-00000000000b"


def _document(tmp_path: Path) -> DesignDocument:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"layout")
    return DesignDocument(
        path=source,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-3,
        user_unit=1e-6,
        polygons_by_layer={},
        visible_layers=frozenset({(1, 0)}),
        bounds=(0.0, 0.0, 1000.0, 1000.0),
    )


def _record(document: DesignDocument, frame_id: str, name: str) -> CoordinateFrameRecord:
    return CoordinateFrameRecord.create_design(
        frame_id=frame_id,
        name=name,
        transform=BFrameTransform.identity(),
        readiness={
            axis: AxisReadiness(ReadinessStatus.MISSING, f"{axis} missing")
            for axis in VISIBLE_STAGE_AXES
        },
        metadata=DesignFrameMetadata.from_document(document).to_dict(),
    )


def _coordinator(
    document: DesignDocument,
) -> tuple[CoordinateSystemCoordinator, CoordinateFrameRegistry, DesignSession]:
    registry = CoordinateFrameRegistry()
    session = DesignSession(document=document)
    return CoordinateSystemCoordinator(registry=registry, session=session), registry, session


def _complete_load(
    coordinator: CoordinateSystemCoordinator,
    intent: LoadCoordinateFramesIntent,
    document: CoordinateFrameDocument,
) -> None:
    coordinator.complete(
        CoordinateAdapterCompletion(
            intent_id=intent.intent_id,
            result=CoordinateFrameLoadResult(
                request_id=intent.intent_id,
                document=document,
            ),
        )
    )


def _save_intent(transition: object) -> SaveCoordinateFramesIntent:
    intents = getattr(transition, "intents")
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, SaveCoordinateFramesIntent)
    return intent


def _link(record: CoordinateFrameRecord) -> DesignSessionFrameLink:
    return DesignSessionFrameLink(
        frame_id=record.frame_id,
        projection=DesignFrameLinkProjection(
            frame_id=record.frame_id,
            source_design_marks=(),
            source_stage_marks=(),
            check_design_marks=(),
            check_stage_marks=(),
        ),
    )


def test_completion_requires_exact_qt_free_store_result_types(tmp_path: Path) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    committed = replace(draft, name="committed", version=1)
    coordinator, registry, _session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)

    arbitrary_load = coordinator.complete(
        CoordinateAdapterCompletion(
            load.intent_id,
            SimpleNamespace(
                request_id=load.intent_id,
                document=CoordinateFrameDocument(records=(draft,)),
                runtime_records=None,
            ),
        )
    )
    assert arbitrary_load.snapshot.frames_loaded is False
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))
    save = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(committed,),
                previous_record=draft,
                committed_record=committed,
            )
        )
    )

    arbitrary_save = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            SimpleNamespace(request_id=save.intent_id, operation="save"),
        )
    )
    assert arbitrary_save.notices == ()
    coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk full"),
        )
    )
    assert registry.get(draft.frame_id) == draft


def test_coordinator_import_is_qt_and_store_worker_free() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys; import probe_station_gui.coordinates.coordinator; "
            "print('PySide6' in sys.modules); "
            "print('probe_station_gui.coordinates.persistence' in sys.modules)",
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["False", "False"]


def test_start_returns_load_intent_and_pending_snapshot(tmp_path: Path) -> None:
    document = _document(tmp_path)
    coordinator, _registry, _session = _coordinator(document)

    transition = coordinator.start(MachineProfileObservation("profile-a"))

    assert transition.snapshot.frames_loaded is False
    assert transition.snapshot.records == ()
    assert transition.intents == (LoadCoordinateFramesIntent(1, "profile-a"),)


def test_coordinator_has_one_public_publication_input() -> None:
    public_methods = {
        name
        for name, value in vars(CoordinateSystemCoordinator).items()
        if not name.startswith("_") and callable(value)
    }

    assert public_methods == {
        "complete",
        "publish_frame_records",
        "snapshot",
        "start",
    }


def test_only_current_load_completion_can_install_records(tmp_path: Path) -> None:
    document = _document(tmp_path)
    first = _record(document, _FRAME_A, "first")
    second = _record(document, _FRAME_B, "second")
    coordinator, registry, _session = _coordinator(document)
    stale = coordinator.start(MachineProfileObservation("first")).intents[0]
    current = coordinator.start(MachineProfileObservation("second")).intents[0]
    assert isinstance(stale, LoadCoordinateFramesIntent)
    assert isinstance(current, LoadCoordinateFramesIntent)

    stale_transition = coordinator.complete(
        CoordinateAdapterCompletion(
            stale.intent_id,
            CoordinateFrameLoadResult(
                stale.intent_id,
                CoordinateFrameDocument(records=(first,)),
            ),
        )
    )
    current_transition = coordinator.complete(
        CoordinateAdapterCompletion(
            current.intent_id,
            CoordinateFrameLoadResult(
                current.intent_id,
                CoordinateFrameDocument(records=(second,)),
            ),
        )
    )

    assert stale_transition.intents == ()
    assert stale_transition.notices == ()
    assert stale_transition.snapshot.frames_loaded is False
    assert current_transition.snapshot.frames_loaded is True
    assert current_transition.snapshot.records == (second,)
    assert registry.snapshot().records == (second,)


def test_load_uses_runtime_records_but_retains_durable_document(tmp_path: Path) -> None:
    design = _document(tmp_path)
    durable = _record(design, _FRAME_A, "durable")
    runtime = replace(durable, name="runtime")
    coordinator, registry, _session = _coordinator(design)
    intent = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(intent, LoadCoordinateFramesIntent)

    transition = coordinator.complete(
        CoordinateAdapterCompletion(
            intent.intent_id,
            CoordinateFrameLoadResult(
                intent.intent_id,
                CoordinateFrameDocument(records=(durable,)),
                runtime_records=(runtime,),
            ),
        )
    )

    assert transition.snapshot.records == (runtime,)
    assert transition.snapshot.document == CoordinateFrameDocument(records=(durable,))
    assert registry.snapshot().records == (runtime,)


def test_publication_preserves_rejected_raw_document_slots(tmp_path: Path) -> None:
    design = _document(tmp_path)
    record = _record(design, _FRAME_A, "original")
    payload = CoordinateFrameDocument(records=(record,)).to_dict()
    rejected = {"frame_id": "broken", "future": {"keep": True}}
    payload["records"].append(rejected)
    document = CoordinateFrameDocument.from_dict(payload)
    renamed = replace(record, name="renamed")
    coordinator, _registry, _session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, document)

    save = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(records=(renamed,))
        )
    )

    serialized = save.document.to_dict()["records"]
    assert serialized[0]["name"] == "renamed"
    assert serialized[1] == rejected


def test_publication_factory_builds_one_immutable_record_upsert(
    tmp_path: Path,
) -> None:
    design = _document(tmp_path)
    first = _record(design, _FRAME_A, "first")
    second = _record(design, _FRAME_B, "second")
    committed = replace(first, name="committed", version=1)
    checkpoint = DesignSessionCheckpoint.capture(DesignSession(document=design))
    projection = _link(committed).projection
    notice = CoordinateNotice("saved")

    replaced = FrameRecordsPublication.for_committed_record(
        (first, second),
        committed,
        previous_record=first,
        previous_session=checkpoint,
        projection=projection,
        success_notice=notice,
    )
    appended = FrameRecordsPublication.for_committed_record(
        (first,),
        second,
        previous_session=checkpoint,
        projection=_link(second).projection,
    )
    with_notice = FrameRecordsPublication.for_committed_record(
        (first,),
        committed,
        success_message="registration saved",
        success_duration_ms=7000,
        success_code="registration_saved",
    )

    assert replaced.records == (committed, second)
    assert replaced.previous_record is first
    assert replaced.previous_session is checkpoint
    assert replaced.proposed_session_link == _link(committed)
    assert replaced.success_notice is notice
    assert not hasattr(replaced, "rollback_outcome")
    assert appended.records == (first, second)
    assert with_notice.success_notice == CoordinateNotice(
        "registration saved",
        duration_ms=7000,
        code="registration_saved",
    )


def test_coordinator_publishes_factory_proposal_from_domain_inputs(
    tmp_path: Path,
) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    committed = replace(draft, name="registered", version=1)
    coordinator, registry, session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))

    transition = coordinator.publish_frame_records(
        FrameRecordsPublication.for_committed_record(
            registry.snapshot().records,
            committed,
            previous_record=draft,
            previous_session=DesignSessionCheckpoint.capture(session),
            projection=_link(committed).projection,
            success_message="registration saved",
            success_duration_ms=7000,
            success_code="registration_saved",
        )
    )

    save = _save_intent(transition)
    assert registry.get(committed.frame_id) == committed
    assert session.active_frame_id == committed.frame_id
    acknowledged = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreSuccess(save.intent_id, "save"),
        )
    )
    assert acknowledged.notices == (
        CoordinateNotice(
            "registration saved",
            duration_ms=7000,
            code="registration_saved",
        ),
    )


def test_publication_captures_its_adopted_session_for_rollback(
    tmp_path: Path,
) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    committed = replace(draft, name="registered", version=1)
    coordinator, registry, session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))

    save = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication.for_committed_record(
                registry.snapshot().records,
                committed,
                previous_record=draft,
                projection=_link(committed).projection,
            )
        )
    )
    assert session.active_frame_id == committed.frame_id

    coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk full"),
        )
    )

    assert registry.get(draft.frame_id) == draft
    assert session.active_frame_id is None


def test_terminal_failure_privately_releases_contact_commit(
    tmp_path: Path,
) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    contacted = replace(
        draft,
        transform=replace(draft.transform, a_zero_machine_mm=4.0),
        version=1,
    )
    released: list[dict[str, object]] = []
    registry = CoordinateFrameRegistry()
    session = DesignSession(document=design)
    coordinator = CoordinateSystemCoordinator(
        registry=registry,
        session=session,
        registration_lifecycle=SimpleNamespace(
            _release_contact_commit=lambda **identity: released.append(identity)
        ),
    )
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))
    save = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication.for_committed_record(
                registry.snapshot().records,
                contacted,
                previous_record=draft,
            )
        )
    )

    failed = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk full"),
        )
    )
    repeated = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "late"),
        )
    )

    assert not hasattr(failed, "publication_outcomes")
    assert not hasattr(repeated, "publication_outcomes")
    assert released == [
        {
            "session_identity": id(session),
            "frame_id": draft.frame_id,
            "frame_version": draft.version,
        }
    ]


def test_publication_installs_one_adopted_registry_and_session_before_save(
    tmp_path: Path,
) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    committed = replace(draft, name="registered", version=1)
    coordinator, registry, session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))
    checkpoint = DesignSessionCheckpoint.capture(session)

    transition = coordinator.publish_frame_records(
        FrameRecordsPublication(
            records=(committed,),
            previous_record=draft,
            committed_record=committed,
            previous_session=checkpoint,
            proposed_session_link=_link(committed),
        )
    )

    intent = _save_intent(transition)
    assert intent.document.records == (committed,)
    assert registry.get(draft.frame_id) == committed
    assert session.active_frame_id == committed.frame_id
    assert transition.snapshot.records == registry.snapshot().records


def test_older_save_failure_defers_to_newer_publication(tmp_path: Path) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    registered = replace(draft, name="registered", version=1)
    focused = replace(registered, name="focused", version=2)
    coordinator, registry, session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))
    first = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(registered,),
                previous_record=draft,
                committed_record=registered,
                previous_session=DesignSessionCheckpoint.capture(session),
                proposed_session_link=_link(registered),
                success_notice=CoordinateNotice("registration saved"),
            )
        )
    )
    second = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(focused,),
                previous_record=registered,
                committed_record=focused,
                previous_session=DesignSessionCheckpoint.capture(session),
                proposed_session_link=_link(focused),
                success_notice=CoordinateNotice("focus saved"),
            )
        )
    )

    transition = coordinator.complete(
        CoordinateAdapterCompletion(
            first.intent_id,
            CoordinateFrameStoreFailure(first.intent_id, "save", "disk full"),
        )
    )

    assert transition.notices == ()
    assert registry.get(draft.frame_id) == focused
    assert session.active_frame_id == focused.frame_id
    assert coordinator.snapshot().records == (focused,)
    assert second.intent_id > first.intent_id


def test_deferred_and_stale_failures_do_not_release_contact_commit(
    tmp_path: Path,
) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    contacted = replace(
        draft,
        name="contacted",
        transform=replace(draft.transform, a_zero_machine_mm=4.0),
        version=1,
    )
    renamed = replace(contacted, name="renamed", version=2)
    releases: list[dict[str, object]] = []
    registry = CoordinateFrameRegistry()
    coordinator = CoordinateSystemCoordinator(
        registry=registry,
        session=DesignSession(document=design),
        registration_lifecycle=SimpleNamespace(
            _release_contact_commit=lambda **identity: releases.append(identity)
        ),
    )
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))
    first = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication.for_committed_record(
                registry.snapshot().records,
                contacted,
                previous_record=draft,
            )
        )
    )
    second = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication.for_committed_record(
                registry.snapshot().records,
                renamed,
                previous_record=contacted,
            )
        )
    )

    coordinator.complete(
        CoordinateAdapterCompletion(
            first.intent_id,
            CoordinateFrameStoreFailure(first.intent_id, "save", "deferred"),
        )
    )
    coordinator.complete(
        CoordinateAdapterCompletion(
            second.intent_id,
            CoordinateFrameStoreSuccess(second.intent_id, "save"),
        )
    )
    coordinator.complete(
        CoordinateAdapterCompletion(
            first.intent_id,
            CoordinateFrameStoreFailure(first.intent_id, "save", "stale"),
        )
    )

    assert releases == []


def test_terminal_failure_rolls_back_each_frame_and_exact_session_once(
    tmp_path: Path,
) -> None:
    design = _document(tmp_path)
    a0 = _record(design, _FRAME_A, "a0")
    b0 = _record(design, _FRAME_B, "b0")
    a1 = replace(a0, name="a1", version=1)
    a2 = replace(a1, name="a2", version=2)
    b1 = replace(b0, name="b1", version=1)
    coordinator, registry, session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(a0, b0)))
    baseline = DesignSessionCheckpoint.capture(session)
    first = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(a1, b0),
                previous_record=a0,
                committed_record=a1,
                previous_session=baseline,
                proposed_session_link=_link(a1),
            )
        )
    )
    coordinator.publish_frame_records(
        FrameRecordsPublication(
            records=(a2, b0),
            previous_record=a1,
            committed_record=a2,
            previous_session=DesignSessionCheckpoint.capture(session),
            proposed_session_link=_link(a2),
        )
    )
    terminal = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(a2, b1),
                previous_record=b0,
                committed_record=b1,
                previous_session=DesignSessionCheckpoint.capture(session),
                proposed_session_link=_link(b1),
            )
        )
    )
    session.registration_status = "changed after publication"

    transition = coordinator.complete(
        CoordinateAdapterCompletion(
            terminal.intent_id,
            CoordinateFrameStoreFailure(terminal.intent_id, "save", "disk full"),
        )
    )

    assert first.intent_id < terminal.intent_id
    assert registry.get(_FRAME_A) == a0
    assert registry.get(_FRAME_B) == b0
    assert DesignSessionCheckpoint.capture(session) == baseline
    assert transition.snapshot.records == (a0, b0)
    assert transition.notices == (
        CoordinateNotice("Design coordinate frames could not be saved.", severity="error", duration_ms=6000),
    )


def test_bulk_publication_failure_restores_full_previous_record_set(
    tmp_path: Path,
) -> None:
    design = _document(tmp_path)
    original_a = _record(design, _FRAME_A, "original-a")
    original_b = _record(design, _FRAME_B, "original-b")
    changed_a = replace(original_a, name="changed-a")
    coordinator, registry, _session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(
        coordinator,
        load,
        CoordinateFrameDocument(records=(original_a, original_b)),
    )
    save = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(records=(changed_a,))
        )
    )

    coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk full"),
        )
    )

    assert registry.get(_FRAME_A) == original_a
    assert registry.get(_FRAME_B) == original_b


def test_newer_success_acknowledges_every_coalesced_publication(tmp_path: Path) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    registered = replace(draft, name="registered", version=1)
    focused = replace(registered, name="focused", version=2)
    coordinator, _registry, session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))
    first = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(registered,),
                previous_record=draft,
                committed_record=registered,
                previous_session=DesignSessionCheckpoint.capture(session),
                proposed_session_link=_link(registered),
                success_notice=CoordinateNotice("registration saved"),
            )
        )
    )
    second = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(focused,),
                previous_record=registered,
                committed_record=focused,
                previous_session=DesignSessionCheckpoint.capture(session),
                proposed_session_link=_link(focused),
                success_notice=CoordinateNotice("focus saved"),
            )
        )
    )

    transition = coordinator.complete(
        CoordinateAdapterCompletion(
            second.intent_id,
            CoordinateFrameStoreSuccess(second.intent_id, "save"),
        )
    )

    assert first.intent_id < second.intent_id
    assert transition.notices == (CoordinateNotice("focus saved"),)


def test_coalesced_success_keeps_durable_legacy_completion_tag(tmp_path: Path) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    migrated = replace(draft, name="migrated", version=1)
    focused = replace(migrated, name="focused", version=2)
    coordinator, _registry, session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))
    coordinator.publish_frame_records(
        FrameRecordsPublication(
            records=(migrated,),
            previous_record=draft,
            committed_record=migrated,
            previous_session=DesignSessionCheckpoint.capture(session),
            proposed_session_link=_link(migrated),
            success_notice=CoordinateNotice("", code="legacy_migration_saved"),
        )
    )
    latest = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(focused,),
                previous_record=migrated,
                committed_record=focused,
                previous_session=DesignSessionCheckpoint.capture(session),
                proposed_session_link=_link(focused),
            )
        )
    )

    transition = coordinator.complete(
        CoordinateAdapterCompletion(
            latest.intent_id,
            CoordinateFrameStoreSuccess(latest.intent_id, "save"),
        )
    )

    assert transition.notices == (
        CoordinateNotice("", code="legacy_migration_saved"),
    )


def test_only_operator_alignment_failure_requests_snap_restore(tmp_path: Path) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    committed = replace(draft, name="committed", version=1)
    coordinator, _registry, session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))
    save = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(committed,),
                previous_record=draft,
                committed_record=committed,
                previous_session=DesignSessionCheckpoint.capture(session),
                proposed_session_link=_link(committed),
                success_notice=CoordinateNotice(
                    "alignment saved",
                    code="operator_alignment_saved",
                ),
            )
        )
    )

    transition = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk full"),
        )
    )

    assert transition.notices == (
        CoordinateNotice(
            "Design coordinate frames could not be saved.",
            severity="error",
            duration_ms=6000,
            code="operator_alignment_rollback",
        ),
    )


def test_repeated_and_late_save_completions_are_inert(tmp_path: Path) -> None:
    design = _document(tmp_path)
    draft = _record(design, _FRAME_A, "draft")
    committed = replace(draft, name="committed", version=1)
    coordinator, registry, session = _coordinator(design)
    load = coordinator.start(MachineProfileObservation("profile")).intents[0]
    assert isinstance(load, LoadCoordinateFramesIntent)
    _complete_load(coordinator, load, CoordinateFrameDocument(records=(draft,)))
    save = _save_intent(
        coordinator.publish_frame_records(
            FrameRecordsPublication(
                records=(committed,),
                previous_record=draft,
                committed_record=committed,
                previous_session=DesignSessionCheckpoint.capture(session),
                proposed_session_link=_link(committed),
                success_notice=CoordinateNotice("saved"),
            )
        )
    )
    success = CoordinateAdapterCompletion(
        save.intent_id,
        CoordinateFrameStoreSuccess(save.intent_id, "save"),
    )

    assert coordinator.complete(success).notices == (CoordinateNotice("saved"),)
    assert coordinator.complete(success).notices == ()
    late = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "late"),
        )
    )
    assert late.notices == ()
    assert registry.get(draft.frame_id) == committed
