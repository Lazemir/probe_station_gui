from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.types import StageTaskToken


def test_reset_calibration_emits_distinct_exact_operation_tokens() -> None:
    controller = StageController()
    emitted: list[tuple[object, ...]] = []
    controller.objective_calibration_updated = SimpleNamespace(
        emit=lambda *args: emitted.append(args)
    )
    controller.status_message = SimpleNamespace(emit=lambda *_args: None)

    controller.reset_calibration("first")
    controller.reset_calibration("second")

    assert len(emitted) == 2
    assert emitted[0][:2] == ("X5", [])
    assert emitted[1][:2] == ("X5", [])
    token_a = emitted[0][2]
    token_b = emitted[1][2]
    assert token_a != token_b
    assert controller.is_calibration_task_token_current(token_a) is False
    assert controller.is_calibration_task_token_current(token_b) is True
    assert getattr(token_b, "source") == "click_calibration_reset"
    copied_token = StageTaskToken(token_b.generation, token_b.source)
    assert controller.is_calibration_task_token_current(copied_token) is False


def _new_token(controller: StageController, source: str = "_run_move") -> StageTaskToken:
    return controller._calibration_signal_token(source)


def test_calibration_candidate_does_not_publish_until_exact_token_is_accepted() -> None:
    controller = StageController()
    token = _new_token(controller)
    matrix = [[0.02, 0.0], [0.0, 0.03]]

    assert controller.offer_objective_calibration_candidate(
        token,
        "X5",
        matrix,
    )
    assert controller._pixels_to_mm is None
    assert controller.current_fov_size_mm() is None

    assert controller.accept_objective_calibration_candidate(token, matrix)
    assert controller._pixels_to_mm is None
    assert controller.current_fov_size_mm() is None
    assert controller.publish_objective_calibration_candidate(token)
    assert np.asarray(controller._pixels_to_mm) == pytest.approx(np.asarray(matrix))


def test_generation_b_rejects_candidate_a_without_publishing_it() -> None:
    controller = StageController()
    token_a = _new_token(controller, "calibration-a")
    matrix_a = [[0.02, 0.0], [0.0, 0.02]]
    assert controller.offer_objective_calibration_candidate(
        token_a,
        "X5",
        matrix_a,
    )

    token_b = _new_token(controller, "calibration-b")

    assert controller.is_calibration_task_token_current(token_b)
    assert not controller.accept_objective_calibration_candidate(token_a, matrix_a)
    assert controller._pixels_to_mm is None


def test_cancel_after_compute_prevents_candidate_offer_and_emit() -> None:
    controller = StageController()
    token = _new_token(controller)
    computed_matrix = [[0.02, 0.0], [0.0, 0.02]]

    controller.cancel_active_task("cancel after compute")

    assert not controller.offer_objective_calibration_candidate(
        token,
        "X5",
        computed_matrix,
    )
    assert not controller.is_calibration_task_token_current(token)
    assert controller._pixels_to_mm is None


def test_pending_cancellation_invalidates_current_token_before_rotation() -> None:
    controller = StageController()
    token = _new_token(controller)

    controller._cancel_event.set()

    assert not controller.is_calibration_task_token_current(token)


def test_cancel_after_candidate_offer_rejects_commit_and_wakes_waiter() -> None:
    controller = StageController()
    token = _new_token(controller)
    computed_matrix = [[0.02, 0.0], [0.0, 0.02]]
    assert controller.offer_objective_calibration_candidate(
        token,
        "X5",
        computed_matrix,
    )

    controller.cancel_active_motion("cancel candidate")

    assert not controller.accept_objective_calibration_candidate(
        token,
        computed_matrix,
    )
    assert controller._pixels_to_mm is None
    assert controller.wait_for_objective_calibration_candidate(token, timeout_s=0.1) is False


def test_cancel_after_candidate_accept_prevents_publish_and_wakes_waiter() -> None:
    controller = StageController()
    token = _new_token(controller)
    computed_matrix = [[0.02, 0.0], [0.0, 0.02]]
    assert controller.offer_objective_calibration_candidate(
        token,
        "X5",
        computed_matrix,
    )
    assert controller.accept_objective_calibration_candidate(token, computed_matrix)

    controller.cancel_active_task("cancel accepted candidate")

    assert not controller.publish_objective_calibration_candidate(token)
    assert controller._pixels_to_mm is None
    assert controller.wait_for_objective_calibration_candidate(token, timeout_s=0.1) is False
