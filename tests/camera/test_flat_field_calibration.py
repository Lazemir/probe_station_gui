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


@pytest.mark.parametrize(
    "objective_name",
    ["", ".", "..", "X20/extra", "X20\\extra", "X20:alternate"],
)
def test_store_rejects_objective_that_is_not_a_safe_path_component(
    tmp_path: Path,
    flat_frames: list[QImage],
    objective_name: str,
) -> None:
    with pytest.raises(ValueError):
        FlatFieldCalibrationStore(tmp_path).install(objective_name, flat_frames)
