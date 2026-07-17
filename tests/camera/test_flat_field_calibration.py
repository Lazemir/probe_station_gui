from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera.flat_field_calibration import (
    FlatFieldCalibrationStore,
    median_flat_field_reference,
)


@pytest.fixture
def flat_frames() -> list[QImage]:
    frames: list[QImage] = []
    for dark_x in (2, 8, 14):
        image = QImage(17, 11, QImage.Format_RGB32)
        for y in range(image.height()):
            for x in range(image.width()):
                shade = 80 + 4 * x
                if x == dark_x:
                    shade = 5
                image.setPixelColor(x, y, QColor(shade, shade + 1, shade + 2))
        frames.append(image)
    return frames


def test_median_reference_ignores_shifted_dark_features(flat_frames: list[QImage]) -> None:
    reference = median_flat_field_reference(flat_frames)

    assert reference.size() == flat_frames[0].size()
    assert reference.pixelColor(2, 5).red() > 80
    assert reference.pixelColor(8, 5).red() > 80
    assert reference.pixelColor(14, 5).red() > 80


def test_install_writes_versioned_reference_and_current_manifest(
    tmp_path: Path,
    flat_frames: list[QImage],
) -> None:
    stored = FlatFieldCalibrationStore(tmp_path).install(
        "X20", flat_frames, blur_radius_px=401, max_gain=4.0, metadata={"tiles": 9}
    )

    manifest = json.loads(stored.current_manifest.read_text(encoding="utf-8"))
    assert stored.reference_image.is_file()
    assert stored.profile.image_size_px == (17, 11)
    assert manifest["version"] == 1
    assert manifest["objective"] == "X20"
    assert manifest["metadata"] == {"tiles": 9}
    assert stored.reference_image.parent.name != "X20"


def test_load_accepts_existing_current_manifest_shape(tmp_path: Path) -> None:
    profile_dir = tmp_path / "calibrations" / "flat-field" / "X20" / "profile"
    profile_dir.mkdir(parents=True)
    reference_path = profile_dir / "reference.png"
    reference = QImage(17, 11, QImage.Format_RGB32)
    reference.fill(QColor(120, 121, 122))
    assert reference.save(str(reference_path), "PNG")
    current_path = profile_dir.parent / "current.json"
    current_path.write_text(
        json.dumps(
            {
                "version": 1,
                "objective": "X20",
                "reference_image": str(reference_path),
                "frame_size_px": [17, 11],
                "blur_radius_px": 9,
                "max_gain": 5.0,
                "coordinate_space": "raw camera frame, before lens distortion correction",
                "application_order": ["flat_field", "lens_distortion", "mosaic"],
            }
        ),
        encoding="utf-8",
    )

    stored = FlatFieldCalibrationStore(tmp_path).load("X20")

    assert stored.current_manifest == current_path
    assert stored.reference_image == reference_path
    assert stored.profile.blur_radius_px == 9
    assert stored.profile.max_gain == 5.0


@pytest.mark.parametrize(
    ("version", "include_version"),
    [
        pytest.param(None, False, id="missing"),
        pytest.param(None, True, id="null"),
        pytest.param(0, True, id="older"),
        pytest.param(2, True, id="future"),
        pytest.param("1", True, id="string"),
        pytest.param(1.0, True, id="float"),
        pytest.param(True, True, id="boolean"),
    ],
)
def test_load_rejects_current_manifest_without_exact_schema_version(
    tmp_path: Path,
    flat_frames: list[QImage],
    version: object,
    include_version: bool,
) -> None:
    store = FlatFieldCalibrationStore(tmp_path)
    current_path = store.install("X20", flat_frames).current_manifest
    manifest = json.loads(current_path.read_text(encoding="utf-8"))
    if include_version:
        manifest["version"] = version
    else:
        manifest.pop("version")
    current_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="version must be exactly 1"):
        FlatFieldCalibrationStore(tmp_path).load("X20")


def test_failed_install_keeps_previous_current_manifest(
    tmp_path: Path,
    flat_frames: list[QImage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FlatFieldCalibrationStore(tmp_path)
    first = store.install("X20", flat_frames, blur_radius_px=401, max_gain=4.0)
    monkeypatch.setattr(QImage, "save", lambda *_args: False)

    with pytest.raises(RuntimeError):
        store.install("X20", flat_frames, blur_radius_px=401, max_gain=4.0)

    assert store.load("X20").reference_image == first.reference_image


def test_profile_manifest_write_failure_keeps_previous_current_manifest(
    tmp_path: Path,
    flat_frames: list[QImage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FlatFieldCalibrationStore(tmp_path)
    first = store.install("X20", flat_frames)
    previous_current = first.current_manifest.read_bytes()

    from probe_station_gui.camera import flat_field_calibration

    real_write_json = flat_field_calibration._write_json

    def fail_profile_manifest(path: Path, payload: object) -> None:
        if path.name == "profile.json":
            raise OSError("profile manifest write failed")
        real_write_json(path, payload)

    monkeypatch.setattr(flat_field_calibration, "_write_json", fail_profile_manifest)

    with pytest.raises(OSError, match="profile manifest write failed"):
        store.install("X20", flat_frames)

    assert first.current_manifest.read_bytes() == previous_current
    assert FlatFieldCalibrationStore(tmp_path).load("X20").reference_image == first.reference_image


def test_staged_current_manifest_write_failure_keeps_previous_current_manifest(
    tmp_path: Path,
    flat_frames: list[QImage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FlatFieldCalibrationStore(tmp_path)
    first = store.install("X20", flat_frames)
    previous_current = first.current_manifest.read_bytes()

    from probe_station_gui.camera import flat_field_calibration

    real_write_json = flat_field_calibration._write_json
    temporary_paths: list[Path] = []
    profile_manifest_paths: list[Path] = []

    def fail_staged_current_pointer(path: Path, payload: object) -> None:
        if path.name == "profile.json":
            profile_manifest_paths.append(path)
        if (
            path.parent == first.current_manifest.parent
            and path.name.startswith(".current.json.")
            and path.name.endswith(".tmp")
        ):
            temporary_paths.append(path)
            real_write_json(path, payload)
            raise OSError("staged current pointer write failed")
        real_write_json(path, payload)

    monkeypatch.setattr(flat_field_calibration, "_write_json", fail_staged_current_pointer)

    with pytest.raises(OSError, match="staged current pointer write failed"):
        store.install("X20", flat_frames)

    assert profile_manifest_paths
    assert profile_manifest_paths[0].is_file()
    assert first.current_manifest.read_bytes() == previous_current
    assert temporary_paths
    assert not temporary_paths[0].exists()
    assert FlatFieldCalibrationStore(tmp_path).load("X20").reference_image == first.reference_image


def test_current_pointer_replacement_failure_keeps_previous_current_manifest(
    tmp_path: Path,
    flat_frames: list[QImage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FlatFieldCalibrationStore(tmp_path)
    first = store.install("X20", flat_frames)
    previous_current = first.current_manifest.read_bytes()

    def fail_replace(_path: Path, _target: Path) -> Path:
        raise OSError("current pointer replacement failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="current pointer replacement failed"):
        store.install("X20", flat_frames)

    assert first.current_manifest.read_bytes() == previous_current
    assert FlatFieldCalibrationStore(tmp_path).load("X20").reference_image == first.reference_image


@pytest.mark.parametrize(
    "objective_name",
    [
        "",
        ".",
        "..",
        "X20/extra",
        "X20\\extra",
        "X20:alternate",
        "X20.",
        "X20 ",
        "CON",
        "NUL",
        "COM1",
        "LPT1",
    ],
)
def test_store_rejects_objective_that_is_not_a_safe_path_component(
    tmp_path: Path,
    flat_frames: list[QImage],
    objective_name: str,
) -> None:
    with pytest.raises(ValueError):
        FlatFieldCalibrationStore(tmp_path).install(objective_name, flat_frames)
