from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from scripts.prepare_stitched_z_calibration_npz import prepare_stitched_z_calibration


SECTION_2_OFFSET_MM = 8.661368914604154
SECTION_3_OFFSET_MM = 13.56547962940159


def _write_valid_sections(directory: Path) -> tuple[Path, Path, Path]:
    section1 = directory / "section1.npz"
    section2 = directory / "section2.npz"
    section3 = directory / "section3.npz"
    np.savez(
        section1,
        gcode=[11.8, 11.9, 12.0],
        indicator=[11.6, 11.7, 11.8],
    )
    np.savez(
        section2,
        gcode=[11.9, 12.0, 20.214, 20.3],
        indicator=[3.2, 3.4, 11.638631085395847, 11.8],
    )
    np.savez(
        section3,
        gcode=[20.214, 20.25, 20.3, 20.4],
        indicator=[6.6, 6.65, 6.699520370598411, 6.9],
        direction=[1, -1, 1, 1],
    )
    return section1, section2, section3


def _write_sections_with_physical_values(
    directory: Path,
    physical_by_section: tuple[tuple[float, float], ...],
) -> tuple[Path, Path, Path]:
    section1 = directory / "section1.npz"
    section2 = directory / "section2.npz"
    section3 = directory / "section3.npz"
    np.savez(
        section1,
        gcode=[11.8, 11.9],
        indicator=physical_by_section[0],
    )
    np.savez(
        section2,
        gcode=[12.0, 20.214],
        indicator=np.asarray(physical_by_section[1]) - SECTION_2_OFFSET_MM,
    )
    np.savez(
        section3,
        gcode=[20.3, 20.4],
        indicator=np.asarray(physical_by_section[2]) - SECTION_3_OFFSET_MM,
        direction=[1, 1],
    )
    return section1, section2, section3


def test_stitcher_keeps_the_required_sections_and_schema(tmp_path: Path) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    output = tmp_path / "stitched.npz"

    count = prepare_stitched_z_calibration(section1, section2, section3, output)

    assert count == 5
    with np.load(output, allow_pickle=False) as data:
        assert set(data.files) == {"axis", "controller", "physical"}
        assert data["axis"].item() == "Z"
        assert np.all(np.diff(data["controller"]) > 0)
        assert np.all(np.diff(data["physical"]) > 0)
        assert 11.9 in data["controller"]
        assert 12.0 in data["controller"]
        assert 20.3 in data["controller"]
        assert 12.0 not in data["controller"][:2]
        assert 11.9 not in data["controller"][2:]
        assert 20.214 not in data["controller"]
        np.testing.assert_allclose(
            data["physical"],
            [11.6, 11.7, 3.4 + SECTION_2_OFFSET_MM,
             6.699520370598411 + SECTION_3_OFFSET_MM,
             6.9 + SECTION_3_OFFSET_MM],
        )


def test_stitcher_keeps_section_2_end_sample_when_physical_curve_allows_it(
    tmp_path: Path,
) -> None:
    section1, section2, section3 = _write_sections_with_physical_values(
        tmp_path,
        ((1.0, 2.0), (3.0, 4.0), (5.0, 6.0)),
    )
    output = tmp_path / "stitched.npz"

    count = prepare_stitched_z_calibration(section1, section2, section3, output)

    assert count == 6
    with np.load(output, allow_pickle=False) as data:
        assert 20.214 in data["controller"]


@pytest.mark.parametrize(
    ("section", "payload"),
    [
        ("section1", {"indicator": [11.6, 11.7]}),
        ("section2", {"gcode": [12.0, 20.214]}),
        ("section3", {"gcode": [20.3, 20.4], "indicator": [6.7, 6.9]}),
    ],
)
def test_stitcher_rejects_missing_raw_keys(
    tmp_path: Path, section: str, payload: dict[str, list[float]]
) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    np.savez({"section1": section1, "section2": section2, "section3": section3}[section], **payload)

    with pytest.raises(ValueError, match="missing"):
        prepare_stitched_z_calibration(section1, section2, section3, tmp_path / "out.npz")


@pytest.mark.parametrize(
    "payload",
    [
        {"gcode": [11.8, np.nan], "indicator": [11.6, 11.7]},
        {"gcode": [11.8, 11.9], "indicator": [11.6, np.inf]},
    ],
)
def test_stitcher_rejects_non_finite_raw_values(tmp_path: Path, payload: dict) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    np.savez(section1, **payload)

    with pytest.raises(ValueError, match="finite"):
        prepare_stitched_z_calibration(section1, section2, section3, tmp_path / "out.npz")


def test_stitcher_rejects_non_finite_direction_values(tmp_path: Path) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    np.savez(
        section3,
        gcode=[20.214, 20.25, 20.3, 20.4],
        indicator=[6.6, 6.65, 6.7, 6.9],
        direction=[1, -1, np.nan, 1],
    )

    with pytest.raises(ValueError, match="finite"):
        prepare_stitched_z_calibration(section1, section2, section3, tmp_path / "out.npz")


def test_stitcher_rejects_non_increasing_raw_controller_data(tmp_path: Path) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    np.savez(section2, gcode=[12.0, 12.0, 20.214], indicator=[3.4, 3.5, 11.6])

    with pytest.raises(ValueError, match="strictly increasing"):
        prepare_stitched_z_calibration(section1, section2, section3, tmp_path / "out.npz")


def test_stitcher_rejects_non_one_dimensional_raw_arrays(tmp_path: Path) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    np.savez(
        section1,
        gcode=[[11.8, 11.9]],
        indicator=[[11.6, 11.7]],
    )

    with pytest.raises(ValueError, match="one-dimensional"):
        prepare_stitched_z_calibration(section1, section2, section3, tmp_path / "out.npz")


def test_stitcher_rejects_unequal_directed_raw_array_lengths(tmp_path: Path) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    np.savez(
        section3,
        gcode=[20.3, 20.4],
        indicator=[6.7, 6.9],
        direction=[1],
    )

    with pytest.raises(ValueError, match="equal lengths"):
        prepare_stitched_z_calibration(section1, section2, section3, tmp_path / "out.npz")


def test_stitcher_accepts_mixed_direction_section_with_increasing_forward_pass(
    tmp_path: Path,
) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    np.savez(
        section3,
        gcode=[20.214, 20.3, 20.25, 20.4],
        indicator=[6.6, 6.7, 6.65, 6.9],
        direction=[-1, 1, -1, 1],
    )
    output = tmp_path / "stitched.npz"

    count = prepare_stitched_z_calibration(section1, section2, section3, output)

    assert count >= 2
    with np.load(output, allow_pickle=False) as data:
        assert np.all(np.diff(data["controller"]) > 0.0)
        assert np.all(np.diff(data["physical"]) > 0.0)


@pytest.mark.parametrize("empty_section", [1, 2, 3])
def test_stitcher_rejects_a_section_with_no_selected_samples(
    tmp_path: Path,
    empty_section: int,
) -> None:
    section1, section2, section3 = _write_sections_with_physical_values(
        tmp_path,
        ((1.0, 2.0), (3.0, 4.0), (5.0, 6.0)),
    )
    if empty_section == 1:
        np.savez(section1, gcode=[12.0, 12.1], indicator=[1.0, 2.0])
    elif empty_section == 2:
        np.savez(section2, gcode=[11.9, 20.3], indicator=[3.0, 4.0])
    else:
        np.savez(
            section3,
            gcode=[20.214, 20.3],
            indicator=[5.0, 6.0],
            direction=[1, -1],
        )

    with pytest.raises(ValueError, match=rf"Section {empty_section}.*no selected"):
        prepare_stitched_z_calibration(section1, section2, section3, tmp_path / "out.npz")


@pytest.mark.parametrize(
    ("removed_section", "physical_by_section"),
    [
        (1, ((100.0, 101.0), (1.0, 2.0), (3.0, 4.0))),
        (2, ((1.0, 2.0), (100.0, 101.0), (3.0, 4.0))),
        (3, ((1.0, 2.0), (3.0, 4.0), (-1.0, 0.0))),
    ],
)
def test_stitcher_rejects_a_section_removed_by_physical_lis(
    tmp_path: Path,
    removed_section: int,
    physical_by_section: tuple[tuple[float, float], ...],
) -> None:
    section1, section2, section3 = _write_sections_with_physical_values(
        tmp_path,
        physical_by_section,
    )

    with pytest.raises(ValueError, match=rf"Section {removed_section}.*removed"):
        prepare_stitched_z_calibration(section1, section2, section3, tmp_path / "out.npz")


def test_stitcher_rejects_fewer_than_two_usable_output_points(tmp_path: Path) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    np.savez(section1, gcode=[12.0], indicator=[11.8])
    np.savez(section2, gcode=[11.9], indicator=[3.2])
    np.savez(section3, gcode=[20.214], indicator=[6.6], direction=[1])

    with pytest.raises(ValueError):
        prepare_stitched_z_calibration(section1, section2, section3, tmp_path / "out.npz")


def test_stitcher_refuses_to_overwrite_destination(tmp_path: Path) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    output = tmp_path / "stitched.npz"
    output.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        prepare_stitched_z_calibration(section1, section2, section3, output)

    assert output.read_bytes() == b"keep"


def test_stitcher_refuses_existing_normalized_suffixless_destination(
    tmp_path: Path,
) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    destination = tmp_path / "stitched"
    normalized_destination = tmp_path / "stitched.npz"
    normalized_destination.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        prepare_stitched_z_calibration(section1, section2, section3, destination)

    assert normalized_destination.read_bytes() == b"keep"


def test_stitcher_exclusively_creates_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    output = tmp_path / "stitched.npz"
    output.write_bytes(b"keep")
    monkeypatch.setattr(Path, "exists", lambda _path: False)

    with pytest.raises(FileExistsError):
        prepare_stitched_z_calibration(section1, section2, section3, output)

    assert output.read_bytes() == b"keep"


def test_stitcher_removes_its_partial_output_when_serialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    output = tmp_path / "stitched.npz"

    def fail_after_partial_write(destination, **_arrays) -> None:
        destination.write(b"partial")
        raise RuntimeError("serialization failed")

    monkeypatch.setattr(np, "savez_compressed", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="serialization failed"):
        prepare_stitched_z_calibration(section1, section2, section3, output)

    assert not output.exists()


def test_cli_prints_sample_count_and_exits_nonzero_on_validation_failure(
    tmp_path: Path,
) -> None:
    section1, section2, section3 = _write_valid_sections(tmp_path)
    output = tmp_path / "stitched.npz"
    script = Path(__file__).parents[2] / "scripts" / "prepare_stitched_z_calibration_npz.py"

    completed = subprocess.run(
        [sys.executable, str(script), str(section1), str(section2), str(section3), str(output), "--axis", "Z"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "5"
    invalid_section1 = tmp_path / "invalid-section1.npz"
    invalid_output = tmp_path / "invalid-stitched.npz"
    np.savez(
        invalid_section1,
        gcode=[11.8, np.nan],
        indicator=[11.6, 11.7],
    )
    failed = subprocess.run(
        [
            sys.executable,
            str(script),
            str(invalid_section1),
            str(section2),
            str(section3),
            str(invalid_output),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert failed.returncode != 0
    assert "finite" in failed.stderr
    assert not invalid_output.exists()
