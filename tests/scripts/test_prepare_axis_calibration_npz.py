from pathlib import Path

import numpy as np
import pytest

from scripts.prepare_axis_calibration_npz import (
    longest_strictly_increasing_indices,
    prepare_forward_calibration,
)


def test_longest_strictly_increasing_indices_is_strict_and_deterministic() -> None:
    values = np.asarray([0.2, 0.1, 0.3, 0.3, 0.4, 0.0], dtype=float)

    indices = longest_strictly_increasing_indices(values)

    assert indices.tolist() == [1, 3, 4]
    assert np.all(np.diff(values[indices]) > 0.0)


def test_preparer_selects_direct_pass_and_keeps_original_samples(
    tmp_path: Path,
) -> None:
    source = tmp_path / "raw.npz"
    np.savez(
        source,
        gcode=[0.0, 1.0, 2.0, 3.0, 4.0],
        indicator=[9.0, 0.2, 0.1, 0.3, 0.4],
        direction=[-1, 1, 1, 1, 1],
    )
    destination = tmp_path / "forward.npz"

    count = prepare_forward_calibration(source, destination, axis="Z")

    assert count == 3
    with np.load(destination, allow_pickle=False) as data:
        assert set(data.files) == {"axis", "controller", "physical"}
        assert data["axis"].shape == ()
        assert data["axis"].dtype.kind == "U"
        assert data["axis"].item() == "Z"
        assert np.all(np.diff(data["controller"]) > 0.0)
        assert np.all(np.diff(data["physical"]) > 0.0)
        assert data["controller"].tolist() == [2.0, 3.0, 4.0]
        assert data["physical"].tolist() == [0.1, 0.3, 0.4]


def test_preparer_refuses_to_overwrite_destination(tmp_path: Path) -> None:
    source = tmp_path / "raw.npz"
    destination = tmp_path / "forward.npz"
    np.savez(
        source,
        gcode=[0.0, 1.0],
        indicator=[0.0, 1.0],
        direction=[1, 1],
    )
    destination.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        prepare_forward_calibration(source, destination, axis="X")

    assert destination.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "payload",
    [
        {"gcode": [0.0], "indicator": [0.0]},
        {"gcode": [0.0, 1.0], "indicator": [0.0], "direction": [1, 1]},
        {"gcode": [0.0, 1.0], "indicator": [0.0, np.nan], "direction": [1, 1]},
        {"gcode": [0.0, 1.0], "indicator": [1.0, 0.0], "direction": [1, 1]},
    ],
)
def test_preparer_rejects_unusable_source(tmp_path: Path, payload: dict) -> None:
    source = tmp_path / "raw.npz"
    np.savez(source, **payload)

    with pytest.raises(ValueError):
        prepare_forward_calibration(source, tmp_path / "forward.npz", axis="Z")

