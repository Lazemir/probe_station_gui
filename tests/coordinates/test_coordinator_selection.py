from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    CoordinateAuthorityObservation,
    CoordinateMotionRequest,
    CoordinateSystemSelection,
    CustomSystemsRequest,
    DesignCalibrationObservation,
    FrameRecordsPublication,
    MachineProfileObservation,
    PersistCoordinateSelectionIntent,
)
from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    PhysicalMachinePose,
    ReadinessStatus,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameLoadResult,
    CoordinateFrameStoreFailure,
)
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.coordinates.transforms import BFrameTransform
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.session import DesignSession
from probe_station_gui.settings.axis_calibration_config import (
    default_axis_calibrations,
)
from probe_station_gui.settings.software_coordinates import (
    CustomFrameSettings,
    SoftwareCoordinateSettings,
)
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot


DESIGN_ID = "11111111-1111-4111-8111-111111111111"


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


def _ready_design_record(document: DesignDocument) -> CoordinateFrameRecord:
    metadata = replace(
        DesignFrameMetadata.from_document(document),
        source_design_marks=((0.0, 0.0), (10.0, 0.0)),
        source_machine_marks=((0.0, 0.0), (0.01, 0.0)),
    )
    return CoordinateFrameRecord(
        frame_id=DESIGN_ID,
        kind=FrameKind.DESIGN,
        name="chip",
        version=3,
        transform=BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=1.0,
            a_zero_machine_mm=None,
        ),
        readiness={
            axis: AxisReadiness(
                ReadinessStatus.READY
                if axis in {"X", "Y", "Z", "B"}
                else ReadinessStatus.MISSING,
                "" if axis in {"X", "Y", "Z", "B"} else "Find contact.",
            )
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata=metadata.to_dict(),
    )


def _load(
    coordinator: CoordinateSystemCoordinator,
    records: tuple[CoordinateFrameRecord, ...],
):
    intent = coordinator.start(MachineProfileObservation("profile-a")).intents[0]
    return coordinator.complete(
        CoordinateAdapterCompletion(
            intent.intent_id,
            CoordinateFrameLoadResult(
                intent.intent_id,
                CoordinateFrameDocument(records=records),
            ),
        )
    )


def _authority(*axes: str) -> CoordinateAuthorityObservation:
    return CoordinateAuthorityObservation(
        physical_pose=PhysicalMachinePose({axis: 0.0 for axis in axes}),
        homed_axes=frozenset({"X", "Y"}),
        machine_snapshot=None,
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )


def _machine_snapshot(
    *,
    x_mm: float = 0.0,
    y_mm: float = 0.0,
    z_mm: float = 0.0,
    a_mm: float = 0.0,
    b_deg: float = 0.0,
) -> MachineCoordinateSnapshot:
    machine = (x_mm, y_mm, z_mm, a_mm, b_deg, 0.0)
    axis_index = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}
    mapper = StageAxisCalibrationMapper(
        calibrations=default_axis_calibrations(),
        position_reporting_mode="work",
        active_work_coordinate_system="G54",
        controller_coordinate_offsets={"G54": (0.0,) * len(machine)},
        axis_index=axis_index,
    )
    status = SimpleNamespace(
        state="Idle",
        synchronized_machine_position=machine,
        work_position=machine,
        work_offset=(0.0,) * len(machine),
        coordinate_system="G54",
    )
    return MachineCoordinateSnapshot.from_status(status, mapper, axis_index)


def _design_authority(
    *,
    pivot: tuple[float, float] | None = (0.0, 0.0),
    objective: tuple[float, float] = (0.0, 0.0),
    b_deg: float = 0.0,
    x_mm: float = 0.0,
    y_mm: float = 0.0,
    pivot_error: str | None = None,
    pivot_error_permanent: bool = False,
) -> CoordinateAuthorityObservation:
    snapshot = _machine_snapshot(x_mm=x_mm, y_mm=y_mm, b_deg=b_deg)
    return CoordinateAuthorityObservation(
        physical_pose=snapshot.physical_machine_pose,
        homed_axes=frozenset({"X", "Y", "Z", "A"}),
        machine_snapshot=snapshot,
        pivot_machine_xy=pivot,
        objective_xy_offset=objective,
        pivot_error=pivot_error,
        pivot_error_permanent=pivot_error_permanent,
    )


def test_machine_selection_is_renderable_before_any_machine_pose() -> None:
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession())

    selected = coordinator.select_system(CoordinateSystemSelection("machine"))

    assert selected.accepted
    assert selected.snapshot.selected_frame_id == "machine"
    assert selected.snapshot.display_plan.selected_frame_id == "machine"
    assert all(update.value is None for update in selected.snapshot.display_plan.axis_updates)


def test_temporary_b_loss_retains_one_selected_design_intent(tmp_path: Path) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_authority("X", "Y", "Z", "A", "B"))
    selected = coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))
    assert selected.snapshot.selected_frame_id == DESIGN_ID
    assert selected.intents == (PersistCoordinateSelectionIntent(DESIGN_ID),)

    unavailable = coordinator.observe_authority(_authority("X", "Y", "Z", "A"))

    assert unavailable.snapshot.selected_frame_id == DESIGN_ID
    assert unavailable.snapshot.display_plan.selected_frame_id == DESIGN_ID
    assert unavailable.snapshot.display_plan.selection_available is False
    assert all(
        update.color_role == "unavailable"
        for update in unavailable.snapshot.display_plan.axis_updates
    )
    assert not any(
        isinstance(intent, PersistCoordinateSelectionIntent)
        for intent in unavailable.intents
    )


def test_reload_start_retains_selected_design_as_temporarily_unavailable(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_design_authority())
    coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))

    pending = coordinator.start(MachineProfileObservation("profile-a"))

    assert pending.snapshot.selected_frame_id == DESIGN_ID
    assert pending.snapshot.display_plan.selection_available is False
    assert all(
        update.color_role == "unavailable"
        for update in pending.snapshot.display_plan.axis_updates
    )
    assert not any(
        isinstance(intent, PersistCoordinateSelectionIntent)
        for intent in pending.intents
    )


def test_deleted_selected_frame_falls_back_and_persists_machine(tmp_path: Path) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_authority("X", "Y", "Z", "A", "B"))
    coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))

    reloaded = _load(coordinator, ())

    assert reloaded.snapshot.selected_frame_id == "machine"
    assert reloaded.intents == (PersistCoordinateSelectionIntent("machine"),)


def test_blocked_runtime_provenance_retains_recoverable_selection(tmp_path: Path) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    ready = _ready_design_record(document)
    _load(coordinator, (ready,))
    coordinator.observe_authority(_authority("X", "Y", "Z", "A", "B"))
    coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))
    blocked = replace(
        ready,
        metadata={
            "_runtime_provenance_status": "blocked",
            "_runtime_provenance_reason": "Design source is temporarily unavailable.",
        },
    )

    reloaded = _load(coordinator, (blocked,))

    assert reloaded.snapshot.selected_frame_id == DESIGN_ID
    assert reloaded.snapshot.display_plan.selection_available is False
    assert reloaded.intents == ()


def test_custom_sync_does_not_resurrect_deleted_frame_or_drop_unknown_payload(
    tmp_path: Path,
) -> None:
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession())
    _load(coordinator, ())
    frame_id = str(uuid4())
    settings = SoftwareCoordinateSettings(
        custom_frames=(
            CustomFrameSettings(
                frame_id=frame_id,
                name="fixture",
                origin_x_mm=1.0,
                origin_y_mm=2.0,
                reference_b_deg=0.0,
                xy_angle_deg=0.0,
                b_zero_deg=0.0,
            ),
        ),
        _raw_custom_frames=(
            {"frame_id": frame_id, "name": "old payload"},
            {"future_kind": "preserve-me", "payload": [1, 2, 3]},
        ),
        _accepted_custom_frame_slots=((0, frame_id),),
    )
    synchronized = coordinator.synchronize_custom_systems(
        CustomSystemsRequest(settings)
    )
    assert [record.frame_id for record in synchronized.snapshot.records] == [frame_id]

    deleted = settings.without_custom_frame(frame_id)
    after_delete = coordinator.synchronize_custom_systems(
        CustomSystemsRequest(deleted)
    )

    assert after_delete.snapshot.records == ()
    assert deleted.to_dict()["custom_frames"] == [
        {"future_kind": "preserve-me", "payload": [1, 2, 3]}
    ]


def test_rejected_custom_sync_does_not_poison_later_authority_observation() -> None:
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession())
    _load(coordinator, ())
    blocked = SoftwareCoordinateSettings(
        _preserved_raw_section_present=True,
        _preserved_raw_section={"future": "payload"},
    )

    with pytest.raises(ValueError, match="degraded"):
        coordinator.synchronize_custom_systems(CustomSystemsRequest(blocked))

    observed = coordinator.observe_authority(_authority("X", "Y", "B"))
    assert observed.snapshot.records == ()


def test_reversed_custom_settings_order_is_idempotent_across_authority_samples() -> None:
    registry = CoordinateFrameRegistry()
    coordinator = CoordinateSystemCoordinator._for_testing(
        registry=registry,
        session=DesignSession(),
    )
    _load(coordinator, ())
    settings = SoftwareCoordinateSettings(
        custom_frames=tuple(
            CustomFrameSettings(
                frame_id=str(uuid4()),
                name=name,
                origin_x_mm=float(index),
                origin_y_mm=0.0,
                reference_b_deg=0.0,
                xy_angle_deg=0.0,
                b_zero_deg=0.0,
            )
            for index, name in enumerate(("zeta", "alpha"))
        )
    )
    coordinator.synchronize_custom_systems(CustomSystemsRequest(settings))
    generation = registry.snapshot().generation

    coordinator.observe_authority(_authority("X", "Y", "B"))
    coordinator.observe_authority(_authority("X", "Y", "B"))

    assert registry.snapshot().generation == generation


def test_loaded_design_collision_drops_cached_custom_overlay_without_split_state(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, ())
    settings = SoftwareCoordinateSettings(
        custom_frames=(
            CustomFrameSettings(
                frame_id=DESIGN_ID,
                name="old fixture",
                origin_x_mm=0.0,
                origin_y_mm=0.0,
                reference_b_deg=0.0,
                xy_angle_deg=0.0,
                b_zero_deg=0.0,
            ),
        )
    )
    coordinator.synchronize_custom_systems(CustomSystemsRequest(settings))

    loaded = _load(coordinator, (_ready_design_record(document),))

    assert loaded.snapshot.records == (_ready_design_record(document),)
    assert any("collides" in notice.message for notice in loaded.notices)
    assert coordinator.observe_authority(_design_authority()).snapshot.records == (
        _ready_design_record(document),
    )


def test_explicit_unavailable_selection_is_rejected_without_changing_intent(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_authority("X", "Y", "Z", "A"))

    rejected = coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))

    assert rejected.accepted is False
    assert rejected.snapshot.selected_frame_id == "machine"
    assert rejected.intents == ()
    assert rejected.snapshot.display_plan.selection_available is True
    assert rejected.snapshot.display_plan.selection_reason is None
    assert [notice.message for notice in rejected.notices] == [
        "B coordinate is unavailable."
    ]


def test_registration_transition_keeps_finished_coordinate_presentation() -> None:
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession())

    transition = coordinator.close_design()

    assert transition.snapshot.display_plan is not None
    assert transition.snapshot.display_plan.selected_frame_id == "machine"
    assert transition.snapshot.design_lease is not None


def test_startup_restore_waits_for_authority_then_restores_surviving_frame(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(
        session=DesignSession(document=document),
        restore_frame_id=DESIGN_ID,
    )

    loaded = _load(coordinator, (_ready_design_record(document),))
    restored = coordinator.observe_authority(_design_authority())

    assert loaded.snapshot.selected_frame_id == DESIGN_ID
    assert loaded.snapshot.display_plan.selection_available is False
    assert restored.snapshot.selected_frame_id == DESIGN_ID
    assert restored.snapshot.display_plan.selection_available is True
    assert restored.intents == ()


def test_invalid_pivot_falls_back_to_machine_and_persists_only_once(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_design_authority())
    coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))

    invalid = coordinator.observe_authority(
        _design_authority(
            pivot=(float("nan"), 0.0),
            pivot_error="Stored rotation pivot is invalid.",
            pivot_error_permanent=True,
        )
    )
    repeated = coordinator.observe_authority(
        _design_authority(
            pivot=(float("nan"), 0.0),
            pivot_error="Stored rotation pivot is invalid.",
            pivot_error_permanent=True,
        )
    )

    assert invalid.snapshot.selected_frame_id == "machine"
    assert invalid.intents == (PersistCoordinateSelectionIntent("machine"),)
    assert repeated.snapshot.selected_frame_id == "machine"
    assert repeated.intents == ()


def test_temporary_missing_pivot_retains_selected_design(tmp_path: Path) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_design_authority())
    coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))

    unavailable = coordinator.observe_authority(_design_authority(pivot=None))

    assert unavailable.snapshot.selected_frame_id == DESIGN_ID
    assert unavailable.snapshot.display_plan.selection_available is False
    assert unavailable.intents == ()


def test_design_lease_uses_active_design_frame_not_gui_selection(
    tmp_path: Path,
) -> None:
    document = replace(_document(tmp_path), source_load_id="load-a")
    session = DesignSession(document=document, active_frame_id=DESIGN_ID)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=session)
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_design_authority())
    coordinator.select_system(CoordinateSystemSelection("machine"))

    lease = coordinator.current_design_lease()

    assert coordinator.snapshot().selected_frame_id == "machine"
    assert lease.frame_id == DESIGN_ID
    assert lease.usable
    assert coordinator.project_design_to_raw_stage(lease, (10.0, 20.0)) is not None


def test_authority_and_snapshot_paths_do_not_resolve_design_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    document = replace(_document(tmp_path), source_load_id="load-a")
    session = DesignSession(document=document, active_frame_id=DESIGN_ID)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=session)
    _load(coordinator, (_ready_design_record(document),))
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda *_args, **_kwargs: pytest.fail("GUI-thread path resolution"),
    )

    observed = coordinator.observe_authority(_design_authority())

    assert observed.snapshot.design_lease.usable
    assert coordinator.snapshot().design_lease.usable


def test_authority_transition_reports_registration_loss_recovery_once(
    tmp_path: Path,
) -> None:
    document = replace(_document(tmp_path), source_load_id="load-a")
    session = DesignSession(document=document, active_frame_id=DESIGN_ID)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=session)
    _load(coordinator, (_ready_design_record(document),))
    available_authority = _design_authority()

    ready = coordinator.observe_authority(available_authority)
    lost = coordinator.observe_authority(_authority("X", "Y", "Z", "A"))
    repeated = coordinator.observe_authority(_authority("X", "Y", "Z", "A"))
    recovered = coordinator.observe_authority(available_authority)

    assert ready.view_changed
    assert ready.snapshot.registration.registration_valid
    assert lost.view_changed
    assert not lost.snapshot.registration.registration_valid
    assert not repeated.view_changed
    assert recovered.view_changed
    assert recovered.snapshot.registration.registration_valid


def test_identical_authority_sample_does_not_relink_active_design(
    tmp_path: Path,
    monkeypatch,
) -> None:
    document = replace(_document(tmp_path), source_load_id="load-a")
    session = DesignSession(document=document, active_frame_id=DESIGN_ID)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=session)
    _load(coordinator, (_ready_design_record(document),))
    authority = _design_authority()
    coordinator.observe_authority(authority)
    relinks: list[object] = []
    monkeypatch.setattr(
        session,
        "link_active_frame",
        lambda *args, **kwargs: relinks.append((args, kwargs)),
    )

    repeated = coordinator.observe_authority(authority)

    assert relinks == []
    assert not repeated.view_changed


def test_design_lease_rejects_missing_xyb_registration(tmp_path: Path) -> None:
    document = replace(_document(tmp_path), source_load_id="load-a")
    session = DesignSession(document=document, active_frame_id=DESIGN_ID)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=session)
    record = _ready_design_record(document)
    readiness = dict(record.readiness)
    readiness["X"] = AxisReadiness(ReadinessStatus.MISSING, "Register X.")
    _load(coordinator, (replace(record, readiness=readiness),))
    coordinator.observe_authority(_design_authority())

    lease = coordinator.current_design_lease()

    assert lease.usable is False
    assert lease.reason == "Register X."
    assert coordinator.project_design_to_raw_stage(lease, (1.0, 2.0)) is None


def test_design_lease_stales_on_every_projection_input(tmp_path: Path) -> None:
    document = replace(_document(tmp_path), source_load_id="load-a")
    session = DesignSession(document=document, active_frame_id=DESIGN_ID)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=session)
    record = _ready_design_record(document)
    _load(coordinator, (record,))
    coordinator.observe_authority(_design_authority())
    lease = coordinator.current_design_lease()
    assert coordinator.design_lease_is_current(lease)

    coordinator.observe_authority(_design_authority(objective=(1.0, 0.0)))
    assert coordinator.design_lease_is_current(lease) is False
    coordinator.observe_authority(_design_authority())
    lease = coordinator.current_design_lease()
    coordinator.observe_authority(_design_authority(pivot=(1.0, 0.0)))
    assert coordinator.design_lease_is_current(lease) is False
    coordinator.observe_authority(_design_authority())
    lease = coordinator.current_design_lease()
    coordinator.observe_authority(_design_authority(b_deg=2.0))
    assert coordinator.design_lease_is_current(lease) is False

    coordinator.observe_authority(_design_authority())
    lease = coordinator.current_design_lease()
    _load(coordinator, (replace(record, version=record.version + 1),))
    assert coordinator.design_lease_is_current(lease) is False

    lease = coordinator.current_design_lease()
    session.document = replace(document, source_load_id="load-b")
    assert coordinator.design_lease_is_current(lease) is False


def test_semantically_corrupt_selected_frame_falls_back_to_machine(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    record = _ready_design_record(document)
    _load(coordinator, (record,))
    coordinator.observe_authority(_design_authority())
    coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))
    corrupt = replace(
        record,
        transform=replace(record.transform, z_zero_machine_mm=None),
    )

    reloaded = _load(coordinator, (corrupt,))

    assert reloaded.snapshot.selected_frame_id == "machine"
    assert reloaded.intents == (PersistCoordinateSelectionIntent("machine"),)


def test_pending_restore_recovers_after_runtime_provenance_verification(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(
        session=DesignSession(document=document),
        restore_frame_id=DESIGN_ID,
    )
    record = _ready_design_record(document)
    pending = replace(
        record,
        metadata={
            **record.metadata,
            "_runtime_provenance_status": "pending",
            "_runtime_provenance_reason": "Design coordinate provenance is being checked.",
        },
    )
    _load(coordinator, (pending,))

    waiting = coordinator.observe_authority(_design_authority())
    verified = _load(coordinator, (record,))

    assert waiting.snapshot.selected_frame_id == DESIGN_ID
    assert waiting.snapshot.display_plan.selection_available is False
    assert waiting.intents == ()
    assert verified.snapshot.selected_frame_id == DESIGN_ID
    assert verified.intents == ()


def test_publication_deleting_selected_frame_reduces_selection_immediately(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_design_authority())
    coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))

    published = coordinator._publish_frame_records(FrameRecordsPublication(records=()))

    assert published.snapshot.selected_frame_id == "machine"
    assert PersistCoordinateSelectionIntent("machine") in published.intents


def test_custom_deletion_reduces_selected_custom_and_does_not_resurrect(
    tmp_path: Path,
) -> None:
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession())
    _load(coordinator, ())
    coordinator.observe_authority(_design_authority())
    frame_id = str(uuid4())
    settings = SoftwareCoordinateSettings(
        custom_frames=(
            CustomFrameSettings(
                frame_id=frame_id,
                name="fixture",
                origin_x_mm=1.0,
                origin_y_mm=2.0,
                reference_b_deg=0.0,
                xy_angle_deg=0.0,
                b_zero_deg=0.0,
            ),
        )
    )
    coordinator.synchronize_custom_systems(CustomSystemsRequest(settings))
    coordinator.select_system(CoordinateSystemSelection(frame_id))

    deleted = coordinator.synchronize_custom_systems(
        CustomSystemsRequest(settings.without_custom_frame(frame_id))
    )
    repeated = coordinator.synchronize_custom_systems(
        CustomSystemsRequest(settings.without_custom_frame(frame_id))
    )

    assert deleted.snapshot.records == ()
    assert deleted.snapshot.selected_frame_id == "machine"
    assert deleted.intents == (PersistCoordinateSelectionIntent("machine"),)
    assert repeated.snapshot.records == ()
    assert repeated.intents == ()


def test_pending_frame_save_rollback_cannot_resurrect_deleted_custom(
    tmp_path: Path,
) -> None:
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession())
    _load(coordinator, ())
    frame_id = str(uuid4())
    settings = SoftwareCoordinateSettings(
        custom_frames=(
            CustomFrameSettings(
                frame_id=frame_id,
                name="fixture",
                origin_x_mm=1.0,
                origin_y_mm=2.0,
                reference_b_deg=0.0,
                xy_angle_deg=0.0,
                b_zero_deg=0.0,
            ),
        )
    )
    synchronized = coordinator.synchronize_custom_systems(
        CustomSystemsRequest(settings)
    )
    pending = coordinator._publish_frame_records(
        FrameRecordsPublication(records=synchronized.snapshot.records)
    )
    save = next(intent for intent in pending.intents if hasattr(intent, "intent_id"))
    coordinator.synchronize_custom_systems(
        CustomSystemsRequest(settings.without_custom_frame(frame_id))
    )

    failed = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk busy"),
        )
    )

    assert failed.snapshot.records == ()
    assert coordinator.snapshot().records == ()


def test_calibration_save_failure_keeps_runtime_design_frame_blocked(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    session = DesignSession(document=document, active_frame_id=DESIGN_ID)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=session)
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_design_authority())

    changed = coordinator.observe_design_calibrations(
        DesignCalibrationObservation(
            tuple((axis, f"new-{axis}") for axis in ("X", "Y", "Z", "A", "B"))
        )
    )
    save = next(intent for intent in changed.intents if hasattr(intent, "intent_id"))
    failed = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk busy"),
        )
    )

    runtime_record = failed.snapshot.records[0]
    assert runtime_record.readiness["X"].status is ReadinessStatus.STALE
    assert failed.snapshot.design_lease.usable is False
    assert "calibration" in failed.snapshot.design_lease.reason.lower()


def test_display_plan_contains_machine_and_uses_same_selection_decision(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_design_authority())
    selected = coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))

    plan = selected.snapshot.display_plan
    assert plan.selected_frame_id == selected.snapshot.selected_frame_id
    assert [entry.frame_id for entry in plan.selector_entries] == [
        "machine",
        DESIGN_ID,
    ]
    assert all(update.axis != "C" for update in plan.axis_updates)


def test_relative_motion_uses_selected_design_snapshot_for_xy_rotation(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    record = replace(
        _ready_design_record(document),
        transform=BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=90.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=1.0,
        ),
    )
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (record,))
    coordinator.observe_authority(_design_authority(x_mm=10.0, y_mm=20.0))
    selected = coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))

    projection = coordinator.project_motion(
        CoordinateMotionRequest(
            lease=selected.snapshot.motion_lease,
            mode="G91",
            axis_values=(("X", 1.0),),
        )
    )

    assert projection.accepted
    assert dict(projection.raw_distances) == pytest.approx({"Y": 1.0})
    assert dict(projection.raw_targets) == pytest.approx({"X": 10.0, "Y": 21.0})
    assert dict(projection.display_targets) == pytest.approx({"X": 21.0, "Y": -10.0})


def test_non_machine_b_motion_rejects_single_segment_pivot_chord(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    record = replace(
        _ready_design_record(document),
        transform=BFrameTransform(
            origin_xy_at_reference_b=(10.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=1.0,
        ),
    )
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (record,))
    coordinator.observe_authority(_design_authority(x_mm=10.0, y_mm=0.0))
    selected = coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))

    projection = coordinator.project_motion(
        CoordinateMotionRequest(
            lease=selected.snapshot.motion_lease,
            mode="G91",
            axis_values=(("B", 90.0),),
        )
    )

    assert projection.accepted is False
    assert projection.raw_distances == ()
    assert projection.raw_targets == ()
    assert "pivot path" in projection.reason.lower()


def test_relative_motion_rejects_stale_selected_snapshot_lease(tmp_path: Path) -> None:
    document = _document(tmp_path)
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (_ready_design_record(document),))
    coordinator.observe_authority(_design_authority())
    selected = coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))
    stale_lease = selected.snapshot.motion_lease

    coordinator.observe_authority(_design_authority(b_deg=5.0))
    projection = coordinator.project_motion(
        CoordinateMotionRequest(
            lease=stale_lease,
            mode="G91",
            axis_values=(("X", 1.0),),
        )
    )

    assert projection.accepted is False
    assert projection.raw_distances == ()
    assert "changed" in projection.reason.lower()


def test_continuous_relative_motion_rebases_pose_but_not_coordinate_basis(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    record = replace(
        _ready_design_record(document),
        transform=BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=90.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=1.0,
        ),
    )
    coordinator = CoordinateSystemCoordinator._adopt_session(session=DesignSession(document=document))
    _load(coordinator, (record,))
    coordinator.observe_authority(_design_authority(x_mm=10.0, y_mm=20.0))
    selected = coordinator.select_system(CoordinateSystemSelection(DESIGN_ID))
    start_lease = selected.snapshot.motion_lease

    coordinator.observe_authority(_design_authority(x_mm=10.0, y_mm=21.0))
    rebased = coordinator.project_motion(
        CoordinateMotionRequest(
            lease=start_lease,
            mode="G91",
            axis_values=(("X", 1.0),),
            allow_pose_rebase=True,
        )
    )

    assert rebased.accepted
    assert rebased.lease != start_lease
    assert dict(rebased.raw_distances) == pytest.approx({"Y": 1.0})

    coordinator.observe_authority(
        _design_authority(x_mm=10.0, y_mm=21.0, pivot=(1.0, 0.0))
    )
    changed_basis = coordinator.project_motion(
        CoordinateMotionRequest(
            lease=rebased.lease,
            mode="G91",
            axis_values=(("X", 1.0),),
            allow_pose_rebase=True,
        )
    )

    assert changed_basis.accepted is False
    assert "changed" in changed_basis.reason.lower()
