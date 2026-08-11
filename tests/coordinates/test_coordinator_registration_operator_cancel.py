from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    DesignActivationRequest,
    DesignSessionCheckpoint,
    FrameRecordsPublication,
    MachinePoseCaptureResult,
    RegistrationCaptureRequest,
    RegistrationCheckMarkRequest,
    RegistrationInvalidationRequest,
    RegistrationSourceMarkRequest,
    RegistrationSourceMarksRequest,
    RestoreOperatorAlignmentUiEffect,
)
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.registration_lifecycle import RegistrationCancellation
from probe_station_gui.views import main_window_connection_flow as connection_flow
from tests.coordinates.coordinator_registration_support import (
    _document,
    _draft,
    _loaded_coordinator,
    _machine_snapshot,
)


def _capture_first_operator_mark(coordinator) -> object:
    started = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
            operator_alignment=True,
            mark_index=0,
            operator_pick_generation=7,
        )
    )
    intent = started.intents[0]
    return coordinator.complete(
        CoordinateAdapterCompletion(
            intent.intent_id,
            MachinePoseCaptureResult(
                intent.intent_id,
                succeeded=True,
                snapshot=_machine_snapshot(3.0, 4.0, 0.0),
            ),
        )
    )


def _start_second_operator_mark(coordinator) -> object:
    started = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
            operator_alignment=True,
            mark_index=1,
            operator_pick_generation=8,
        )
    )
    return started.intents[0]


def test_coordinator_owns_registration_mark_and_invalidation_mutations(
    tmp_path,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, _draft(document))
    session.source_stage_marks = ((3.0, 4.0),)

    selected = coordinator.set_registration_source_mark(
        RegistrationSourceMarkRequest((10.0, 20.0), slot=0)
    )
    replaced = coordinator.replace_registration_source_marks(
        RegistrationSourceMarksRequest(((1.0, 2.0), (3.0, 4.0)))
    )
    checked = coordinator.add_registration_check_mark(
        RegistrationCheckMarkRequest((5.0, 6.0))
    )
    invalidated = coordinator.invalidate_registration(
        RegistrationInvalidationRequest("B authority changed.")
    )

    assert selected.snapshot.registration.source_stage_marks == ()
    assert replaced.snapshot.registration.source_design_marks == (
        (1.0, 2.0),
        (3.0, 4.0),
    )
    assert checked.snapshot.registration.check_design_marks == ((5.0, 6.0),)
    assert invalidated.snapshot.registration.registration_valid is False
    assert invalidated.snapshot.registration.registration_status == (
        "B authority changed."
    )


def _owner(events: list[object]) -> SimpleNamespace:
    layout = SimpleNamespace(
        set_alignment_capture_points=lambda points: events.append(
            ("layout", tuple(points))
        ),
        set_focus_candidate=lambda _candidate: None,
        set_selected_focus_point=lambda _point: None,
    )
    return SimpleNamespace(
        _alignment_design_draft=((0.0, 0.0), (1000.0, 0.0)),
        _alignment_stage_draft=[],
        _alignment_draft_fit_residuals=(1.0, 2.0),
        _manual_alignment_pick_slot=1,
        _manual_alignment_pick_generation=8,
        _set_design_snap_enabled=lambda enabled: events.append(("snap", enabled)),
        _refresh_manual_alignment_ui=lambda: events.append("manual"),
        _update_stage_coordinate_apply_state=lambda: events.append("apply"),
        _refresh_design_panel=lambda: events.append("panel"),
        _refresh_design_position=lambda: events.append("position"),
        _show_status=lambda _message, _duration=0: None,
        stage_controller=SimpleNamespace(
            request_machine_coordinate_snapshot=lambda *_args, **_kwargs: True
        ),
        design_layout_window=layout,
    )


def test_explicit_cancel_restores_empty_operator_baseline_once_and_stale_is_inert(
    monkeypatch,
    tmp_path,
) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, _draft(document))
    captured = _capture_first_operator_mark(coordinator)
    pending = _start_second_operator_mark(coordinator)
    events: list[object] = []
    owner = _owner(events)
    monkeypatch.setattr(
        connection_flow.stage_position_panel,
        "refresh_coordinate_frame_display",
        lambda _owner: None,
    )
    connection_flow.apply_coordinate_transition(owner, captured)
    assert owner._alignment_stage_draft == [(3.0, 4.0)]
    events.clear()

    cancelled = coordinator.cancel_registration(
        RegistrationCancellation.MARK_SET_CHANGED
    )

    expected = RestoreOperatorAlignmentUiEffect(
        design_marks=((0.0, 0.0), (1000.0, 0.0)),
        stage_marks=(),
    )
    assert cancelled.view_changed
    assert cancelled.ui_effects == (expected,)
    assert cancelled.snapshot.registration.operator_stage_marks == ()
    assert session.source_stage_marks == ()
    connection_flow.apply_coordinate_transition(owner, cancelled)
    assert owner._alignment_stage_draft == []
    assert events.count(("layout", expected.design_marks)) == 1
    assert events.count("manual") == 1

    replay = coordinator.cancel_registration(RegistrationCancellation.MARK_SET_CHANGED)
    stale = coordinator.complete(
        CoordinateAdapterCompletion(
            pending.intent_id,
            MachinePoseCaptureResult(
                pending.intent_id,
                succeeded=True,
                snapshot=_machine_snapshot(5.0, 6.0, 0.0),
            ),
        )
    )
    connection_flow.apply_coordinate_transition(owner, replay)
    connection_flow.apply_coordinate_transition(owner, stale)

    assert not replay.view_changed
    assert replay.ui_effects == ()
    assert not stale.view_changed
    assert stale.ui_effects == ()
    assert owner._alignment_stage_draft == []
    assert events.count(("layout", expected.design_marks)) == 1
    assert events.count("manual") == 1


def test_design_reactivation_carries_operator_baseline_restore_once(tmp_path) -> None:
    document = _document(tmp_path)
    coordinator, _registry, session = _loaded_coordinator(document, _draft(document))
    _capture_first_operator_mark(coordinator)
    pending = _start_second_operator_mark(coordinator)
    request = DesignActivationRequest(
        session_state=session.snapshot_state(),
        frame_metadata=DesignFrameMetadata.from_document(document),
        machine_snapshot=_machine_snapshot(1.0, 2.0, 0.0),
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
        requested_frame_id=session.active_frame_id,
    )

    activated = coordinator.activate_design(request)

    assert activated.view_changed
    assert activated.ui_effects == (
        RestoreOperatorAlignmentUiEffect(
            design_marks=((0.0, 0.0), (1000.0, 0.0)),
            stage_marks=(),
        ),
    )
    assert activated.snapshot.registration.operator_stage_marks == ()

    replay = coordinator.activate_design(request)
    stale = coordinator.complete(
        CoordinateAdapterCompletion(
            pending.intent_id,
            MachinePoseCaptureResult(
                pending.intent_id,
                succeeded=True,
                snapshot=_machine_snapshot(5.0, 6.0, 0.0),
            ),
        )
    )
    assert replay.ui_effects == ()
    assert stale.ui_effects == ()


def test_changed_frame_version_restores_superseded_operator_draft_once(
    monkeypatch,
    tmp_path,
) -> None:
    document = _document(tmp_path)
    coordinator, registry, session = _loaded_coordinator(document, _draft(document))
    captured = _capture_first_operator_mark(coordinator)
    stale = _start_second_operator_mark(coordinator)
    current = registry.get(session.active_frame_id)
    assert current is not None
    newer = replace(current, version=current.version + 1)
    projection = session.prepare_active_frame_link(
        newer,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    coordinator._publish_frame_records(
        FrameRecordsPublication.for_committed_record(
            registry.snapshot().records,
            newer,
            previous_record=current,
            previous_session=DesignSessionCheckpoint.capture(session),
            projection=projection,
        )
    )
    events: list[object] = []
    owner = _owner(events)
    monkeypatch.setattr(
        connection_flow.stage_position_panel,
        "refresh_coordinate_frame_display",
        lambda _owner: None,
    )
    connection_flow.apply_coordinate_transition(owner, captured)
    assert owner._alignment_stage_draft == [(3.0, 4.0)]
    events.clear()

    replacement = coordinator.capture_registration_mark(
        RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
            operator_alignment=True,
            mark_index=1,
            operator_pick_generation=9,
        )
    )

    expected = RestoreOperatorAlignmentUiEffect(
        design_marks=((0.0, 0.0), (1000.0, 0.0)),
        stage_marks=(),
    )
    assert replacement.view_changed
    assert replacement.ui_effects == (expected,)
    connection_flow.apply_coordinate_transition(owner, replacement)
    assert owner._alignment_stage_draft == []
    assert events.count(("layout", expected.design_marks)) == 1

    late = coordinator.complete(
        CoordinateAdapterCompletion(
            stale.intent_id,
            MachinePoseCaptureResult(
                stale.intent_id,
                succeeded=True,
                snapshot=_machine_snapshot(5.0, 6.0, 0.0),
            ),
        )
    )
    assert not late.view_changed
    assert late.ui_effects == ()
    current_intent = replacement.intents[0]
    current_result = coordinator.complete(
        CoordinateAdapterCompletion(
            current_intent.intent_id,
            MachinePoseCaptureResult(
                current_intent.intent_id,
                succeeded=True,
                snapshot=_machine_snapshot(7.0, 8.0, 0.0),
            ),
        )
    )
    assert current_result.snapshot.registration.operator_stage_marks == (
        None,
        (7.0, 8.0),
    )
