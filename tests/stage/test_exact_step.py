import pytest

from probe_station_gui.stage.exact_step import ExactStepAccumulator


AXES = ("X", "Y", "Z", "A", "B", "C")


def test_relative_presses_accumulate_from_one_baseline() -> None:
    accumulator = ExactStepAccumulator(AXES)

    assert accumulator.add_relative("X", 0.001, baseline=1.0) == pytest.approx(1.001)
    assert accumulator.add_relative("X", 0.001, baseline=99.0) == pytest.approx(1.002)
    assert accumulator.add_relative("X", -0.001, baseline=99.0) == pytest.approx(1.001)

    assert accumulator.targets == {"X": pytest.approx(1.001)}


def test_axes_accumulate_independently() -> None:
    accumulator = ExactStepAccumulator(AXES)

    accumulator.add_relative("X", 0.002, baseline=1.0)
    accumulator.add_relative("Y", -0.003, baseline=4.0)

    assert accumulator.targets == {
        "X": pytest.approx(1.002),
        "Y": pytest.approx(3.997),
    }


def test_absolute_target_replaces_only_its_axis() -> None:
    accumulator = ExactStepAccumulator(AXES)
    accumulator.add_relative("X", 0.001, baseline=1.0)
    accumulator.add_relative("Y", 0.001, baseline=2.0)

    accumulator.set_absolute("X", 7.0)

    assert accumulator.targets == {"X": 7.0, "Y": pytest.approx(2.001)}


def test_drain_returns_snapshot_and_clears_pending_targets() -> None:
    accumulator = ExactStepAccumulator(AXES)
    accumulator.add_relative("B", 0.1, baseline=20.0)

    snapshot = accumulator.drain()

    assert snapshot == {"B": pytest.approx(20.1)}
    assert accumulator.targets == {}
    assert accumulator.drain() == {}


@pytest.mark.parametrize(
    ("axis", "value"),
    [("Q", 1.0), ("X", float("nan")), ("X", float("inf"))],
)
def test_invalid_axis_or_target_is_rejected(axis: str, value: float) -> None:
    accumulator = ExactStepAccumulator(AXES)

    with pytest.raises(ValueError):
        accumulator.set_absolute(axis, value)

