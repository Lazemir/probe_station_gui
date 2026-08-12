from __future__ import annotations

from types import SimpleNamespace

from probe_station_gui.stage.axis_coordinates import NeedleHeightSaveResult
from probe_station_gui.views import main_window_docks
from probe_station_gui.views import main_window_needle_calibration as calibration_ui


class _Signal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args: object) -> None:
        for callback in tuple(self.callbacks):
            callback(*args)


def test_save_current_needle_height_only_starts_background_request() -> None:
    requests: list[object] = []
    controller = SimpleNamespace(
        request_needle_height_save=lambda request_id: (
            requests.append(request_id) or True
        ),
        latest_a_position=lambda: (_ for _ in ()).throw(
            AssertionError("GUI must not read cached or live A here")
        ),
        current_a_position=lambda: (_ for _ in ()).throw(
            AssertionError("GUI must not query A")
        ),
        set_current_axis_work_coordinate=lambda *_args: (_ for _ in ()).throw(
            AssertionError("GUI must not issue G10")
        ),
    )
    owner = SimpleNamespace(stage_controller=controller)

    calibration_ui.save_current_needle_height(owner)

    assert requests == [1]
    assert owner._needle_height_save_request_id == 1
    assert owner._pending_needle_height_save_request_id == 1


def test_matching_needle_height_result_saves_lowering(monkeypatch) -> None:
    saved: list[float] = []
    owner = SimpleNamespace(_pending_needle_height_save_request_id=7)
    monkeypatch.setattr(
        calibration_ui,
        "save_needle_down_position_from_lowering",
        lambda received_owner, lowering: (
            saved.append(lowering)
            if received_owner is owner
            else (_ for _ in ()).throw(AssertionError("wrong owner"))
        ),
    )

    calibration_ui.on_needle_height_save_finished(
        owner,
        7,
        NeedleHeightSaveResult(True, lowering_mm=1.25),
    )

    assert saved == [1.25]
    assert owner._pending_needle_height_save_request_id is None


def test_failed_needle_height_result_reports_without_saving(monkeypatch) -> None:
    statuses: list[str] = []
    owner = SimpleNamespace(
        _pending_needle_height_save_request_id=8,
        _show_status=lambda message: statuses.append(message),
    )
    monkeypatch.setattr(
        calibration_ui,
        "save_needle_down_position_from_lowering",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not save")),
    )

    calibration_ui.on_needle_height_save_finished(
        owner,
        8,
        NeedleHeightSaveResult(False, error="Unable to set A0 at needle contact."),
    )

    assert statuses == ["Unable to set A0 at needle contact."]
    assert owner._pending_needle_height_save_request_id is None


def test_joystick_needle_save_intent_and_controller_result_are_wired(
    monkeypatch,
) -> None:
    signal_names = (
        "autofocus_requested",
        "home_axis_requested",
        "home_all_requested",
        "needles_raise_requested",
        "needles_lift_requested",
        "needles_lower_requested",
        "needle_current_lower_contact_save_requested",
        "needle_contact_coordinate_save_requested",
        "zero_b_requested",
    )
    joystick = SimpleNamespace(**{name: _Signal() for name in signal_names})
    requests: list[object] = []
    controller = SimpleNamespace(
        request_autofocus=lambda: None,
        request_needles_raise=lambda: None,
        request_needles_lift=lambda: None,
        request_needles_lower=lambda: None,
        request_needle_height_save=lambda request_id: (
            requests.append(request_id) or True
        ),
        needle_height_save_finished=_Signal(),
    )
    owner = SimpleNamespace(
        joystick_panel=joystick,
        stage_controller=controller,
        _zero_b_axis=lambda: None,
    )
    saved: list[float] = []
    monkeypatch.setattr(
        calibration_ui,
        "save_needle_down_position_from_lowering",
        lambda _owner, lowering: saved.append(lowering),
    )

    main_window_docks._connect_joystick_motion_actions(owner)
    joystick.needle_current_lower_contact_save_requested.emit()
    controller.needle_height_save_finished.emit(
        1,
        NeedleHeightSaveResult(True, lowering_mm=0.6),
    )

    assert requests == [1]
    assert saved == [0.6]


def test_stale_needle_height_result_cannot_replace_newer_request(monkeypatch) -> None:
    owner = SimpleNamespace(
        _pending_needle_height_save_request_id=12,
        _show_status=lambda *_args: (_ for _ in ()).throw(
            AssertionError("stale result must be inert")
        ),
    )
    monkeypatch.setattr(
        calibration_ui,
        "save_needle_down_position_from_lowering",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("stale result must not save")
        ),
    )

    calibration_ui.on_needle_height_save_finished(
        owner,
        11,
        NeedleHeightSaveResult(True, lowering_mm=0.7),
    )

    assert owner._pending_needle_height_save_request_id == 12


def test_rejected_repeat_save_keeps_first_accepted_request_pending() -> None:
    requests: list[object] = []

    def request(request_id: object) -> bool:
        requests.append(request_id)
        return len(requests) == 1

    owner = SimpleNamespace(
        stage_controller=SimpleNamespace(request_needle_height_save=request)
    )

    calibration_ui.save_current_needle_height(owner)
    calibration_ui.save_current_needle_height(owner)

    assert requests == [1, 2]
    assert owner._needle_height_save_request_id == 2
    assert owner._pending_needle_height_save_request_id == 1
