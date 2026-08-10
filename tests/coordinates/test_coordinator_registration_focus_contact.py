from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import probe_station_gui.coordinates.coordinator_model as coordinator_model
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    MachinePoseCaptureResult,
    RegistrationCaptureRequest,
    SaveCoordinateFramesIntent,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameStoreFailure,
    CoordinateFrameStoreSuccess,
)
from probe_station_gui.design.focus_candidate import FocusCandidate
from probe_station_gui.design.frame_registration import (
    commit_xyb_registration,
    set_contact_reference,
    set_focus_reference,
)
from probe_station_gui.design.session import DesignSession
from tests.coordinates.coordinator_registration_support import (
    _document,
    _draft,
    _loaded_coordinator,
    _machine_snapshot,
)


def test_focus_move_autofocus_and_durable_z_commit_are_one_ordered_workflow(
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
    optical_type = getattr(coordinator_model, "RegistrationOpticalObservation")
    candidate_request_type = getattr(coordinator_model, "FocusCandidateRequest")
    reference_request_type = getattr(coordinator_model, "FocusReferenceRequest")
    move_intent_type = getattr(coordinator_model, "MoveToFocusTargetIntent")
    move_result_type = getattr(coordinator_model, "FocusMoveResult")
    autofocus_intent_type = getattr(coordinator_model, "RunAutofocusIntent")
    autofocus_result_type = getattr(coordinator_model, "AutofocusResult")
    optical = optical_type(
        fov_size=(0.5, 0.5),
        objective_name="5x",
        optical_calibration_identity="5x-calibration",
    )
    candidate = FocusCandidate(
        center=(0.0, 0.0),
        bounds=(-0.1, -0.1, 0.1, 0.1),
        distance_from_design_center=0.0,
    )

    offered = coordinator.offer_focus_candidate(
        candidate_request_type(candidate=candidate, optical=optical)
    )
    assert offered.snapshot.registration.focus_candidate == candidate
    started = coordinator.use_focus_reference(
        reference_request_type(
            design_point=candidate.center,
            optical=optical,
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    assert len(started.intents) == 1
    move = started.intents[0]
    assert isinstance(move, move_intent_type)
    assert move.target_xy == pytest.approx((1.0, 2.0))

    moved = coordinator.complete(
        CoordinateAdapterCompletion(
            move.intent_id,
            move_result_type(
                move.intent_id,
                succeeded=True,
                completed_target_xy=move.target_xy,
            ),
        )
    )
    assert len(moved.intents) == 1
    autofocus = moved.intents[0]
    assert isinstance(autofocus, autofocus_intent_type)
    focused = coordinator.complete(
        CoordinateAdapterCompletion(
            autofocus.intent_id,
            autofocus_result_type(
                autofocus.intent_id,
                succeeded=True,
                physical_z_mm=6.25,
            ),
        )
    )
    assert len(focused.intents) == 1
    save = focused.intents[0]
    assert isinstance(save, SaveCoordinateFramesIntent)
    assert focused.notices == ()
    current = registry.get(registered.frame_id)
    assert current is not None and current.transform is not None
    assert current.transform.z_zero_machine_mm == pytest.approx(6.25)
    assert current.transform.a_zero_machine_mm is None
    assert current.readiness["A"].available is False
    assert focused.snapshot.registration.focus_candidate is None

    acknowledged = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreSuccess(save.intent_id, "save"),
        )
    )
    assert [notice.message for notice in acknowledged.notices] == [
        "Focus reference ready."
    ]


def test_focus_target_uses_request_b_and_pivot_instead_of_session_projection(
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
    coordinator, _registry, session = _loaded_coordinator(document, registered)
    session.link_active_frame(
        registered,
        machine_point_for_navigation=lambda point: point,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    optical = coordinator_model.RegistrationOpticalObservation(
        fov_size=(0.5, 0.5),
        objective_name="5x",
        optical_calibration_identity="5x-calibration",
    )
    candidate = FocusCandidate(
        center=(0.0, 0.0),
        bounds=(-0.1, -0.1, 0.1, 0.1),
        distance_from_design_center=0.0,
    )
    coordinator.offer_focus_candidate(
        coordinator_model.FocusCandidateRequest(candidate, optical)
    )

    started = coordinator.use_focus_reference(
        coordinator_model.FocusReferenceRequest(
            design_point=candidate.center,
            optical=optical,
            machine_snapshot=_machine_snapshot(28.0, 11.0, 90.0),
            pivot_machine_xy=(10.0, 20.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )

    assert len(started.intents) == 1
    move = started.intents[0]
    assert isinstance(move, coordinator_model.MoveToFocusTargetIntent)
    assert move.target_xy == pytest.approx((28.0, 11.0))


def test_focus_move_completion_with_wrong_actual_target_is_inert(
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
    optical = coordinator_model.RegistrationOpticalObservation(
        fov_size=(0.5, 0.5),
        objective_name="5x",
        optical_calibration_identity="5x-calibration",
    )
    candidate = FocusCandidate(
        center=(0.0, 0.0),
        bounds=(-0.1, -0.1, 0.1, 0.1),
        distance_from_design_center=0.0,
    )
    coordinator.offer_focus_candidate(
        coordinator_model.FocusCandidateRequest(candidate, optical)
    )
    started = coordinator.use_focus_reference(
        coordinator_model.FocusReferenceRequest(
            design_point=candidate.center,
            optical=optical,
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    move = started.intents[0]

    stale = coordinator.complete(
        CoordinateAdapterCompletion(
            move.intent_id,
            coordinator_model.FocusMoveResult(
                move.intent_id,
                succeeded=True,
                completed_target_xy=(99.0, 101.0),
            ),
        )
    )

    assert stale.intents == ()
    assert registry.get(registered.frame_id) == registered


def test_optical_context_change_clears_only_focus_and_keeps_capture_live(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(
        document,
        _draft(document),
    )
    optical = coordinator_model.RegistrationOpticalObservation(
        fov_size=(0.5, 0.5),
        objective_name="5x",
        optical_calibration_identity="calibration-a",
    )
    candidate = FocusCandidate(
        center=(0.0, 0.0),
        bounds=(-0.1, -0.1, 0.1, 0.1),
        distance_from_design_center=0.0,
    )
    coordinator.offer_focus_candidate(
        coordinator_model.FocusCandidateRequest(candidate, optical)
    )
    coordinator.observe_focus_context(optical)
    capture = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    ).intents[0]

    changed = coordinator.observe_focus_context(
        replace(
            optical,
            fov_size=(0.25, 0.25),
            optical_calibration_identity="calibration-b",
        )
    )

    assert not changed.view_changed
    assert changed.snapshot.registration.focus_candidate is None
    captured = coordinator.complete(
        CoordinateAdapterCompletion(
            capture.intent_id,
            MachinePoseCaptureResult(
                capture.intent_id,
                True,
                _machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )
    assert [notice.code for notice in captured.notices] == [
        "registration_capture_updated"
    ]
    assert session.source_stage_marks == ((3.0, 4.0),)


def test_optical_context_change_does_not_cancel_pending_first_contact(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    focused = set_focus_reference(
        commit_xyb_registration(
            _draft(document),
            design_points=((0.0, 0.0), (1.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        ),
        physical_machine_z_mm=6.0,
    )
    coordinator, _registry, session = _loaded_coordinator(document, focused)
    session.link_active_frame(focused)
    lease = coordinator_model.FirstContactRequest(
        focused.frame_id,
        focused.version,
    )
    read = coordinator.arm_first_contact(lease).intents[0]
    optical = coordinator_model.RegistrationOpticalObservation(
        fov_size=(0.5, 0.5),
        objective_name="5x",
        optical_calibration_identity="calibration-a",
    )
    coordinator.observe_focus_context(optical)

    coordinator.observe_focus_context(
        replace(optical, objective_name="20x", fov_size=(0.1, 0.1))
    )
    contacted = coordinator.complete(
        CoordinateAdapterCompletion(
            read.intent_id,
            coordinator_model.PhysicalAReadResult(
                read.intent_id,
                succeeded=True,
                physical_a_mm=8.0,
            ),
        )
    )

    assert len(contacted.intents) == 1
    assert isinstance(contacted.intents[0], SaveCoordinateFramesIntent)


def test_resetting_z_invalidates_dependent_a_in_the_same_publication(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    contacted = set_contact_reference(
        set_focus_reference(
            commit_xyb_registration(
                _draft(document),
                design_points=((0.0, 0.0), (1.0, 0.0)),
                physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
                physical_b_deg=0.0,
                pivot_machine_xy=(0.0, 0.0),
            ),
            physical_machine_z_mm=6.0,
        ),
        physical_machine_a_mm=8.0,
    )
    coordinator, registry, session = _loaded_coordinator(document, contacted)
    session.link_active_frame(contacted)
    request_type = getattr(coordinator_model, "FocusReferenceResetRequest")

    transition = coordinator.reset_focus_reference(
        request_type(
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
            reason="Focus reference reset.",
        )
    )

    assert len(transition.intents) == 1
    assert isinstance(transition.intents[0], SaveCoordinateFramesIntent)
    reset = registry.get(contacted.frame_id)
    assert reset is not None and reset.transform is not None
    assert reset.transform.z_zero_machine_mm is None
    assert reset.transform.a_zero_machine_mm is None
    assert not reset.readiness["Z"].available
    assert not reset.readiness["A"].available


def test_failed_focus_reset_preparation_preserves_pending_focus_move(
    monkeypatch,
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
    optical = coordinator_model.RegistrationOpticalObservation(
        fov_size=(0.5, 0.5),
        objective_name="5x",
        optical_calibration_identity="5x-calibration",
    )
    candidate = FocusCandidate(
        center=(0.0, 0.0),
        bounds=(-0.1, -0.1, 0.1, 0.1),
        distance_from_design_center=0.0,
    )
    coordinator.offer_focus_candidate(
        coordinator_model.FocusCandidateRequest(candidate, optical)
    )
    move = coordinator.use_focus_reference(
        coordinator_model.FocusReferenceRequest(
            design_point=candidate.center,
            optical=optical,
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    ).intents[0]
    baseline = session.snapshot_state()
    records = registry.snapshot().records
    monkeypatch.setattr(
        DesignSession,
        "prepare_active_frame_link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection")),
    )

    rejected = coordinator.reset_focus_reference(
        coordinator_model.FocusReferenceResetRequest(
            machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
            reason="Focus reference reset.",
        )
    )

    assert rejected.notices and "projection" in rejected.notices[0].message
    assert rejected.snapshot.registration.focus_candidate == candidate
    assert session.snapshot_state() == baseline
    assert registry.snapshot().records == records
    moved = coordinator.complete(
        CoordinateAdapterCompletion(
            move.intent_id,
            coordinator_model.FocusMoveResult(
                move.intent_id,
                succeeded=True,
                completed_target_xy=move.target_xy,
            ),
        )
    )
    assert len(moved.intents) == 1
    assert isinstance(moved.intents[0], coordinator_model.RunAutofocusIntent)


def test_first_contact_is_single_write_and_exact_save_rollback_rearms_it(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    focused = set_focus_reference(
        commit_xyb_registration(
            _draft(document),
            design_points=((0.0, 0.0), (1.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        ),
        physical_machine_z_mm=6.0,
    )
    coordinator, registry, session = _loaded_coordinator(document, focused)
    session.link_active_frame(
        focused,
        machine_point_for_navigation=lambda point: point,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    request_type = getattr(coordinator_model, "FirstContactRequest")
    result_type = getattr(coordinator_model, "PhysicalAReadResult")
    lease = request_type(frame_id=focused.frame_id, frame_version=focused.version)

    read = coordinator.arm_first_contact(lease).intents[0]
    contact = coordinator.complete(
        CoordinateAdapterCompletion(
            read.intent_id,
            result_type(read.intent_id, succeeded=True, physical_a_mm=8.0),
        )
    )
    assert len(contact.intents) == 1
    save = contact.intents[0]
    assert isinstance(save, SaveCoordinateFramesIntent)
    current = registry.get(focused.frame_id)
    assert current is not None and current.transform is not None
    assert current.transform.a_zero_machine_mm == pytest.approx(8.0)
    assert coordinator.arm_first_contact(lease).intents == ()

    failed = coordinator.complete(
        CoordinateAdapterCompletion(
            save.intent_id,
            CoordinateFrameStoreFailure(save.intent_id, "save", "disk full"),
        )
    )
    assert failed.notices
    restored = registry.get(focused.frame_id)
    assert restored == focused
    retry = coordinator.arm_first_contact(lease)
    assert len(retry.intents) == 1


def test_interrupted_physical_a_completion_cannot_commit_or_publish_contact(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    focused = set_focus_reference(
        commit_xyb_registration(
            _draft(document),
            design_points=((0.0, 0.0), (1.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        ),
        physical_machine_z_mm=6.0,
    )
    coordinator, registry, session = _loaded_coordinator(document, focused)
    session.link_active_frame(
        focused,
        machine_point_for_navigation=lambda point: point,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    request_type = getattr(coordinator_model, "FirstContactRequest")
    intent_type = getattr(coordinator_model, "ReadPhysicalAIntent")
    result_type = getattr(coordinator_model, "PhysicalAReadResult")

    armed = coordinator.arm_first_contact(
        request_type(frame_id=focused.frame_id, frame_version=focused.version)
    )
    assert len(armed.intents) == 1
    read = armed.intents[0]
    assert isinstance(read, intent_type)

    interrupted = coordinator.complete(
        CoordinateAdapterCompletion(
            read.intent_id,
            result_type(
                read.intent_id,
                succeeded=True,
                physical_a_mm=8.0,
                interrupted=True,
            ),
        )
    )

    assert interrupted.intents == ()
    assert interrupted.notices == ()
    current = registry.get(focused.frame_id)
    assert current is not None and current.transform is not None
    assert current.transform.a_zero_machine_mm is None
    assert current.readiness["A"].available is False
