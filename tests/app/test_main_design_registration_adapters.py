from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from probe_station_gui.application import api_meter_visa, api_route_scan, design_markup
from probe_station_gui.application.alignment import _ManualAlignmentCaptureContext
from probe_station_gui.coordinates.coordinator_model import (
    AutofocusResult,
    CoordinateAdapterCompletion,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    FirstContactRequest,
    FocusMoveResult,
    MachinePoseCaptureResult,
    PhysicalAReadResult,
    ReadPhysicalAIntent,
    RegistrationCaptureRequest,
    RegistrationOpticalObservation,
    RegistrationWorkflowSnapshot,
)
from probe_station_gui.design.session import DesignSession
from tests.app.test_main_design_navigation import Main, _make_document, main_module


def test_machine_capture_signal_is_only_translated_to_typed_completion(
    monkeypatch,
) -> None:
    completions: list[CoordinateAdapterCompletion] = []
    rendered: list[CoordinateTransition] = []
    sentinel = object()
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    window = Main.__new__(Main)
    window._manual_alignment_pick_slot = 1
    window._manual_alignment_pick_generation = 9
    window._coordinate_system_coordinator = types.SimpleNamespace(
        complete=lambda completion: completions.append(completion) or transition
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda owner, value: rendered.append(value),
    )

    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        -7,
        True,
        sentinel,
        "captured",
    )

    assert len(completions) == 1
    completion = completions[0]
    assert completion.intent_id == -7
    assert completion.result == MachinePoseCaptureResult(
        -7,
        succeeded=True,
        snapshot=sentinel,
        message="captured",
        active_operator_pick_slot=1,
        active_operator_pick_generation=9,
    )
    assert rendered == [transition]


def test_design_unload_closes_coordinate_owner_before_workspace_adoption(
    monkeypatch,
) -> None:
    events: list[str] = []
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    plan = types.SimpleNamespace(accepted=True)
    window = Main.__new__(Main)
    window._stage_motion = types.SimpleNamespace(
        discard_alignment_rotation=lambda: False
    )
    window._design_mutation_ready = lambda: True
    window._design_load_generation = 1
    window._design_session = types.SimpleNamespace(
        document=types.SimpleNamespace(path=Path("chip.gds")),
        apply_state=lambda _state: events.append("workspace-adopt"),
    )
    candidate = types.SimpleNamespace(snapshot_state=lambda: object())
    window._snapshot_design_session = lambda: candidate
    window._coordinate_system_coordinator = types.SimpleNamespace(
        close_design=lambda: events.append("coordinate-close") or transition
    )
    window._delete_persisted_design_markup = lambda _path: None
    window._design_markup_load_request_id = None
    window._design_markup_load_contexts = {}
    window._design_markup = object()
    window._design_markup_direct_guide_ids = ["guide"]
    window._design_markup_pending_visibility = object()
    window._reset_manual_alignment = lambda **_kwargs: None
    window._last_selected_design_point = (1.0, 2.0)
    window._refresh_design_panel = lambda: None
    window._update_design_position = lambda _value: None
    window._show_navigation_status = lambda _plan: None

    def unload_candidate(session: object) -> object:
        assert session is candidate
        events.append("candidate-unload")
        return plan

    monkeypatch.setattr(
        design_markup.route_editing,
        "unload_design_document",
        unload_candidate,
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda _owner, _transition: None,
    )

    Main._unload_design_document(window)

    assert events == ["candidate-unload", "coordinate-close", "workspace-adopt"]


def test_resolved_image_alignment_capture_submits_typed_request(monkeypatch) -> None:
    requests: list[RegistrationCaptureRequest] = []
    rendered: list[CoordinateTransition] = []
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    window = Main.__new__(Main)
    window._alignment_design_draft = [(0.0, 0.0), (1.0, 0.0)]
    window._manual_alignment_pick_slot = 0
    window._manual_alignment_pick_generation = 4
    window._manual_alignment_capture_context = _ManualAlignmentCaptureContext(
        request_id="image-1",
        slot=0,
        cancelled=main_module.threading.Event(),
    )
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=(3.0, 5.0)
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda _point: (-0.5, 0.25)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        capture_registration_mark=lambda request: requests.append(request) or transition
    )
    window._update_coordinate_display = lambda **_kwargs: None
    window._refresh_manual_alignment_ui = lambda: None
    window._update_stage_coordinate_apply_state = lambda: None
    window._show_status = lambda *_args: None
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda _owner, value: rendered.append(value),
    )

    Main._on_manual_alignment_point_resolved(
        window,
        "image-1",
        True,
        (0.0, 0.0),
        (7.0, 8.0),
        "",
    )

    assert requests == [
        RegistrationCaptureRequest(
            pivot_machine_xy=(3.0, 5.0),
            objective_xy_offset=(0.5, -0.25),
            operator_alignment=True,
            mark_index=0,
            configured_target_xy=(7.0, 8.0),
            capture_source="image",
            operator_pick_generation=4,
        )
    ]
    assert rendered == [transition]


def test_armed_crosshair_capture_submits_no_b_motion(monkeypatch) -> None:
    requests: list[RegistrationCaptureRequest] = []
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    window = Main.__new__(Main)
    window._alignment_design_draft = [(0.0, 0.0), (1.0, 0.0)]
    window._manual_alignment_pick_slot = 1
    window._manual_alignment_pick_generation = 8
    window._manual_alignment_capture_context = None
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=(2.0, 4.0)
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda _point: (0.0, 0.0)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        capture_registration_mark=lambda request: requests.append(request) or transition
    )
    window.stage_controller = types.SimpleNamespace(
        request_rotate_b=lambda *_args: pytest.fail("crosshair capture moved B")
    )
    window._show_status = lambda *_args: None
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )

    Main._capture_manual_alignment_center(window, 1)

    assert requests == [
        RegistrationCaptureRequest(
            pivot_machine_xy=(2.0, 4.0),
            objective_xy_offset=(-0.0, -0.0),
            operator_alignment=True,
            mark_index=1,
            configured_target_xy=None,
            capture_source="center",
            operator_pick_generation=8,
        )
    ]


def test_rejected_fresh_frame_keeps_source_mark_and_ui_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((1.0, 2.0), (3.0, 4.0))
    session.source_stage_marks = ((5.0, 6.0), (7.0, 8.0))
    window = Main.__new__(Main)
    window._design_session = session
    window._manual_alignment_pick_slot = 1
    window._manual_alignment_points = [(1.0, 2.0), (3.0, 4.0)]
    window._stage_motion = types.SimpleNamespace(
        discard_alignment_rotation=lambda: (_ for _ in ()).throw(
            AssertionError("rejected activation discarded alignment correlation")
        )
    )
    window._last_selected_design_point = (9.0, 10.0)
    calls: list[object] = []
    window._coordinate_system_coordinator = types.SimpleNamespace(
        cancel_registration=lambda _reason: (_ for _ in ()).throw(
            AssertionError("rejected activation cancelled current evidence")
        )
    )
    window._set_design_snap_enabled = lambda value: calls.append(("snap", value))
    window._refresh_design_panel = lambda: calls.append("panel")
    window._set_alignment_panel_expanded = lambda: calls.append("alignment")
    window._show_status = lambda *args: calls.append(("status", args))
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            accepted=False,
            notices=(),
        ),
    )

    Main._on_design_layout_point_selected(window, 0, 11.0, 12.0)

    assert session.source_design_marks == ((1.0, 2.0), (3.0, 4.0))
    assert session.source_stage_marks == ((5.0, 6.0), (7.0, 8.0))
    assert window._manual_alignment_pick_slot == 1
    assert window._manual_alignment_points == [(1.0, 2.0), (3.0, 4.0)]
    assert window._last_selected_design_point == (9.0, 10.0)
    assert calls == []


def test_rejected_clear_registration_keeps_current_ui_state(monkeypatch) -> None:
    window = Main.__new__(Main)
    window._stage_motion = types.SimpleNamespace(
        discard_alignment_rotation=lambda: (_ for _ in ()).throw(
            AssertionError("rejected activation discarded alignment correlation")
        )
    )
    window._last_selected_design_point = (9.0, 10.0)
    calls: list[object] = []
    window._set_design_snap_enabled = lambda value: calls.append(("snap", value))
    window._refresh_design_panel = lambda: calls.append("panel")
    window._refresh_design_position = lambda: calls.append("position")
    window._show_status = lambda *args: calls.append(("status", args))
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            accepted=False,
            notices=(),
        ),
    )

    Main._clear_design_registration(window)

    assert window._last_selected_design_point == (9.0, 10.0)
    assert calls == []


def test_accepted_clear_registration_restarts_ui(monkeypatch) -> None:
    window = Main.__new__(Main)
    window._stage_motion = types.SimpleNamespace(
        discard_alignment_rotation=lambda: False
    )
    window._last_selected_design_point = (9.0, 10.0)
    calls: list[object] = []
    window._set_design_snap_enabled = lambda value: calls.append(("snap", value))
    window._refresh_design_panel = lambda: calls.append("panel")
    window._refresh_design_position = lambda: calls.append("position")
    window._show_status = lambda *args: calls.append(("status", args))
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            accepted=True,
            notices=(),
        ),
    )

    Main._clear_design_registration(window)

    assert window._last_selected_design_point is None
    assert calls == [
        ("snap", True),
        "panel",
        "position",
        ("status", ("Design calibration restarted.", 4000)),
    ]


def test_focus_callbacks_translate_only_typed_results(monkeypatch) -> None:
    completions: list[CoordinateAdapterCompletion] = []
    rendered: list[CoordinateTransition] = []
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    window = Main.__new__(Main)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        complete=lambda completion: completions.append(completion) or transition
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda _owner, value: rendered.append(value),
    )

    Main._on_registration_focus_move_finished(window, 21, (4.0, 5.0), True, "moved")
    Main._on_registration_focus_autofocus_finished(window, 22, True, 3.25, "focused")

    assert completions == [
        CoordinateAdapterCompletion(
            21,
            FocusMoveResult(
                21,
                True,
                "moved",
                completed_target_xy=(4.0, 5.0),
            ),
        ),
        CoordinateAdapterCompletion(
            22,
            AutofocusResult(22, True, physical_z_mm=3.25, message="focused"),
        ),
    ]
    assert rendered == [transition, transition]


def test_focus_move_signal_uses_only_its_tracked_token_completion(monkeypatch) -> None:
    completions: list[CoordinateAdapterCompletion] = []
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    window = Main.__new__(Main)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        complete=lambda completion: completions.append(completion) or transition
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )

    Main._on_registration_focus_move_signal(
        window,
        21,
        (4.0, 5.0),
        True,
        "moved",
    )

    assert completions == [
        CoordinateAdapterCompletion(
            21,
            FocusMoveResult(21, True, "moved", completed_target_xy=(4.0, 5.0)),
        )
    ]


def test_main_observes_focus_context_without_owning_lease_policy(
    monkeypatch,
) -> None:
    optical = RegistrationOpticalObservation(
        fov_size=(0.5, 0.5),
        objective_name="5x",
        optical_calibration_identity="calibration-a",
    )
    transition = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None),
    )
    observations: list[RegistrationOpticalObservation] = []
    rendered: list[CoordinateTransition] = []
    window = Main.__new__(Main)
    window._registration_optical_observation = lambda: optical
    window._coordinate_system_coordinator = types.SimpleNamespace(
        observe_focus_context=lambda value: observations.append(value) or transition
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda _owner, value: rendered.append(value),
    )

    Main._observe_design_focus_context(window)

    assert observations == [optical]
    assert rendered == [transition]
    assert not hasattr(window, "_design_focus_overlay_context")


def test_contact_worker_queues_typed_result_before_gui_completion(monkeypatch) -> None:
    requests: list[FirstContactRequest] = []
    completions: list[CoordinateAdapterCompletion] = []
    rendered: list[CoordinateTransition] = []
    queued: list[PhysicalAReadResult] = []
    registration = RegistrationWorkflowSnapshot(
        active_frame_id="frame-1",
        active_frame_version=7,
    )
    armed = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None, registration=registration),
        intents=(ReadPhysicalAIntent(31),),
    )
    completed = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None, registration=registration)
    )

    class _Coordinator:
        def arm_first_contact(
            self, request: FirstContactRequest
        ) -> CoordinateTransition:
            requests.append(request)
            return armed

        def complete(
            self, completion: CoordinateAdapterCompletion
        ) -> CoordinateTransition:
            completions.append(completion)
            return completed

    window = types.SimpleNamespace(
        _coordinate_system_coordinator=_Coordinator(),
        stage_controller=types.SimpleNamespace(
            run_external_current_physical_machine_coordinates=lambda _axes: {"A": 1.75}
        ),
        design_contact_a_read_finished=types.SimpleNamespace(
            emit=lambda result: queued.append(result)
        ),
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda _owner, value: rendered.append(value),
    )

    capture = Main._design_contact_success_callback(
        window,
        types.SimpleNamespace(frame_id="frame-1", frame_version=7),
    )
    assert callable(capture)
    finalizer = capture(object())

    assert requests == [FirstContactRequest("frame-1", 7)]
    assert completions == []
    assert callable(finalizer)
    finalizer()
    assert queued == [PhysicalAReadResult(31, True, physical_a_mm=1.75)]
    assert completions == []
    assert rendered == [armed]

    Main._on_design_contact_a_read_finished(window, queued[0])

    assert completions == [
        CoordinateAdapterCompletion(
            31,
            PhysicalAReadResult(31, True, physical_a_mm=1.75),
        )
    ]
    assert rendered == [armed, completed]


def test_contact_worker_read_failure_is_queued_without_touching_coordinator(
    monkeypatch,
) -> None:
    reads: list[tuple[str, ...]] = []
    queued: list[PhysicalAReadResult] = []
    armed = CoordinateTransition(
        CoordinateSystemSnapshot(
            True,
            (),
            None,
            registration=RegistrationWorkflowSnapshot(
                active_frame_id="frame-2",
                active_frame_version=8,
            ),
        ),
        intents=(ReadPhysicalAIntent(32),),
    )
    coordinator = types.SimpleNamespace(
        arm_first_contact=lambda _request: armed,
    )
    window = types.SimpleNamespace(
        _coordinate_system_coordinator=coordinator,
        stage_controller=types.SimpleNamespace(
            run_external_current_physical_machine_coordinates=lambda axes: (
                reads.append(tuple(axes))
                or (_ for _ in ()).throw(RuntimeError("read failed"))
            )
        ),
        design_contact_a_read_finished=types.SimpleNamespace(
            emit=lambda result: queued.append(result)
        ),
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )

    capture = Main._design_contact_success_callback(
        window,
        types.SimpleNamespace(frame_id="frame-2", frame_version=8),
    )

    assert callable(capture)
    finalizer = capture(object())
    assert callable(finalizer)
    finalizer()
    assert reads == [("A",)]
    assert queued == [PhysicalAReadResult(32, False, message="read failed")]


def test_api_contact_callback_arms_and_completes_only_on_creator_thread(
    monkeypatch,
) -> None:
    application = QApplication.instance() or QApplication([])
    creator_thread = threading.get_ident()
    arm_threads: list[int] = []
    complete_threads: list[int] = []
    render_threads: list[int] = []
    read_threads: list[int] = []
    registration = RegistrationWorkflowSnapshot(
        active_frame_id="frame-threaded",
        active_frame_version=9,
    )
    armed = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None, registration=registration),
        intents=(ReadPhysicalAIntent(33),),
    )
    completed = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None, registration=registration)
    )

    class _Coordinator:
        def arm_first_contact(
            self, _request: FirstContactRequest
        ) -> CoordinateTransition:
            arm_threads.append(threading.get_ident())
            return armed

        def complete(
            self, _completion: CoordinateAdapterCompletion
        ) -> CoordinateTransition:
            complete_threads.append(threading.get_ident())
            return completed

    class _Owner(QObject):
        design_contact_arm_requested = Signal(object)
        design_contact_a_read_finished = Signal(object)

    owner = _Owner()
    owner._coordinate_system_coordinator = _Coordinator()
    owner.stage_controller = types.SimpleNamespace(
        run_external_current_physical_machine_coordinates=lambda _axes: (
            read_threads.append(threading.get_ident()) or {"A": 1.5}
        )
    )
    owner.design_contact_arm_requested.connect(
        lambda request: Main._on_design_contact_arm_requested(owner, request),
        Qt.ConnectionType.BlockingQueuedConnection,
    )
    owner.design_contact_a_read_finished.connect(
        lambda result: Main._on_design_contact_a_read_finished(owner, result),
        Qt.ConnectionType.QueuedConnection,
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda _owner, _transition: render_threads.append(threading.get_ident()),
    )
    worker_finished = threading.Event()

    def construct_and_run_callback() -> None:
        capture = Main._design_contact_success_callback(
            owner,
            types.SimpleNamespace(frame_id="frame-threaded", frame_version=9),
        )
        assert callable(capture)
        finalizer = capture(object())
        assert callable(finalizer)
        finalizer()
        worker_finished.set()

    worker = threading.Thread(
        target=construct_and_run_callback,
        name="ApiStageCommand-check_contact",
    )
    worker.start()
    deadline = time.monotonic() + 2.0
    while (worker.is_alive() or not complete_threads) and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.001)
    worker.join(timeout=0.2)
    application.processEvents()

    assert worker_finished.is_set()
    assert arm_threads == [creator_thread]
    assert complete_threads == [creator_thread]
    assert render_threads == [creator_thread, creator_thread]
    assert len(read_threads) == 1 and read_threads[0] != creator_thread

    owner._design_contact_success_callback = types.MethodType(
        Main._design_contact_success_callback,
        owner,
    )
    point = types.SimpleNamespace(index=7)
    owner.lcr_controller = object()
    owner.route_measurement_status = types.SimpleNamespace(emit=lambda _message: None)
    owner._api_contact_context = lambda _number: {
        "accepted": True,
        "point": point,
        "contact": {"contact_number": 7},
    }
    owner._api_ensure_measurement_instrument_connected = lambda: None
    owner._api_needle_feedrate = lambda _payload: 7.0
    owner._snapshot_active_route_design_frame = lambda: types.SimpleNamespace(
        frame_id="frame-threaded",
        frame_version=9,
    )
    owner._api_timestamp_utc = lambda: "2026-08-10T00:00:00+00:00"
    created_current: list[dict[str, object]] = []

    class _CurrentContactRunner:
        SHORT_CHECK_SAMPLE_COUNT = (
            api_meter_visa.RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT
        )
        AUTO_CONTACT_SEEK_MAX_TOTAL_MM = (
            api_meter_visa.RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM
        )
        AUTO_CONTACT_SEEK_STEP_MM = (
            api_meter_visa.RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM
        )
        DEFAULT_CONTACT_SETTLE_S = (
            api_meter_visa.RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S
        )

        def __init__(self, **kwargs: object) -> None:
            created_current.append(dict(kwargs))

        def check_contact(self, _point: object) -> object:
            return object()

    api_result: dict[str, object] = {}
    api_finished = threading.Event()

    def construct_current_contact_runner() -> None:
        api_result.update(
            Main._api_measure_current_contact(
                owner,
                {"contact_number": 7},
                seek=False,
            )
        )
        api_finished.set()

    monkeypatch.setattr(
        api_meter_visa,
        "RouteMeasurementRunner",
        _CurrentContactRunner,
    )
    monkeypatch.setattr(
        api_meter_visa,
        "api_current_contact_response",
        lambda *_args, **_kwargs: {"accepted": True},
    )
    api_worker = threading.Thread(
        target=construct_current_contact_runner,
        name="ApiStageCommand-check_contact-real-path",
    )
    api_worker.start()
    deadline = time.monotonic() + 2.0
    while api_worker.is_alive() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.001)
    api_worker.join(timeout=0.2)

    assert api_finished.is_set()
    assert api_result == {"accepted": True}
    assert callable(created_current[0]["post_success_contact"])

    owner._api_route_lcr_controller = object()
    owner.route_measurement_progress = types.SimpleNamespace(emit=lambda *_args: None)
    owner.route_measurement_result = types.SimpleNamespace(emit=lambda *_args: None)
    owner.route_measurement_waiting_changed = types.SimpleNamespace(
        emit=lambda *_args: None
    )
    owner._capture_api_route_photo_artifact = lambda *_args: None
    owner._api_route_photo_autofocus = lambda *_args, **_kwargs: None
    owner._capture_route_contact_photo = lambda *_args: None
    owner._capture_route_pre_contact_photo = lambda *_args: None
    created_route: list[dict[str, object]] = []

    class _RouteSessionRunner:
        def __init__(self, **kwargs: object) -> None:
            created_route.append(dict(kwargs))

    monkeypatch.setattr(
        api_route_scan,
        "RouteExternalMeasurementSessionRunner",
        _RouteSessionRunner,
    )
    route_finished = threading.Event()

    def construct_route_session_runner() -> None:
        Main._build_api_route_session_runner(
            owner,
            session_id="session-1",
            points=[point],
            selected_point=point,
            start_settings=types.SimpleNamespace(
                measurement_count=5,
                initial_measurement_count=2,
                max_relative_rms=0.01,
                contact_quality_limits=None,
                contact_seek_step_mm=0.001,
                contact_seek_range_mm=0.01,
                contact_settle_s=0.1,
                photo_enabled=False,
                photo_focus_enabled=False,
                photo_settle_s=0.0,
                photo_focus_range_mm=0.03,
            ),
            meter_configuration=types.SimpleNamespace(
                nplc_label=lambda: "1",
                measurement_type_label=lambda: "resistance",
            ),
            needle_feedrate=7.0,
            design_frame_snapshot=types.SimpleNamespace(
                frame_id="frame-threaded",
                frame_version=9,
            ),
        )
        route_finished.set()

    route_worker = threading.Thread(
        target=construct_route_session_runner,
        name="ApiStageCommand-start_route_session-real-path",
    )
    route_worker.start()
    deadline = time.monotonic() + 2.0
    while route_worker.is_alive() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.001)
    route_worker.join(timeout=0.2)

    assert route_finished.is_set()
    assert callable(created_route[0]["post_success_contact"])
    assert arm_threads == [creator_thread, creator_thread, creator_thread]
    assert complete_threads == [creator_thread]
    assert render_threads == [creator_thread] * 4
