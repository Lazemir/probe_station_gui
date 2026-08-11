from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

import probe_station_gui.camera.bright_grid_calibration as bright_grid_calibration
import probe_station_gui.camera.bright_grid_detection as bright_grid_detection
import probe_station_gui.camera.distortion as distortion
import probe_station_gui.camera.seam_radial_fit as seam_radial_fit
import probe_station_gui.camera.stage_geometry_fit as stage_geometry_fit


ROOT = Path(__file__).resolve().parents[2]
CAMERA = ROOT / "probe_station_gui" / "camera"
MOVED_SYMBOLS = {
    "BrightFeatureBounds",
    "GridDetection",
    "SeamRadialDistortionFit",
    "StageGeometryCalibrationFit",
    "detect_bright_feature_bounds",
    "detect_bright_grid",
    "fit_distortion_from_grid_frames",
    "fit_seam_radial_distortion",
    "fit_stage_geometry_from_grid_frames",
    "fit_stage_geometry_from_observations",
}


def _top_level_definitions(module: ast.Module) -> set[str]:
    return {
        node.name
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _imports(module: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def _parsed(filename: str) -> ast.Module:
    return ast.parse((CAMERA / filename).read_text(encoding="utf-8"))


def test_fitting_symbols_have_direct_owners_without_distortion_facade() -> None:
    assert not hasattr(distortion, "fit_stage_geometry_from_observations")
    assert not hasattr(distortion, "fit_distortion_from_grid_frames")
    assert not hasattr(distortion, "fit_seam_radial_distortion")
    assert hasattr(stage_geometry_fit, "fit_stage_geometry_from_observations")
    assert hasattr(bright_grid_calibration, "fit_distortion_from_grid_frames")
    assert not hasattr(bright_grid_calibration, "detect_bright_grid")
    assert hasattr(bright_grid_detection, "detect_bright_grid")
    assert hasattr(seam_radial_fit, "fit_seam_radial_distortion")

    old_definitions = _top_level_definitions(_parsed("distortion.py"))
    assert old_definitions.isdisjoint(MOVED_SYMBOLS)


def test_fitting_import_dag_points_toward_runtime_correction_owner() -> None:
    distortion_imports = _imports(_parsed("distortion.py"))
    grid_imports = _imports(_parsed("bright_grid_calibration.py"))
    detection_imports = _imports(_parsed("bright_grid_detection.py"))
    stage_imports = _imports(_parsed("stage_geometry_fit.py"))
    seam_imports = _imports(_parsed("seam_radial_fit.py"))

    assert not any(
        name.endswith("_fit") or name.endswith("_calibration")
        for name in distortion_imports
    )
    assert "probe_station_gui.camera.distortion" in grid_imports
    assert "probe_station_gui.camera.bright_grid_detection" in grid_imports
    assert "probe_station_gui.camera.distortion" not in detection_imports
    assert "probe_station_gui.camera.distortion" in stage_imports
    assert "probe_station_gui.camera.bright_grid_detection" in stage_imports
    assert "probe_station_gui.camera.distortion" in seam_imports
    assert "probe_station_gui.camera.stage_geometry_fit" not in grid_imports
    assert "probe_station_gui.camera.seam_radial_fit" not in grid_imports
    assert "probe_station_gui.camera.stage_geometry_fit" not in seam_imports
    assert "probe_station_gui.camera.bright_grid_calibration" not in seam_imports


def test_runtime_distortion_import_stays_free_of_fitters_and_heavy_dependencies() -> (
    None
):
    script = """
import sys
import probe_station_gui.camera.distortion
for name in (
    'probe_station_gui.camera.bright_grid_calibration',
    'probe_station_gui.camera.bright_grid_detection',
    'probe_station_gui.camera.stage_geometry_fit',
    'probe_station_gui.camera.seam_radial_fit',
    'probe_station_gui.camera.imaging',
    'cv2',
    'scipy',
    'scipy.optimize',
):
    assert name not in sys.modules, name
import probe_station_gui.camera.optical_calibration_geometry
for name in (
    'probe_station_gui.camera.bright_grid_calibration',
    'probe_station_gui.camera.bright_grid_detection',
    'probe_station_gui.camera.seam_radial_fit',
    'probe_station_gui.camera.geometry_segmentation',
    'probe_station_gui.camera.geometry_feature_tracking',
    'probe_station_gui.camera.geometry_alignment_preview',
    'cv2',
    'scipy',
    'scipy.optimize',
    'scipy.spatial',
):
    assert name not in sys.modules, name
"""
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
