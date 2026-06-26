import sys
import types
import unittest


def _restore_real_imports_for_main() -> None:
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            del sys.modules[name]
    serial_module = sys.modules.get("serial")
    if serial_module is not None and not hasattr(serial_module, "__path__"):
        for name in list(sys.modules):
            if name == "serial" or name.startswith("serial."):
                del sys.modules[name]
    for name in list(sys.modules):
        if name == "probe_station_gui" or name.startswith("probe_station_gui."):
            del sys.modules[name]


_restore_real_imports_for_main()
from main import Main


class _FakeStageController:
    DEFAULT_FEEDRATE = 600.0

    def __init__(self, *, state: str = "run") -> None:
        self.state = state
        self.position = (0.0, 0.0, 4.0, 0.0, 0.0)
        self.status_timestamp = 10.0
        self.move_requests: list[tuple[float, float]] = []

    def latest_stage_state(self) -> str:
        return self.state

    def axes_are_homed(self, axes: set[str]) -> bool:
        return axes.issubset({"X", "Y", "Z"})

    def latest_stage_position(self) -> tuple[float, ...]:
        return self.position

    def last_status_timestamp(self) -> float:
        return self.status_timestamp

    def is_busy(self) -> bool:
        return False

    def request_move_to_xy(self, target_x_mm: float, target_y_mm: float) -> None:
        self.move_requests.append((float(target_x_mm), float(target_y_mm)))


class _DesignRestoreStageController:
    def __init__(self) -> None:
        self.homed_axes = {"X", "Y", "Z", "A"}
        self.unhomed_requests: list[set[str]] = []

    def mark_axes_unhomed(self, axes: set[str]) -> set[str]:
        normalized = {str(axis).strip().upper() for axis in axes}
        self.unhomed_requests.append(normalized)
        removed = self.homed_axes.intersection(normalized)
        self.homed_axes -= removed
        return set(removed)


def _make_main(
    *,
    state: str = "run",
) -> tuple[
    Main,
    list[tuple[float, ...]],
    list[tuple[tuple[float, float], tuple[float, float]]],
    list[tuple[tuple[float, float], tuple[float, float]]],
]:
    window = Main.__new__(Main)
    published: list[tuple[float, ...]] = []
    reconciles: list[tuple[tuple[float, float], tuple[float, float]]] = []
    smooth_calls: list[tuple[tuple[float, float], tuple[float, float]]] = []

    window.stage_controller = _FakeStageController(state=state)
    window.contact_calibration_window = None
    window._pending_alignment_preparation = None
    window._last_reported_b_position = None
    window._coordinate_move_stage_position = None
    window._planned_move_stage_xy = (5.0, 5.0)
    window._planned_move_started_at = 1.0
    window._planned_move_waiting_for_fresh_status = False
    window._planned_move_stop_status_timestamp = None
    window._pending_planned_move_target_xy = None
    window._pending_planned_move_source_label = None
    window._manual_jog_waiting_for_fresh_status = False
    window._manual_jog_stage_position = None
    window._manual_jog_stage_xy = None
    window._manual_jog_stop_tail_position = None
    window._manual_jog_stop_status_timestamp = None
    window._manual_jog_settle_until = 0.0
    window._manual_jog_last_timestamp = None

    window._coerce_position_tuple = lambda position: tuple(position)
    window._maybe_restore_persisted_design = lambda _position: None
    window._manual_jog_prediction_available = lambda: False
    window._manual_jog_prediction_active = lambda: False
    window._should_ignore_manual_jog_status_sample = lambda _stage_xy: False
    window._log_design_position_reconcile = (
        lambda predicted, actual: reconciles.append((predicted, actual))
    )

    def smooth(
        predicted: tuple[float, float],
        actual: tuple[float, float],
    ) -> tuple[float, float]:
        smooth_calls.append((predicted, actual))
        return actual

    window._smooth_manual_jog_actual_position = smooth
    window._learn_manual_jog_stop_tail = lambda _predicted, _actual: None
    window._clear_manual_jog_stop_prediction = lambda: None
    window._publish_stage_position_estimate = lambda position: published.append(
        tuple(float(value) for value in position)
    )
    window._finish_coordinate_move_if_idle = lambda _position: None
    window._clear_stage_motion_axes = lambda: None

    return window, published, reconciles, smooth_calls


def _make_design_restore_main(
    expected_position: tuple[float, ...] | None,
) -> tuple[
    Main,
    _DesignRestoreStageController,
    list[str],
    list[tuple[str, dict]],
    list[bool],
]:
    window = Main.__new__(Main)
    stage_controller = _DesignRestoreStageController()
    statuses: list[str] = []
    starts: list[tuple[str, dict]] = []
    saved_without_design: list[bool] = []

    window.stage_controller = stage_controller
    window._pending_persisted_design_state = {
        "document_path": "C:\\designs\\sample.gds",
    }
    window._pending_persisted_design_position = expected_position
    window._design_session = types.SimpleNamespace(document=None)
    window._show_status = lambda message, _timeout=0: statuses.append(message)
    window._persisted_design_file_is_current = lambda _state: True
    window._save_controller_state_without_design = (
        lambda: saved_without_design.append(True)
    )
    window._start_design_document_load = (
        lambda path, **kwargs: starts.append((path, dict(kwargs)))
    )
    return window, stage_controller, statuses, starts, saved_without_design


class MainPlannedMovePredictionTest(unittest.TestCase):
    def test_invalid_stage_position_only_updates_display(self) -> None:
        window, published, _reconciles, _smooth_calls = _make_main(state="idle")
        display_updates: list[object] = []
        finished: list[object] = []
        cleared: list[str] = []

        window._update_stage_position_display = lambda position: display_updates.append(
            position
        )
        window._finish_coordinate_move_if_idle = lambda position: finished.append(position)
        window._clear_stage_motion_axes = lambda: cleared.append("clear")

        Main._on_stage_position_changed(window, ["not", "a", "tuple"])

        self.assertEqual(display_updates, [["not", "a", "tuple"]])
        self.assertEqual(published, [])
        self.assertEqual(finished, [])
        self.assertEqual(cleared, [])

    def test_design_coordinate_waits_for_absolute_xy_move_start(self) -> None:
        window, _published, _reconciles, _smooth_calls = _make_main(state="idle")
        stage_controller = window.stage_controller
        window._design_session = type("_DesignSession", (), {"document": object()})()
        window._raw_stage_xy_from_design_xy = lambda _design_xy: (1.5, -2.0)
        window._last_selected_design_point = None
        window._refresh_design_panel = lambda: None
        starts = []
        window._start_planned_move_prediction = (
            lambda target, **kwargs: starts.append((target, kwargs))
        )

        accepted = Main._move_to_design_coordinate(
            window,
            (100.0, 200.0),
            source_label="design window",
        )

        self.assertTrue(accepted)
        self.assertEqual(stage_controller.move_requests, [(1.5, -2.0)])
        self.assertEqual(window._pending_planned_move_target_xy, (1.5, -2.0))
        self.assertEqual(window._pending_planned_move_source_label, "design window")
        self.assertEqual(starts, [])

        Main._on_absolute_xy_move_started(window, 1.5, -2.0, 300.0)

        self.assertIsNone(window._pending_planned_move_target_xy)
        self.assertIsNone(window._pending_planned_move_source_label)
        self.assertEqual(
            starts,
            [
                (
                    (1.5, -2.0),
                    {
                        "source_label": "design window",
                        "feedrate_mm_min": 300.0,
                    },
                )
            ],
        )

    def test_active_planned_move_status_keeps_predicted_position(self) -> None:
        window, published, reconciles, smooth_calls = _make_main(state="run")

        Main._on_stage_position_changed(window, (0.0, 0.0, 4.0, 0.0, 0.0))

        self.assertEqual(window._planned_move_stage_xy, (5.0, 5.0))
        self.assertEqual(window._manual_jog_stage_xy, (5.0, 5.0))
        self.assertEqual(published[-1][:2], (5.0, 5.0))
        self.assertEqual(reconciles, [((5.0, 5.0), (0.0, 0.0))])
        self.assertEqual(smooth_calls, [])

    def test_completed_planned_move_can_accept_fresh_status(self) -> None:
        window, published, _reconciles, smooth_calls = _make_main(state="idle")
        window._planned_move_started_at = None
        window._planned_move_waiting_for_fresh_status = True

        Main._on_stage_position_changed(window, (0.0, 0.0, 4.0, 0.0, 0.0))

        self.assertEqual(window._planned_move_stage_xy, (0.0, 0.0))
        self.assertFalse(window._planned_move_waiting_for_fresh_status)
        self.assertEqual(published[-1][:2], (0.0, 0.0))
        self.assertEqual(smooth_calls, [((5.0, 5.0), (0.0, 0.0))])

    def test_unhomed_xy_without_manual_prediction_clears_prediction_and_finishes_when_idle(
        self,
    ) -> None:
        window, published, _reconciles, _smooth_calls = _make_main(state="idle")
        coordinate_updates: list[tuple[float, float] | None] = []
        design_updates: list[tuple[float, float] | None] = []
        finished: list[tuple[float, ...]] = []
        cleared: list[str] = []
        displayed: list[tuple[float, ...]] = []

        window.stage_controller.axes_are_homed = lambda axes: False
        window._manual_jog_stage_position = (9.0, 9.0, 9.0)
        window._manual_jog_stage_xy = (9.0, 9.0)
        window._planned_move_stage_xy = (8.0, 8.0)
        window._update_stage_position_display = lambda position: displayed.append(position)
        window._update_coordinate_display = (
            lambda *, center_xy=None, cursor_xy=None: coordinate_updates.append(center_xy)
        )
        window._update_design_position = lambda stage_xy: design_updates.append(stage_xy)
        window._can_display_design_position = lambda: True
        window._finish_coordinate_move_if_idle = lambda position: finished.append(position)
        window._clear_stage_motion_axes = lambda: cleared.append("clear")

        Main._on_stage_position_changed(window, (1.0, 2.0, 3.0))

        self.assertEqual(displayed, [(1.0, 2.0, 3.0)])
        self.assertIsNone(window._manual_jog_stage_position)
        self.assertIsNone(window._manual_jog_stage_xy)
        self.assertIsNone(window._planned_move_stage_xy)
        self.assertEqual(coordinate_updates, [None])
        self.assertEqual(design_updates, [(1.0, 2.0)])
        self.assertEqual(finished, [(1.0, 2.0, 3.0)])
        self.assertEqual(cleared, ["clear"])
        self.assertEqual(published, [])

    def test_b_axis_motion_invalidates_registration_only_when_conditions_match(self) -> None:
        window, _published, _reconciles, _smooth_calls = _make_main(state="run")
        invalidations: list[str] = []
        window._design_session = types.SimpleNamespace(
            registration=types.SimpleNamespace(valid=True)
        )
        window._invalidate_design_registration = lambda reason: invalidations.append(
            reason
        )
        window._last_reported_b_position = 5.0

        Main._on_stage_position_changed(window, (0.0, 0.0, 4.0, 0.0, 5.5))

        self.assertEqual(
            invalidations,
            ["Design registration cleared after B-axis motion."],
        )
        self.assertEqual(window._last_reported_b_position, 5.5)

        below_tolerance, _published, _reconciles, _smooth_calls = _make_main(state="run")
        below_tolerance._design_session = types.SimpleNamespace(
            registration=types.SimpleNamespace(valid=True)
        )
        below_tolerance._invalidate_design_registration = (
            lambda reason: invalidations.append(f"unexpected:{reason}")
        )
        below_tolerance._last_reported_b_position = 5.0

        Main._on_stage_position_changed(below_tolerance, (0.0, 0.0, 4.0, 0.0, 5.0))

        pending_alignment, _published, _reconciles, _smooth_calls = _make_main(
            state="run"
        )
        pending_alignment._design_session = types.SimpleNamespace(
            registration=types.SimpleNamespace(valid=True)
        )
        pending_alignment._pending_alignment_preparation = object()
        pending_alignment._invalidate_design_registration = (
            lambda reason: invalidations.append(f"unexpected:{reason}")
        )
        pending_alignment._last_reported_b_position = 5.0

        Main._on_stage_position_changed(pending_alignment, (0.0, 0.0, 4.0, 0.0, 5.5))

        invalid_registration, _published, _reconciles, _smooth_calls = _make_main(
            state="run"
        )
        invalid_registration._design_session = types.SimpleNamespace(
            registration=types.SimpleNamespace(valid=False)
        )
        invalid_registration._invalidate_design_registration = (
            lambda reason: invalidations.append(f"unexpected:{reason}")
        )
        invalid_registration._last_reported_b_position = 5.0

        Main._on_stage_position_changed(invalid_registration, (0.0, 0.0, 4.0, 0.0, 5.5))

        self.assertEqual(
            invalidations,
            ["Design registration cleared after B-axis motion."],
        )

    def test_publish_happens_before_idle_finish_and_motion_clear(self) -> None:
        window, _published, _reconciles, _smooth_calls = _make_main(state="idle")
        calls: list[str] = []
        window._planned_move_started_at = None
        window._planned_move_stage_xy = None
        window._publish_stage_position_estimate = lambda position: calls.append(
            f"publish:{position[:2]}"
        )
        window._finish_coordinate_move_if_idle = lambda position: calls.append(
            f"finish:{position[:2]}"
        )
        window._clear_stage_motion_axes = lambda: calls.append("clear")

        Main._on_stage_position_changed(window, (1.0, 2.0, 3.0))

        self.assertEqual(
            calls,
            ["publish:(1.0, 2.0)", "finish:(1.0, 2.0)", "clear"],
        )


class MainPersistedDesignRestoreTest(unittest.TestCase):
    def test_z_a_b_mismatch_keeps_design_and_clears_only_z_homing(self) -> None:
        window, stage_controller, statuses, starts, saved_without_design = (
            _make_design_restore_main((1.0, 2.0, 3.0, 4.0, 5.0))
        )

        Main._maybe_restore_persisted_design(
            window,
            (1.0, 2.0, 9.0, 0.0, 8.0),
        )

        self.assertEqual(stage_controller.unhomed_requests, [{"Z"}])
        self.assertEqual(stage_controller.homed_axes, {"X", "Y", "A"})
        self.assertEqual(saved_without_design, [])
        self.assertEqual(starts[0][0], "C:\\designs\\sample.gds")
        self.assertEqual(
            starts[0][1],
            {
                "restore_state": {"document_path": "C:\\designs\\sample.gds"},
                "show_window": False,
            },
        )
        self.assertIn(
            "Controller Z coordinate changed. Cleared cached Z homing.",
            statuses,
        )

    def test_xy_mismatch_clears_design_and_xy_homing(self) -> None:
        window, stage_controller, statuses, starts, saved_without_design = (
            _make_design_restore_main((1.0, 2.0, 3.0, 4.0, 5.0))
        )

        Main._maybe_restore_persisted_design(
            window,
            (1.5, 2.5, 3.0, 4.0, 5.0),
        )

        self.assertEqual(stage_controller.unhomed_requests, [{"X", "Y"}])
        self.assertEqual(stage_controller.homed_axes, {"Z", "A"})
        self.assertEqual(starts, [])
        self.assertEqual(saved_without_design, [True])
        self.assertIn(
            "Controller X/Y coordinates changed. Cleared cached design selection.",
            statuses,
        )


if __name__ == "__main__":
    unittest.main()
