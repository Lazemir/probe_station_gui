from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    FrameRecordsPublication,
    MachinePoseCaptureResult,
    MachineProfileObservation,
    RegistrationCaptureRequest,
    RestoreOperatorAlignmentUiEffect,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameLoadResult,
)
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.design.frame_registration import new_design_frame_draft
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.session import DesignSession
from probe_station_gui.settings.axis_calibration_config import (
    default_axis_calibrations,
)
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot
from probe_station_gui.stage.types import _Status
from probe_station_gui.views import main_window_connection_flow as connection_flow


class _MissingAxisPose:
    def require(self, axis: str) -> float:
        raise KeyError(axis)


class _ConversionFailureSnapshot:
    physical_machine_pose = _MissingAxisPose()


class _ExplodingSnapshot:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"stale snapshot was inspected through {name}")


def _document(tmp_path: Path) -> DesignDocument:
    source = tmp_path / "rollback-rendering.gds"
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


def _machine_snapshot(x_value: float, y_value: float) -> MachineCoordinateSnapshot:
    machine = (x_value, y_value, 0.0, 0.0, 0.0, 0.0)
    offset = (0.0,) * len(machine)
    axis_index = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}
    mapper = StageAxisCalibrationMapper(
        calibrations=default_axis_calibrations(),
        position_reporting_mode="work",
        active_work_coordinate_system="G54",
        controller_coordinate_offsets={"G54": offset},
        axis_index=axis_index,
    )
    return MachineCoordinateSnapshot.from_status(
        _Status(
            state="Idle",
            synchronized_machine_position=machine,
            display_position=machine,
            work_position=machine,
            work_offset=offset,
            coordinate_system="G54",
        ),
        mapper,
        axis_index,
    )


def _loaded_coordinator(
    document: DesignDocument,
) -> tuple[CoordinateSystemCoordinator, CoordinateFrameRegistry, DesignSession]:
    record = new_design_frame_draft(document, existing_names=())
    registry = CoordinateFrameRegistry()
    session = DesignSession(document=document)
    coordinator = CoordinateSystemCoordinator(registry=registry, session=session)
    load = coordinator.start(MachineProfileObservation("profile-a")).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            load.intent_id,
            CoordinateFrameLoadResult(
                load.intent_id,
                CoordinateFrameDocument(records=(record,)),
            ),
        )
    )
    session.active_frame_id = record.frame_id
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    return coordinator, registry, session


def _capture(
    coordinator: CoordinateSystemCoordinator,
    snapshot: object,
    *,
    request: RegistrationCaptureRequest | None = None,
) -> object:
    started = coordinator.capture_registration_mark(
        request
        or RegistrationCaptureRequest(
            pivot_machine_xy=(0.0, 0.0),
            objective_xy_offset=(0.0, 0.0),
        )
    )
    intent = started.intents[0]
    return coordinator.complete(
        CoordinateAdapterCompletion(
            intent.intent_id,
            MachinePoseCaptureResult(
                intent.intent_id,
                succeeded=True,
                snapshot=snapshot,
            ),
        )
    )


def _render_non_operator_transition(monkeypatch, transition: object) -> list[str]:
    events: list[str] = []
    owner = SimpleNamespace(
        _coordinate_frames_loaded=True,
        _refresh_design_panel=lambda: events.append("panel"),
        _refresh_design_position=lambda: events.append("position"),
        _show_status=lambda _message, _duration=0: events.append("status"),
    )
    monkeypatch.setattr(
        connection_flow.stage_position_panel,
        "refresh_coordinate_frame_display",
        lambda _owner: events.append("coordinates"),
    )

    connection_flow.apply_coordinate_transition(owner, transition)

    return events


def test_conversion_failure_restores_tentative_batch_and_starts_clean(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator, registry, session = _loaded_coordinator(_document(tmp_path))
    baseline = session.snapshot_state()
    records = registry.snapshot().records
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )
    _capture(coordinator, _machine_snapshot(3.0, 4.0), request=request)
    assert session.source_stage_marks_compact() == [(3.0, 4.0)]

    started = coordinator.capture_registration_mark(request)
    intent = started.intents[0]
    completion = CoordinateAdapterCompletion(
        intent.intent_id,
        MachinePoseCaptureResult(
            intent.intent_id,
            succeeded=True,
            snapshot=_ConversionFailureSnapshot(),
        ),
    )
    failed = coordinator.complete(completion)

    assert failed.intents == ()
    assert failed.notices
    assert failed.view_changed
    assert failed.ui_effects == ()
    assert session.snapshot_state() == baseline
    assert registry.snapshot().records == records
    rendered = _render_non_operator_transition(monkeypatch, failed)
    assert rendered.count("panel") == 1
    assert rendered.count("position") == 1
    replay = coordinator.complete(completion)
    assert replay.notices == ()
    assert not replay.view_changed
    assert replay.ui_effects == ()
    assert session.snapshot_state() == baseline
    fresh = _capture(coordinator, _machine_snapshot(5.0, 6.0), request=request)
    assert fresh.intents == ()
    assert session.source_stage_marks_compact() == [(5.0, 6.0)]


@pytest.mark.parametrize("failure_kind", ("fit", "preparation"))
def test_non_operator_commit_rollback_refreshes_design_views_once(
    failure_kind: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator, registry, session = _loaded_coordinator(_document(tmp_path))
    baseline = session.snapshot_state()
    records = registry.snapshot().records
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )
    _capture(coordinator, _machine_snapshot(3.0, 4.0), request=request)
    second_snapshot = _machine_snapshot(3.0, 4.0)
    if failure_kind == "preparation":
        second_snapshot = _machine_snapshot(4.0, 4.0)
        monkeypatch.setattr(
            DesignSession,
            "prepare_active_frame_link",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("preparation failed")
            ),
        )

    failed = _capture(coordinator, second_snapshot, request=request)

    assert failed.intents == ()
    assert failed.notices
    assert failed.view_changed
    assert failed.ui_effects == ()
    assert session.snapshot_state() == baseline
    assert registry.snapshot().records == records
    events = _render_non_operator_transition(monkeypatch, failed)
    assert events.count("panel") == 1
    assert events.count("position") == 1


def test_operator_rollback_renders_an_exact_empty_stage_draft(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator, _registry, session = _loaded_coordinator(_document(tmp_path))
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
        operator_alignment=True,
        mark_index=0,
        operator_pick_generation=7,
    )
    _capture(coordinator, _machine_snapshot(3.0, 4.0), request=request)

    failed = _capture(
        coordinator,
        _ConversionFailureSnapshot(),
        request=replace(request, mark_index=1, operator_pick_generation=8),
    )

    expected = RestoreOperatorAlignmentUiEffect(
        design_marks=((0.0, 0.0), (1000.0, 0.0)),
        stage_marks=(),
    )
    assert failed.view_changed
    assert failed.ui_effects == (expected,)
    assert session.source_stage_marks == ()

    events: list[object] = []
    layout = SimpleNamespace(
        set_alignment_capture_points=lambda points: events.append(
            ("layout", tuple(points))
        ),
        set_focus_candidate=lambda _candidate: None,
        set_selected_focus_point=lambda _point: None,
    )
    owner = SimpleNamespace(
        _coordinate_frames_loaded=True,
        _alignment_design_draft=((0.0, 0.0), (1000.0, 0.0)),
        _alignment_stage_draft=[(3.0, 4.0)],
        _alignment_draft_fit_residuals=(1.0, 2.0),
        _manual_alignment_pick_slot=1,
        _manual_alignment_pick_generation=99,
        _set_design_snap_enabled=lambda enabled: events.append(("snap", enabled)),
        _refresh_manual_alignment_ui=lambda: events.append("manual"),
        _update_stage_coordinate_apply_state=lambda: events.append("apply"),
        _refresh_design_panel=lambda: events.append("panel"),
        _refresh_design_position=lambda: events.append("position"),
        _show_status=lambda _message, _duration=0: None,
        design_layout_window=layout,
    )
    monkeypatch.setattr(
        connection_flow.stage_position_panel,
        "refresh_coordinate_frame_display",
        lambda _owner: events.append("coordinates"),
    )

    connection_flow.apply_coordinate_transition(owner, failed)

    assert owner._alignment_stage_draft == []
    assert events.count(("layout", expected.design_marks)) == 1
    assert events.count("manual") == 1
    assert events.count("apply") == 1
    assert events.count("panel") == 1
    assert events.count("position") == 1


def test_stale_capture_rollback_renders_once_and_replay_is_inert(
    monkeypatch,
    tmp_path: Path,
) -> None:
    coordinator, registry, session = _loaded_coordinator(_document(tmp_path))
    request = RegistrationCaptureRequest(
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )
    baseline = session.snapshot_state()
    _capture(coordinator, _machine_snapshot(3.0, 4.0), request=request)
    started = coordinator.capture_registration_mark(request)
    intent = started.intents[0]
    current = registry.get(session.active_frame_id)
    assert current is not None
    coordinator.publish_frame_records(
        FrameRecordsPublication.for_committed_record(
            registry.snapshot().records,
            replace(current, version=current.version + 1),
            previous_record=current,
        )
    )
    completion = CoordinateAdapterCompletion(
        intent.intent_id,
        MachinePoseCaptureResult(
            intent.intent_id,
            succeeded=True,
            snapshot=_ExplodingSnapshot(),
        ),
    )

    stale = coordinator.complete(completion)
    replay = coordinator.complete(completion)

    assert stale.view_changed
    assert stale.ui_effects == ()
    assert session.source_stage_marks == baseline.source_stage_marks
    assert not replay.view_changed
    assert replay.ui_effects == ()
    assert _render_non_operator_transition(monkeypatch, stale) == [
        "coordinates",
        "panel",
        "position",
    ]
    assert _render_non_operator_transition(monkeypatch, replay) == ["coordinates"]
