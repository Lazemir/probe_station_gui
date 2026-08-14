from __future__ import annotations

import types
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from probe_station_gui.application import design_load
from probe_station_gui.application.design_load import _PendingDesignMarkupLoad
from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    MachineProfileObservation,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameLoadResult,
)
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.design.layout_state import DesignLayoutState
from probe_station_gui.design.markup import MarkupDocument, MarkupLoadChoice
from probe_station_gui.design.markup_store import StoreLoadResult
from probe_station_gui.design.selection_model import (
    MixedArrayRequest,
    SelectionModel,
    markup_entity_id,
    route_entity_id,
)
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design import session_navigation
from probe_station_gui.route.model import MeasurementRoute
from tests.app.test_main_design_navigation import (
    DesignDocument,
    Main,
    _activate_candidate_success,
    _machine_snapshot,
    _make_document,
    _make_window,
    main_module,
)
from tests.app.route_run_execution_support import (
    activate_route_run,
    install_route_run_execution,
)
from tests.stage.controller_test_support import StageController as RealStageController


class _LayoutStateAdapter:
    def __init__(
        self,
        route: MeasurementRoute,
        markup: MarkupDocument,
        selection: SelectionModel,
        statuses: list[str],
    ) -> None:
        self._state = (
            DesignLayoutState()
            .with_route(route, selected_route_point_index=-1)
            .with_markup(markup)
            .with_selection(selection)
        )
        self._statuses = statuses

    @property
    def selection(self) -> SelectionModel:
        return self._state.selection

    @selection.setter
    def selection(self, value: SelectionModel) -> None:
        self._state = self._state.with_selection(value)

    def plan_mixed_delete(self, *, edit_safe: bool):
        return self._state.plan_mixed_delete(edit_safe=edit_safe)

    def plan_mixed_array(self, request: MixedArrayRequest, *, edit_safe: bool):
        return self._state.plan_mixed_array(request, edit_safe=edit_safe)

    def set_selection(self, value: SelectionModel) -> None:
        self.selection = value
        self._statuses.append(f"selection:{len(value.ids)}")

    def set_markup(self, value: MarkupDocument) -> None:
        self._state = self._state.with_markup(value)
        self._statuses.append(f"markup:{len(value.guides)}")

    def set_guide_undo_available(self, value: bool) -> None:
        self._statuses.append(f"undo:{value}")

    def set_route_edit_enabled(self, value: bool) -> None:
        self._statuses.append(f"edit:{value}")


def _make_mixed_edit_window(tmp_path: Path) -> tuple[Main, list[str]]:
    window, _stage, statuses = _make_window()
    document = _make_document(tmp_path)
    session_navigation.load_document(window._design_session, document)
    route = MeasurementRoute.default_for_document(document)
    route_point = route.add_point((2.0, 3.0))
    window._design_session.route = route
    markup = MarkupDocument.empty(document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="guide-1",
    )
    window._design_markup = markup
    window._design_markup_direct_guide_ids = ["guide-1"]
    install_route_run_execution(window)
    selection = SelectionModel(
        frozenset(
            {
                route_entity_id(route_point.id),
                markup_entity_id("guide-1"),
            }
        )
    )
    window.design_layout_window = _LayoutStateAdapter(
        route,
        markup,
        selection,
        statuses,
    )
    window._publish_design_markup = lambda: statuses.append("publish_markup")
    return window, statuses


def _pending_markup_context(
    document: DesignDocument,
    *,
    generation: int = 1,
    previous_markup: MarkupDocument | None = None,
) -> _PendingDesignMarkupLoad:
    candidate_session = DesignSession()
    plan = design_load.design_navigation.design_document_loaded_plan(
        candidate_session,
        document,
        None,
        None,
    )
    assert plan.accepted
    return _PendingDesignMarkupLoad(
        generation=generation,
        session=candidate_session,
        plan=plan,
        show_window=False,
        previous_markup=previous_markup,
    )


def test_rejected_design_activation_keeps_previous_session_markup_and_registry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, _statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous-activation")
    session_navigation.load_document(window._design_session, previous_document)
    previous_markup = MarkupDocument.empty(previous_document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="previous-guide",
    )
    window._design_markup = previous_markup
    registry = CoordinateFrameRegistry()
    coordinator = CoordinateSystemCoordinator._for_testing(
        registry=registry,
        session=window._design_session,
    )
    load = coordinator.start(MachineProfileObservation("profile-a")).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            load.intent_id,
            CoordinateFrameLoadResult(
                load.intent_id,
                CoordinateFrameDocument(),
            ),
        )
    )
    window._coordinate_system_coordinator = coordinator
    window.stage_controller.axes_are_homed = lambda axes: bool(set(axes) <= {"X", "Y"})
    window.stage_controller.latest_machine_coordinate_snapshot = lambda: (
        _machine_snapshot((1.0, 2.0, 0.0, 0.0, 0.0, 0.0))
    )
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=(0.0, 0.0)
    )
    window._active_objective_xy_offset = lambda: (0.0, 0.0)
    window._active_design_frame_metadata = None
    window._design_load_pending = True
    window._design_markup_pending_visibility = True
    success_plans: list[object] = []
    window._apply_design_load_success_plan = lambda *args: success_plans.append(args)
    monkeypatch.setattr(
        main_module.coordinate_flow.stage_position_panel,
        "refresh_coordinate_frame_display",
        lambda _owner: None,
    )
    candidate_document = _make_document(tmp_path / "rejected-activation")
    context = replace(
        _pending_markup_context(
            candidate_document,
            previous_markup=previous_markup,
        ),
        frame_metadata=object(),
    )
    candidate_markup = MarkupDocument.empty(candidate_document.path)
    session_identity = id(window._design_session)
    baseline = window._design_session.snapshot_state()
    records = registry.snapshot().records

    Main._commit_pending_design_load(window, context, candidate_markup)

    assert id(window._design_session) == session_identity
    assert window._design_session.snapshot_state() == baseline
    assert registry.snapshot().records == records
    assert window._design_markup is previous_markup
    assert window._active_design_frame_metadata is None
    assert success_plans == []
    assert window._design_load_pending is False
    assert window._design_markup_pending_visibility is None


def test_mixed_delete_commits_route_and_markup_together(tmp_path: Path) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)

    Main._delete_design_selection(window)

    assert window._design_session.route is not None
    assert window._design_session.route.points == []
    assert window._design_markup is not None
    assert window._design_markup.guides == ()
    assert "publish_markup" in statuses
    assert "selection:0" in statuses


def test_route_running_blocks_even_markup_only_delete(tmp_path: Path) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    window.design_layout_window.selection = SelectionModel(
        frozenset({markup_entity_id("guide-1")})
    )
    activate_route_run(
        window,
        object(),
        types.SimpleNamespace(is_alive=lambda: True),
    )

    Main._delete_design_selection(window)

    assert window._design_markup is not None
    assert [guide.id for guide in window._design_markup.guides] == ["guide-1"]
    assert window._design_session.route is not None
    assert len(window._design_session.route.points) == 1
    assert "publish_markup" not in statuses
    assert "Design editing is locked." in statuses


def test_pending_design_load_blocks_old_route_mutation(tmp_path: Path) -> None:
    window, _statuses = _make_mixed_edit_window(tmp_path)
    window._design_markup_load_request_id = 99
    window._design_load_pending = True

    Main._remove_selected_route_point(window)

    assert window._design_session.route is not None
    assert len(window._design_session.route.points) == 1


def test_mixed_array_keeps_sources_and_copies_both_entity_types(tmp_path: Path) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    original_selection = window.design_layout_window.selection
    request = MixedArrayRequest(
        direction_1=(10.0, 0.0),
        count_1=2,
        direction_2=(0.0, 5.0),
        count_2=1,
        source_ids=original_selection.ids,
    )

    Main._apply_mixed_design_array(window, request)

    assert window._design_session.route is not None
    assert [point.camera_center for point in window._design_session.route.points] == [
        (2.0, 3.0),
        (12.0, 3.0),
    ]
    assert window._design_markup is not None
    assert [(guide.start, guide.end) for guide in window._design_markup.guides] == [
        ((0.0, 0.0), (1.0, 0.0)),
        ((10.0, 0.0), (11.0, 0.0)),
    ]
    assert "publish_markup" in statuses
    assert f"selection:{len(original_selection.ids)}" in statuses


def test_mixed_array_preserves_canonical_selection_when_request_ids_are_stale(
    tmp_path: Path,
) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    route = window._design_session.route
    markup = window._design_markup
    assert route is not None
    assert markup is not None
    canonical_id = route_entity_id(route.points[0].id)
    stale_point = route.add_point((20.0, 30.0))
    window.design_layout_window = _LayoutStateAdapter(
        route,
        markup,
        SelectionModel(frozenset({canonical_id})),
        statuses,
    )
    request = MixedArrayRequest(
        direction_1=(10.0, 0.0),
        count_1=2,
        direction_2=(0.0, 5.0),
        count_2=1,
        source_ids=frozenset({route_entity_id(stale_point.id)}),
    )

    Main._apply_mixed_design_array(window, request)

    assert [point.camera_center for point in route.points] == [
        (2.0, 3.0),
        (20.0, 30.0),
        (12.0, 3.0),
    ]
    assert window.design_layout_window.selection.ids == frozenset({canonical_id})


def test_rotate_design_transforms_and_persists_route_and_markup_together(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        snapshot=lambda: CoordinateSystemSnapshot(False, (), None),
        cancel_registration=lambda _reason: CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None)
        ),
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        _activate_candidate_success,
    )

    Main._rotate_design_document(window, 1)

    assert window._design_session.document is not None
    assert window._design_session.document.rotation_quarter_turns == 1
    assert window._design_session.route is not None
    assert [point.camera_center for point in window._design_session.route.points] == [
        (7.0, 2.0),
    ]
    assert window._design_markup is not None
    assert [(guide.start, guide.end) for guide in window._design_markup.guides] == [
        ((10.0, 0.0), (10.0, 1.0)),
    ]
    assert "publish_markup" in statuses


@pytest.mark.parametrize(
    ("choice", "expected_guides", "expected_event"),
    [
        (MarkupLoadChoice.KEEP, 1, "publish_markup"),
        (MarkupLoadChoice.START_EMPTY, 0, "delete_markup"),
    ],
)
def test_changed_markup_choice_rebinds_or_discards_saved_guides(
    monkeypatch,
    tmp_path: Path,
    choice: MarkupLoadChoice,
    expected_guides: int,
    expected_event: str,
) -> None:
    window, _stage, statuses = _make_window()
    document = _make_document(tmp_path)
    saved = MarkupDocument.empty(document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="saved-guide",
    )
    document.path.write_bytes(b"changed-after-markup")
    window._design_markup_load_request_id = 7
    window._design_markup_load_contexts[7] = _pending_markup_context(document)
    window._prompt_changed_markup_choice = lambda _path: choice
    window._publish_design_markup = lambda: statuses.append("publish_markup")
    window._delete_persisted_design_markup = lambda _path: statuses.append(
        "delete_markup"
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: statuses.append("persisted"),
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        _activate_candidate_success,
    )

    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(7, saved.source_path, saved),
    )

    assert window._design_markup is not None
    assert len(window._design_markup.guides) == expected_guides
    assert window._design_markup.matches_source(document.path)
    assert expected_event in statuses


def test_changed_markup_cancel_restores_preload_design(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous")
    previous_session = DesignSession()
    session_navigation.load_document(previous_session, previous_document)
    previous_markup = MarkupDocument.empty(previous_document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="previous-guide",
    )
    new_document = _make_document(tmp_path / "new")
    saved = MarkupDocument.empty(new_document.path).append_guide(
        (2.0, 0.0),
        (3.0, 0.0),
        guide_id="new-guide",
    )
    new_document.path.write_bytes(b"changed")
    window._design_session = previous_session
    window._design_markup = previous_markup
    window._design_markup_load_request_id = 9
    window._design_markup_load_contexts[9] = _pending_markup_context(
        new_document,
        previous_markup=previous_markup,
    )
    window._prompt_changed_markup_choice = lambda _path: MarkupLoadChoice.CANCEL
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: statuses.append("persisted"),
    )

    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(9, saved.source_path, saved),
    )

    assert window._design_session is previous_session
    assert window._design_markup is previous_markup
    assert "Design load canceled." in statuses
    assert "persisted" not in statuses


def test_changed_markup_cancel_never_commits_or_clears_preload_ui_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous-live")
    session_navigation.load_document(window._design_session, previous_document)
    previous_markup = MarkupDocument.empty(previous_document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="previous-guide",
    )
    window._design_markup = previous_markup
    window._design_markup_direct_guide_ids = ["previous-guide"]
    window._last_selected_design_point = (4.0, 5.0)
    previous_selection = SelectionModel(frozenset({markup_entity_id("previous-guide")}))
    previewed: list[DesignDocument] = []
    finished_previews: list[DesignDocument | None] = []
    pending_states: list[bool] = []
    layout = types.SimpleNamespace(
        selection=previous_selection,
        set_document_preview=previewed.append,
        finish_document_preview=finished_previews.append,
        set_design_load_pending=pending_states.append,
        set_markup=lambda markup: setattr(
            layout,
            "selection",
            (previous_selection if markup is previous_markup else SelectionModel()),
        ),
        set_guide_undo_available=lambda _value: None,
    )
    window.design_layout_window = layout
    loads: list[tuple[int, Path]] = []
    window._design_markup_store = types.SimpleNamespace(
        load=lambda request_id, path: loads.append((request_id, Path(path)))
    )
    window._begin_design_markup_load = types.MethodType(
        Main._begin_design_markup_load,
        window,
    )
    window._design_load_show_window[1] = True
    new_document = _make_document(tmp_path / "new-cancelled")
    saved = MarkupDocument.empty(new_document.path).append_guide(
        (2.0, 0.0),
        (3.0, 0.0),
        guide_id="new-guide",
    )
    new_document.path.write_bytes(b"changed-after-save")
    window._prompt_changed_markup_choice = lambda _path: MarkupLoadChoice.CANCEL
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: statuses.append("persisted"),
    )

    Main._on_design_document_loaded(window, 1, new_document, None)

    assert window._design_session.document is previous_document
    assert window._design_markup is previous_markup
    assert window._design_markup_direct_guide_ids == ["previous-guide"]
    assert window._last_selected_design_point == (4.0, 5.0)
    assert layout.selection == previous_selection
    assert previewed == [new_document]
    assert pending_states[-1]
    request_id = loads[-1][0]

    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(request_id, saved.source_path, saved),
    )

    assert window._design_session.document is previous_document
    assert window._design_markup is previous_markup
    assert window._design_markup_direct_guide_ids == ["previous-guide"]
    assert window._last_selected_design_point == (4.0, 5.0)
    assert layout.selection == previous_selection
    assert finished_previews == [previous_document]
    assert not pending_states[-1]


def test_newer_design_load_invalidates_older_markup_response(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, _statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous-generation")
    session_navigation.load_document(window._design_session, previous_document)
    loads: list[tuple[int, Path]] = []
    window._design_markup_store = types.SimpleNamespace(
        load=lambda request_id, path: loads.append((request_id, Path(path)))
    )
    window._begin_design_markup_load = types.MethodType(
        Main._begin_design_markup_load,
        window,
    )
    previewed: list[DesignDocument] = []
    window.design_layout_window = types.SimpleNamespace(
        set_document_preview=previewed.append,
        finish_document_preview=lambda _document: None,
        set_design_load_pending=lambda _pending: None,
    )
    monkeypatch.setattr(
        design_load,
        "toggle_design_layout_window",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        design_load.threading,
        "Thread",
        lambda **_kwargs: types.SimpleNamespace(start=lambda: None),
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: None,
    )
    window._coordinate_system_coordinator = types.SimpleNamespace(
        cancel_registration=lambda _reason: CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None)
        )
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        _activate_candidate_success,
    )
    first_document = _make_document(tmp_path / "first-generation")
    second_document = _make_document(tmp_path / "second-generation")
    window._design_load_show_window[1] = False

    Main._on_design_document_loaded(window, 1, first_document, None)
    first_request_id = loads[-1][0]

    Main._start_design_document_load(
        window,
        str(second_document.path),
        restore_state=None,
        show_window=False,
    )
    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(
            first_request_id,
            first_document.path,
            MarkupDocument.empty(first_document.path),
        ),
    )

    assert window._design_session.document is previous_document

    Main._on_design_document_loaded(window, 2, second_document, None)
    second_request_id = loads[-1][0]
    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(
            second_request_id,
            second_document.path,
            MarkupDocument.empty(second_document.path),
        ),
    )

    assert window._design_session.document is second_document
    assert previewed == []


def test_markup_read_failure_commits_the_already_visible_design(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, _statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous-read-failure")
    session_navigation.load_document(window._design_session, previous_document)
    loads: list[tuple[int, Path]] = []
    window._design_markup_store = types.SimpleNamespace(
        load=lambda request_id, path: loads.append((request_id, Path(path)))
    )
    window._begin_design_markup_load = types.MethodType(
        Main._begin_design_markup_load,
        window,
    )
    previewed: list[DesignDocument] = []
    finished: list[DesignDocument | None] = []
    pending: list[bool] = []
    window.design_layout_window = types.SimpleNamespace(
        set_document_preview=previewed.append,
        finish_document_preview=finished.append,
        set_design_load_pending=pending.append,
        set_status_message=lambda _message: None,
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: None,
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        _activate_candidate_success,
    )
    monkeypatch.setattr(
        design_load,
        "toggle_design_layout_window",
        lambda *_args: None,
    )
    document = _make_document(tmp_path / "visible-despite-read-failure")
    window._design_load_show_window[1] = True

    Main._on_design_document_loaded(window, 1, document, None)
    request_id = loads[-1][0]

    Main._on_design_markup_store_failed(
        window,
        types.SimpleNamespace(
            request_id=request_id,
            operation="load",
            source_path=document.path,
            message="read failed",
        ),
    )

    assert previewed == [document]
    assert finished == [document]
    assert window._design_session.document is document
    assert window._design_markup == MarkupDocument.empty(document.path)
    assert not pending[-1]


def test_start_empty_deletes_sidecar_even_with_pending_visibility(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, statuses = _make_window()
    document = _make_document(tmp_path)
    saved = MarkupDocument.empty(document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
    )
    document.path.write_bytes(b"changed-after-markup")
    window._design_markup_load_request_id = 11
    window._design_markup_load_contexts[11] = _pending_markup_context(document)
    window._design_markup_pending_visibility = False
    window._prompt_changed_markup_choice = lambda _path: MarkupLoadChoice.START_EMPTY
    window._publish_design_markup = lambda: statuses.append("publish_markup")
    window._delete_persisted_design_markup = lambda _path: statuses.append(
        "delete_markup"
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: None,
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        _activate_candidate_success,
    )

    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(11, saved.source_path, saved),
    )

    assert "delete_markup" in statuses
    assert "publish_markup" not in statuses


def test_empty_markup_visibility_is_persisted_as_a_snapshot(tmp_path: Path) -> None:
    window, _stage, _statuses = _make_window()
    document = _make_document(tmp_path)
    markup = MarkupDocument.empty(document.path, visible=False)
    operations: list[tuple[str, object]] = []
    window._design_markup = markup
    window._design_markup_store = types.SimpleNamespace(
        publish=lambda request_id, value: operations.append(
            ("publish", (request_id, value))
        ),
        delete=lambda request_id, path: operations.append(
            ("delete", (request_id, path))
        ),
    )

    Main._publish_design_markup(window)

    assert operations == [("publish", (1, markup))]


def test_clear_all_deletes_sidecar_instead_of_saving_empty_markup(
    tmp_path: Path,
) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    window._delete_persisted_design_markup = lambda path: statuses.append(
        f"delete:{Path(path).name}"
    )
    window._publish_design_markup = lambda: statuses.append("publish_markup")

    Main._clear_design_guides(window)

    assert window._design_markup is not None
    assert window._design_markup.guides == ()
    assert "delete:loaded.gds" in statuses
    assert "publish_markup" not in statuses


def test_move_to_design_coordinate_seeds_prediction_and_requests_move_after_acceptance() -> (
    None
):
    window, stage_controller, statuses = _make_window()
    session_navigation.load_document(
        window._design_session,
        DesignDocument(
            path=Path("C:/designs/sample.gds"),
            library=object(),
            top_cell=object(),
            top_cell_name="TOP",
            cell_names=("TOP",),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 1.0, 1.0),
            polygons_by_layer={(1, 0): (np.asarray([[0.0, 0.0], [1.0, 0.0]]),)},
            visible_layers=frozenset({(1, 0)}),
        ),
    )

    accepted = Main._move_to_design_coordinate(
        window,
        (100.0, 200.0),
        source_label="design window",
    )

    assert accepted
    assert statuses == ["refresh_panel", "clear_prediction"]
    assert window._last_selected_design_point == (100.0, 200.0)
    assert window._pending_planned_move_target_xy == (1.5, -2.0)
    assert window._pending_planned_move_source_label == "design window"
    assert stage_controller.move_requests == [(1.5, -2.0)]


def test_move_to_design_coordinate_reports_busy_race_rejection() -> None:
    window, _stage_controller, _statuses = _make_window()
    session_navigation.load_document(
        window._design_session,
        DesignDocument(
            path=Path("C:/designs/sample.gds"),
            library=object(),
            top_cell=object(),
            top_cell_name="TOP",
            cell_names=("TOP",),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 1.0, 1.0),
            polygons_by_layer={(1, 0): (np.asarray([[0.0, 0.0], [1.0, 0.0]]),)},
            visible_layers=frozenset({(1, 0)}),
        ),
    )

    accepted = Main._move_to_design_coordinate(
        window,
        (100.0, 200.0),
        source_label="focus reference",
        move_request=lambda _x, _y: False,
    )

    assert accepted is False
    assert window._pending_planned_move_target_xy is None
    assert window._pending_planned_move_source_label is None


def test_move_to_design_coordinate_reports_real_controller_rejection() -> None:
    window, _stage_controller, _statuses = _make_window()
    stage_controller = RealStageController()
    stage_controller._start_background_task = lambda **_kwargs: False
    window.stage_controller = stage_controller
    session_navigation.load_document(
        window._design_session,
        DesignDocument(
            path=Path("C:/designs/sample.gds"),
            library=object(),
            top_cell=object(),
            top_cell_name="TOP",
            cell_names=("TOP",),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 1.0, 1.0),
            polygons_by_layer={(1, 0): (np.asarray([[0.0, 0.0], [1.0, 0.0]]),)},
            visible_layers=frozenset({(1, 0)}),
        ),
    )

    try:
        accepted = Main._move_to_design_coordinate(
            window,
            (100.0, 200.0),
            source_label="focus reference",
        )

        assert accepted is False
        assert window._pending_planned_move_target_xy is None
        assert window._pending_planned_move_source_label is None
    finally:
        stage_controller.shutdown()
