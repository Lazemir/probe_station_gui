from __future__ import annotations

from types import SimpleNamespace

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
