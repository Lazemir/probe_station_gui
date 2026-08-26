import time
import types
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from unittest import mock

from tests.app.import_reset import restore_real_imports_for_main


restore_real_imports_for_main()
import main as main_module
from probe_station_gui.application import design_load
from probe_station_gui.stage.coordinate_targets import CoordinatePendingEdits
from main import Main
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAuthorityObservation,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    RegistrationWorkflowSnapshot,
)
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage.manual_jog_prediction import (
    ManualJogPredictionConfig,
    ManualJogPredictionState,
)
from probe_station_gui.stage import position_update


class _IdentityAxisCalibrationMapper:
    @staticmethod
    def controller_to_physical(_axis: str, value: float) -> float:
        return float(value)


def _ignore_authority_observation(
    _observation: CoordinateAuthorityObservation,
) -> None:
    return


@dataclass(frozen=True)
class _FakeCoordinateSystemCoordinator:
    current: CoordinateSystemSnapshot
    authority_observer: Callable[[CoordinateAuthorityObservation], None]

    def snapshot(self) -> CoordinateSystemSnapshot:
        return self.current

    def observe_authority(
        self,
        observation: CoordinateAuthorityObservation,
    ) -> CoordinateTransition:
        self.authority_observer(observation)
        return CoordinateTransition(self.current)


class _FakeStageController:
    DEFAULT_FEEDRATE = 600.0

    def __init__(self, *, state: str = "run") -> None:
        self.state = state
        self.position = (0.0, 0.0, 4.0, 0.0, 0.0)
        self.machine_position = self.position
        self.mapper = _IdentityAxisCalibrationMapper()
        self.status_timestamp = 10.0
        self.last_jog_write_time: float | None = None
        self.move_requests: list[tuple[float, float]] = []

    def latest_stage_state(self) -> str:
        return self.state

    def axes_are_homed(self, axes: set[str]) -> bool:
        return axes.issubset({"X", "Y", "Z"})

    def latest_stage_position(self) -> tuple[float, ...]:
        return self.position

    def latest_synchronized_machine_position(self) -> tuple[float, ...]:
        return self.machine_position

    def _axis_calibration_mapper(self) -> _IdentityAxisCalibrationMapper:
        return self.mapper

    def last_status_timestamp(self) -> float:
        return self.status_timestamp

    def last_jog_write_timestamp(self) -> float | None:
        return self.last_jog_write_time

    def is_busy(self) -> bool:
        return False

    def homed_axes(self) -> set[str]:
        return {"X", "Y", "Z"}

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

    def export_cached_controller_state(self) -> dict[str, object]:
        return {"design_session": {"document_path": "C:\\designs\\sample.gds"}}


def _make_main(
    *,
    state: str = "run",
    registration_valid: bool = False,
    authority_observer: Callable[
        [CoordinateAuthorityObservation], None
    ] = _ignore_authority_observation,
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
    window._coordinate_system_coordinator = _FakeCoordinateSystemCoordinator(
        CoordinateSystemSnapshot(
            frames_loaded=False,
            records=(),
            document=None,
            registration=RegistrationWorkflowSnapshot(
                registration_valid=registration_valid
            ),
        ),
        authority_observer,
    )
    window.contact_calibration_window = None
    window._pending_alignment_preparation = None
    motion_state = types.SimpleNamespace(
        physical_machine_pose=PhysicalMachinePose.from_mapping({}),
        coordinate_active=False,
        coordinate_stage_position=None,
        presented_stage_xy=None,
    )
    motion_state.snapshot = lambda: types.SimpleNamespace(
        physical_machine_pose=motion_state.physical_machine_pose,
        coordinate_active=motion_state.coordinate_active,
        coordinate_stage_position=motion_state.coordinate_stage_position,
        presented_stage_xy=motion_state.presented_stage_xy,
    )
    motion_state.pending_coordinate_edits = lambda: CoordinatePendingEdits((), None)
    window._stage_motion = motion_state
    window._manual_jog_prediction = ManualJogPredictionState(
        ManualJogPredictionConfig(
            axis_names=Main.STAGE_AXIS_NAMES,
            ignore_idle_after_command_s=Main.MANUAL_JOG_IGNORE_IDLE_AFTER_COMMAND_S,
            reconcile_smooth_threshold_mm=Main.MANUAL_JOG_RECONCILE_SMOOTH_THRESHOLD_MM,
            reconcile_smooth_alpha=Main.MANUAL_JOG_RECONCILE_SMOOTH_ALPHA,
            status_settle_hold_s=Main.MANUAL_JOG_STATUS_SETTLE_HOLD_S,
            default_stop_tail_s=Main.MANUAL_JOG_DEFAULT_STOP_TAIL_S,
            stop_tail_min_s=Main.MANUAL_JOG_STOP_TAIL_MIN_S,
            stop_tail_max_s=Main.MANUAL_JOG_STOP_TAIL_MAX_S,
            stop_tail_learn_alpha=Main.MANUAL_JOG_STOP_TAIL_LEARN_ALPHA,
        )
    )

    window._pending_persisted_design_state = None
    window._pending_persisted_design_position = None
    window._log_design_position_reconcile = lambda predicted, actual: reconciles.append(
        (predicted, actual)
    )
    original_smooth = window._manual_jog_prediction.smooth_actual_stage_xy

    def smooth(
        predicted: tuple[float, float],
        actual: tuple[float, float],
        *,
        latest_state: str,
    ) -> tuple[float, float]:
        smooth_calls.append((predicted, actual))
        return original_smooth(
            predicted,
            actual,
            latest_state=latest_state,
        )

    window._manual_jog_prediction.smooth_actual_stage_xy = smooth
    window._stage_axis_fields = {}
    window._stage_unhomed_display_origins = {}
    window._stage_axis_raw_values = {}
    window._stage_axis_display_values = {}
    window._stage_axis_homed = set()
    window._stage_axis_base_styles = {}
    window._stage_limit_axes = set()
    window._current_linear_feedrate = lambda: 123.0
    window._update_stage_coordinate_apply_state = lambda: None
    window._update_coordinate_display = lambda **_kwargs: None
    window._update_design_position = lambda stage_xy: (
        published.append(tuple(float(value) for value in stage_xy))
        if stage_xy is not None
        else None
    )
    window._can_display_design_position = lambda: True
    window._pending_homing_axes = []
    window._stage_motion_axes = set()
    window._stage_motion_blink_dimmed = False
    window._stage_motion_blink_timer = types.SimpleNamespace(isActive=lambda: False)
    window._stage_position_panel = None

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
    window._coordinate_system_coordinator = types.SimpleNamespace(
        current_design_lease=lambda: types.SimpleNamespace(document=None)
    )
    window._show_status = lambda message, _timeout=0: statuses.append(message)
    window.settings_manager = types.SimpleNamespace(
        save_controller_state=lambda _state: saved_without_design.append(True)
    )
    window._start_design_document_load = lambda path, **kwargs: starts.append(
        (path, dict(kwargs))
    )
    return window, stage_controller, statuses, starts, saved_without_design


def _prepare_design_refresh(
    window: Main,
    *,
    session_xy: tuple[float, float] = (1.0, 2.0),
) -> None:
    window._design_session = types.SimpleNamespace(document=object())
    window.serial_connection = types.SimpleNamespace(is_open=True)
    window._pending_design_stage_xy = None
    motion_state = types.SimpleNamespace(
        presented_stage_xy=session_xy,
        physical_machine_pose=PhysicalMachinePose.from_mapping({}),
        coordinate_active=False,
        coordinate_stage_position=None,
    )
    motion_state.snapshot = lambda: types.SimpleNamespace(
        presented_stage_xy=motion_state.presented_stage_xy,
        physical_machine_pose=motion_state.physical_machine_pose,
        coordinate_active=motion_state.coordinate_active,
        coordinate_stage_position=motion_state.coordinate_stage_position,
    )
    motion_state.pending_coordinate_edits = lambda: CoordinatePendingEdits((), None)
    window._stage_motion = motion_state


class MainPlannedMovePredictionTest(unittest.TestCase):
    def test_design_refresh_keeps_active_coordinate_prediction_precedence(self) -> None:
        window, published, _reconciles, _smooth_calls = _make_main()
        _prepare_design_refresh(window)
        window._stage_motion.coordinate_active = True
        window._stage_motion.coordinate_stage_position = (
            8.0,
            9.0,
            4.0,
            0.0,
            0.0,
        )

        Main._refresh_design_position(window)

        self.assertEqual(published, [(8.0, 9.0)])

    def test_design_refresh_keeps_active_manual_prediction_precedence(self) -> None:
        window, published, _reconciles, _smooth_calls = _make_main()
        _prepare_design_refresh(window)
        window._manual_jog_prediction.stage_position = (6.0, 7.0, 4.0, 0.0, 0.0)
        window._manual_jog_prediction.stage_xy = (6.0, 7.0)
        window._manual_jog_prediction.axis_velocities = {"X": 1.0}

        Main._refresh_design_position(window)

        self.assertEqual(published, [(6.0, 7.0)])

    def test_manual_b_motion_marks_activity_without_staling_registration(self) -> None:
        window = Main.__new__(Main)
        invalidations: list[str] = []
        window._invalidate_design_registration = invalidations.append

        with mock.patch.object(
            main_module.stage_position_panel_adapter,
            "set_stage_motion_axes",
        ) as set_motion_axes:
            Main._on_manual_motion_axis(window, "b")

        set_motion_axes.assert_called_once_with(window, {"B"})
        self.assertEqual(invalidations, [])

    def test_invalid_stage_position_only_updates_display(self) -> None:
        window, published, _reconciles, _smooth_calls = _make_main(state="idle")
        display_updates: list[object] = []
        finished: list[object] = []
        cleared: list[str] = []

        window._clear_stage_motion_axes = lambda: cleared.append("clear")

        with mock.patch.object(
            position_update.stage_position_panel,
            "update_stage_position_display",
            side_effect=lambda _owner, position: display_updates.append(position),
        ):
            position_update.on_stage_position_changed(window, ["not", "a", "tuple"])

        self.assertEqual(display_updates, [["not", "a", "tuple"]])
        self.assertEqual(published, [])
        self.assertEqual(finished, [])
        self.assertEqual(cleared, [])

    def test_idle_status_learns_manual_stop_tail_and_clears_waiting(self) -> None:
        window, published, _reconciles, _smooth_calls = _make_main(state="idle")
        prediction = window._manual_jog_prediction
        prediction.stage_position = (5.0, 5.0, 4.0, 0.0, 0.0)
        prediction.stage_xy = (5.0, 5.0)
        prediction.waiting_for_fresh_status = True
        prediction.stop_tail_position = (5.0, 5.0, 4.0, 0.0, 0.0)
        prediction.stop_axis_velocities = {"X": 1.0}
        prediction.stop_tail_s = 0.1

        position_update.on_stage_position_changed(window, (6.0, 7.0, 4.0, 0.0, 0.0))

        self.assertFalse(prediction.waiting_for_fresh_status)
        self.assertIsNone(prediction.stop_status_timestamp)
        self.assertEqual(prediction.stop_axis_velocities, {})
        self.assertEqual(published[-1][:2], (6.0, 7.0))

    def test_fresh_idle_manual_jog_sample_is_ignored_but_stale_idle_reconciles(
        self,
    ) -> None:
        window, published, reconciles, _smooth_calls = _make_main(state="idle")
        now = time.monotonic()
        prediction = window._manual_jog_prediction
        prediction.stage_position = (5.0, 5.0, 4.0, 0.0, 0.0)
        prediction.stage_xy = (5.0, 5.0)
        prediction.axis_velocities = {"X": 1.0}
        prediction.command_started_at = now - 0.01
        window.stage_controller.last_jog_write_time = now - 0.01

        position_update.on_stage_position_changed(window, (0.0, 0.0, 4.0, 0.0, 0.0))

        self.assertEqual(published, [])
        self.assertEqual(reconciles, [])
        self.assertEqual(prediction.stage_xy, (5.0, 5.0))

        prediction.command_started_at = now - (
            Main.MANUAL_JOG_IGNORE_IDLE_AFTER_COMMAND_S + 0.1
        )
        window.stage_controller.last_jog_write_time = now - (
            Main.MANUAL_JOG_IGNORE_IDLE_AFTER_COMMAND_S + 0.1
        )

        position_update.on_stage_position_changed(window, (0.0, 0.0, 4.0, 0.0, 0.0))

        self.assertEqual(reconciles, [((5.0, 5.0), (0.0, 0.0))])
        self.assertEqual(published[-1][:2], (0.0, 0.0))

    def test_unhomed_xy_without_manual_prediction_clears_prediction_and_finishes_when_idle(
        self,
    ) -> None:
        window, published, _reconciles, _smooth_calls = _make_main(state="idle")
        coordinate_updates: list[tuple[float, float] | None] = []
        design_updates: list[tuple[float, float] | None] = []
        cleared: list[str] = []
        displayed: list[tuple[float, ...]] = []

        window.stage_controller.axes_are_homed = lambda axes: False
        window._manual_jog_prediction.stage_position = (9.0, 9.0, 9.0)
        window._manual_jog_prediction.stage_xy = (9.0, 9.0)
        window._update_coordinate_display = lambda *, center_xy=None, cursor_xy=None: (
            coordinate_updates.append(center_xy)
        )
        window._update_design_position = lambda stage_xy: design_updates.append(
            stage_xy
        )
        window._can_display_design_position = lambda: True
        window._stage_motion_axes = {"X"}
        window._stage_position_panel = types.SimpleNamespace(
            refresh_axis_styles=lambda _axes, _dimmed: cleared.append("clear")
        )

        with mock.patch.object(
            position_update.stage_position_panel,
            "update_stage_position_display",
            side_effect=lambda _owner, position: displayed.append(position),
        ):
            position_update.on_stage_position_changed(window, (1.0, 2.0, 3.0))

        self.assertEqual(displayed, [(1.0, 2.0, 3.0)])
        self.assertIsNone(window._manual_jog_prediction.stage_position)
        self.assertIsNone(window._manual_jog_prediction.stage_xy)
        self.assertEqual(coordinate_updates, [None])
        self.assertEqual(design_updates, [(1.0, 2.0)])
        self.assertEqual(cleared, ["clear"])
        self.assertEqual(published, [])

    def test_publish_happens_before_motion_clear(self) -> None:
        window, _published, _reconciles, _smooth_calls = _make_main(state="idle")
        calls: list[str] = []
        window._stage_motion_axes = {"X"}
        window._stage_position_panel = types.SimpleNamespace(
            refresh_axis_styles=lambda _axes, _dimmed: calls.append("clear")
        )

        with mock.patch.object(
            position_update,
            "publish_stage_position_estimate",
            side_effect=lambda _owner, position, **_kwargs: calls.append(
                f"publish:{position[:2]}"
            ),
        ):
            position_update.on_stage_position_changed(window, (1.0, 2.0, 3.0))

        self.assertEqual(
            calls,
            ["publish:(1.0, 2.0)", "clear"],
        )


class MainPersistedDesignRestoreTest(unittest.TestCase):
    def test_z_mismatch_with_homed_z_keeps_design_and_reports_one_z_clear(self) -> None:
        window, stage_controller, statuses, starts, saved_without_design = (
            _make_design_restore_main((1.0, 2.0, 3.0, 4.0, 5.0))
        )

        design_load.design_workspace.maybe_restore_persisted_design(
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
        self.assertEqual(
            statuses.count("Controller Z coordinate changed. Cleared cached Z homing."),
            1,
        )

    def test_z_mismatch_with_unhomed_z_keeps_design_and_does_not_report_z_clear(
        self,
    ) -> None:
        window, stage_controller, statuses, starts, saved_without_design = (
            _make_design_restore_main((1.0, 2.0, 3.0, 4.0, 5.0))
        )
        stage_controller.homed_axes.remove("Z")

        design_load.design_workspace.maybe_restore_persisted_design(
            window,
            (1.0, 2.0, 9.0, 4.0, 5.0),
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
        self.assertNotIn(
            "Controller Z coordinate changed. Cleared cached Z homing.",
            statuses,
        )

    def test_xy_mismatch_clears_design_and_xy_homing(self) -> None:
        window, stage_controller, statuses, starts, saved_without_design = (
            _make_design_restore_main((1.0, 2.0, 3.0, 4.0, 5.0))
        )

        design_load.design_workspace.maybe_restore_persisted_design(
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
