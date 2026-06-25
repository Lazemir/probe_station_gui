"""Microscope image annotation, metadata, route capture, and scan planning."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen


Point2D = tuple[float, float]


@dataclass(frozen=True)
class MicroscopeScaleCalibration:
    """Physical pixel size for one captured microscope frame."""

    pixel_size_x_um: float
    pixel_size_y_um: float
    source: str = ""

    @property
    def pixel_size_x_mm(self) -> float:
        return float(self.pixel_size_x_um) / 1000.0

    @property
    def pixel_size_y_mm(self) -> float:
        return float(self.pixel_size_y_um) / 1000.0

    def to_dict(self) -> dict[str, object]:
        return {
            "PixelSize": [float(self.pixel_size_x_um), float(self.pixel_size_y_um)],
            "PixelSizeUnits": "um",
            "PhysicalSizeX": float(self.pixel_size_x_um),
            "PhysicalSizeXUnit": "um",
            "PhysicalSizeY": float(self.pixel_size_y_um),
            "PhysicalSizeYUnit": "um",
            "source": self.source,
        }


@dataclass(frozen=True)
class MicroscopeImageMetadata:
    """Metadata written into the image overlay and a JSON sidecar."""

    title: str
    mode: str
    captured_at: str
    objective_name: str = ""
    magnification: float | None = None
    route_name: str = ""
    route_point_index: int | None = None
    route_point_label: str = ""
    route_position: int | None = None
    route_total: int | None = None
    scan_tile_index: int | None = None
    scan_tile_total: int | None = None
    scan_row: int | None = None
    scan_column: int | None = None
    design_xy: Point2D | None = None
    stage_position: tuple[float, ...] | None = None
    stage_xy: Point2D | None = None
    image_size_px: tuple[int, int] | None = None
    fov_um: Point2D | None = None
    notes: tuple[str, ...] = ()
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["standard"] = {
            "metadata_sidecar": "BIDS microscopy style PixelSize/PixelSizeUnits",
            "pixel_size_reference": "OME PhysicalSizeX/PhysicalSizeY",
        }
        return data


@dataclass(frozen=True)
class MicroscopeCaptureResult:
    """Saved microscope image plus the raw source frame used for mosaics."""

    image_path: Path
    metadata_path: Path
    raw_image: QImage
    metadata: dict[str, object]


@dataclass(frozen=True)
class MicroscopeScanTile:
    """One stage position in a design scan plan."""

    index: int
    row: int
    column: int
    stage_xy: Point2D


@dataclass(frozen=True)
class MicroscopeScanPlan:
    """Tile grid that covers an axis-aligned stage-space design bounds."""

    tiles: tuple[MicroscopeScanTile, ...]
    stage_bounds: tuple[float, float, float, float]
    covered_stage_bounds: tuple[float, float, float, float]
    fov_size_mm: Point2D
    overlap_fraction: float
    row_count: int
    column_count: int


def utc_timestamp() -> str:
    """Return an ISO timestamp suitable for metadata sidecars."""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def objective_scale_calibration(objective: object) -> MicroscopeScaleCalibration | None:
    """Build pixel-size metadata from an objective profile's pixels_to_mm matrix."""

    if not bool(getattr(objective, "xy_calibration_configured", False)):
        return None
    matrix = getattr(objective, "pixels_to_mm", None)
    try:
        xx = float(matrix[0][0])
        xy = float(matrix[1][0])
        yx = float(matrix[0][1])
        yy = float(matrix[1][1])
    except (TypeError, ValueError, IndexError):
        return None
    pixel_x_mm = math.hypot(xx, xy)
    pixel_y_mm = math.hypot(yx, yy)
    if not (
        math.isfinite(pixel_x_mm)
        and math.isfinite(pixel_y_mm)
        and pixel_x_mm > 0.0
        and pixel_y_mm > 0.0
    ):
        return None
    name = str(getattr(objective, "name", "") or "")
    return MicroscopeScaleCalibration(
        pixel_size_x_um=pixel_x_mm * 1000.0,
        pixel_size_y_um=pixel_y_mm * 1000.0,
        source=f"objective:{name}" if name else "objective",
    )


def save_microscope_image(
    *,
    frame: QImage,
    output_dir: str | Path,
    filename_stem: str,
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
) -> MicroscopeCaptureResult:
    """Save an annotated PNG and a JSON sidecar with calibrated pixel metadata."""

    if frame.isNull():
        raise ValueError("Cannot save an empty microscope frame.")
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    safe_stem = safe_filename_component(filename_stem) or "microscope_image"
    image_path = _deduplicated_path(directory / f"{safe_stem}.png")
    sidecar_path = image_path.with_suffix(".json")

    raw = frame.convertToFormat(QImage.Format_RGB888).copy()
    metadata = _metadata_with_image_size(metadata, raw, scale)
    annotated = render_microscope_overlay(raw, metadata, scale)
    sidecar = _sidecar_payload(metadata, scale, image_path.name)
    annotated.setText("ProbeStationGUI.Metadata", json.dumps(sidecar, ensure_ascii=True))
    if not annotated.save(str(image_path), "PNG"):
        raise OSError(f"Unable to save microscope image to {image_path}.")
    with sidecar_path.open("w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=2, ensure_ascii=False)
    return MicroscopeCaptureResult(
        image_path=image_path,
        metadata_path=sidecar_path,
        raw_image=raw,
        metadata=sidecar,
    )


def render_microscope_overlay(
    frame: QImage,
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
) -> QImage:
    """Return a copy of frame with a SEM-style data band and scale bar."""

    image = frame.convertToFormat(QImage.Format_RGB32).copy()
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    try:
        _draw_data_band(painter, image, metadata, scale)
    finally:
        painter.end()
    return image


def build_design_scan_plan(
    *,
    stage_bounds: tuple[float, float, float, float],
    fov_size_mm: Point2D,
    overlap_fraction: float,
) -> MicroscopeScanPlan:
    """Build a serpentine tile plan covering stage-space design bounds."""

    left, bottom, right, top = _normalized_bounds(stage_bounds)
    fov_w = _positive_float(fov_size_mm[0], "FOV width")
    fov_h = _positive_float(fov_size_mm[1], "FOV height")
    overlap = min(max(float(overlap_fraction), 0.0), 0.95)
    step_x = fov_w * (1.0 - overlap)
    step_y = fov_h * (1.0 - overlap)
    x_centers = _axis_centers(left, right, fov_w, step_x)
    y_centers_bottom_to_top = _axis_centers(bottom, top, fov_h, step_y)
    y_centers_top_to_bottom = list(reversed(y_centers_bottom_to_top))

    tiles: list[MicroscopeScanTile] = []
    for row, y_value in enumerate(y_centers_top_to_bottom):
        row_columns = list(enumerate(x_centers))
        if row % 2 == 1:
            row_columns.reverse()
        for column, x_value in row_columns:
            tiles.append(
                MicroscopeScanTile(
                    index=len(tiles) + 1,
                    row=row,
                    column=column,
                    stage_xy=(float(x_value), float(y_value)),
                )
            )
    covered_left = min(x_centers) - fov_w * 0.5
    covered_right = max(x_centers) + fov_w * 0.5
    covered_bottom = min(y_centers_bottom_to_top) - fov_h * 0.5
    covered_top = max(y_centers_bottom_to_top) + fov_h * 0.5
    return MicroscopeScanPlan(
        tiles=tuple(tiles),
        stage_bounds=(left, bottom, right, top),
        covered_stage_bounds=(
            float(covered_left),
            float(covered_bottom),
            float(covered_right),
            float(covered_top),
        ),
        fov_size_mm=(fov_w, fov_h),
        overlap_fraction=overlap,
        row_count=len(y_centers_bottom_to_top),
        column_count=len(x_centers),
    )


def stitch_scan_tiles(
    *,
    plan: MicroscopeScanPlan,
    tile_images: Sequence[tuple[MicroscopeScanTile, QImage]],
    scale: MicroscopeScaleCalibration,
) -> QImage:
    """Place scan tiles into a single stage-coordinate mosaic."""

    if not tile_images:
        raise ValueError("No scan tiles were captured.")
    left, bottom, right, top = plan.covered_stage_bounds
    pixel_x_mm = scale.pixel_size_x_mm
    pixel_y_mm = scale.pixel_size_y_mm
    width_px = max(1, int(math.ceil((right - left) / pixel_x_mm)))
    height_px = max(1, int(math.ceil((top - bottom) / pixel_y_mm)))
    mosaic = QImage(width_px, height_px, QImage.Format_RGB32)
    mosaic.fill(Qt.black)
    painter = QPainter(mosaic)
    try:
        for tile, image in tile_images:
            raw = image.convertToFormat(QImage.Format_RGB32)
            tile_left = float(tile.stage_xy[0]) - plan.fov_size_mm[0] * 0.5
            tile_top = float(tile.stage_xy[1]) + plan.fov_size_mm[1] * 0.5
            x_px = int(round((tile_left - left) / pixel_x_mm))
            y_px = int(round((top - tile_top) / pixel_y_mm))
            painter.drawImage(x_px, y_px, raw)
    finally:
        painter.end()
    return mosaic


def stage_bounds_from_design_bounds(
    design_bounds: tuple[float, float, float, float],
    transform: Callable[[Point2D], Point2D | None],
) -> tuple[float, float, float, float]:
    """Project design bounds corners through a registration into stage bounds."""

    left, bottom, right, top = design_bounds
    corners = (
        (float(left), float(bottom)),
        (float(left), float(top)),
        (float(right), float(bottom)),
        (float(right), float(top)),
    )
    stage_points: list[Point2D] = []
    for corner in corners:
        point = transform(corner)
        if point is None:
            raise ValueError("Design registration is required for microscope scan.")
        stage_points.append((float(point[0]), float(point[1])))
    xs = [point[0] for point in stage_points]
    ys = [point[1] for point in stage_points]
    return (min(xs), min(ys), max(xs), max(ys))


def safe_filename_component(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text.strip("._")[:120]


def route_photo_filename(
    *,
    route_name: str,
    point_index: int,
    point_label: str,
    captured_at: str,
) -> str:
    stamp = _timestamp_for_filename(captured_at)
    label = safe_filename_component(point_label) or f"P{int(point_index):03d}"
    route = safe_filename_component(route_name) or "route"
    return f"{route}_point_{int(point_index):03d}_{label}_{stamp}"


def scan_tile_filename(
    *,
    scan_name: str,
    tile: MicroscopeScanTile,
    captured_at: str,
) -> str:
    stamp = _timestamp_for_filename(captured_at)
    name = safe_filename_component(scan_name) or "design_scan"
    return (
        f"{name}_tile_{tile.index:04d}_"
        f"r{tile.row + 1:03d}_c{tile.column + 1:03d}_{stamp}"
    )


def _draw_data_band(
    painter: QPainter,
    image: QImage,
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
) -> None:
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return
    font_size = max(8, min(13, width // 150))
    base_font = QFont()
    base_font.setPointSize(font_size)
    painter.setFont(base_font)
    line_height = float(font_size + 8)
    band_height = max(74, int(line_height * 3 + 24))
    band_top = max(0, height - band_height)
    band = QRectF(0.0, float(band_top), float(width), float(height - band_top))
    painter.fillRect(band, QColor(0, 0, 0, 220))
    painter.setPen(QPen(QColor(255, 255, 255, 210), 1))
    painter.drawLine(QPointF(0.0, float(band_top)), QPointF(float(width), float(band_top)))

    margin = 14.0
    scale_bar_width = min(max(width * 0.30, 170.0), 360.0)
    scale_rect = QRectF(
        margin,
        float(band_top) + 10.0,
        scale_bar_width,
        max(42.0, band.height() - 18.0),
    )
    _draw_scale_bar(painter, scale_rect, scale)

    text_left = scale_rect.right() + 18.0
    text_rect_width = max(40.0, float(width) - text_left - margin)
    text_top = float(band_top) + 8.0
    lines = _metadata_overlay_lines(metadata, scale)
    painter.setPen(QPen(QColor(255, 255, 255), 1))
    for index, line in enumerate(lines[:3]):
        rect = QRectF(
            text_left,
            text_top + index * line_height,
            text_rect_width,
            line_height,
        )
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, line)


def _draw_scale_bar(
    painter: QPainter,
    rect: QRectF,
    scale: MicroscopeScaleCalibration,
) -> None:
    pixel_size_um = float(scale.pixel_size_x_um)
    if pixel_size_um <= 0.0 or not math.isfinite(pixel_size_um):
        return
    target_px = max(70.0, min(rect.width() * 0.72, 220.0))
    length_um = _nice_length_um(target_px * pixel_size_um)
    bar_px = length_um / pixel_size_um
    if bar_px <= 0.0 or bar_px > rect.width() - 18.0:
        return
    bar_x = rect.left() + 4.0
    bar_y = rect.bottom() - 17.0
    tick = 8.0
    label = _format_um(length_um)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, False)
    painter.setPen(QPen(QColor(255, 255, 255), 5.0))
    painter.drawLine(QPointF(bar_x, bar_y), QPointF(bar_x + bar_px, bar_y))
    painter.setPen(QPen(QColor(255, 255, 255), 2.0))
    painter.drawLine(QPointF(bar_x, bar_y - tick), QPointF(bar_x, bar_y + tick))
    painter.drawLine(
        QPointF(bar_x + bar_px, bar_y - tick),
        QPointF(bar_x + bar_px, bar_y + tick),
    )
    font = painter.font()
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(
        QRectF(bar_x, rect.top(), max(bar_px, 72.0), 24.0),
        Qt.AlignLeft | Qt.AlignVCenter,
        label,
    )
    painter.restore()


def _metadata_overlay_lines(
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
) -> list[str]:
    objective = metadata.objective_name or "objective unknown"
    if metadata.magnification is not None and math.isfinite(float(metadata.magnification)):
        objective = f"{objective} {float(metadata.magnification):g}x"
    point = ""
    if metadata.route_point_index is not None:
        label = metadata.route_point_label or f"P{metadata.route_point_index}"
        point = f" | point {metadata.route_point_index} {label}"
    if metadata.scan_tile_index is not None and metadata.scan_tile_total is not None:
        point = f" | tile {metadata.scan_tile_index}/{metadata.scan_tile_total}"
    fov = ""
    if metadata.fov_um is not None:
        fov = f" | FoV {metadata.fov_um[0]:.1f} x {metadata.fov_um[1]:.1f} um"
    stage = _format_stage_position(metadata.stage_position, metadata.stage_xy)
    design = ""
    if metadata.design_xy is not None:
        design = f" | design X={metadata.design_xy[0]:.3f} Y={metadata.design_xy[1]:.3f}"
    return [
        f"{metadata.title}{point}",
        (
            f"{metadata.captured_at} | {metadata.mode} | {objective} | "
            f"pixel {scale.pixel_size_x_um:.4g} x {scale.pixel_size_y_um:.4g} um"
            f"{fov}"
        ),
        f"{stage}{design}".strip(" |") or "stage position unavailable",
    ]


def _format_stage_position(
    stage_position: tuple[float, ...] | None,
    stage_xy: Point2D | None,
) -> str:
    values: list[float] = []
    if stage_position is not None:
        values = [float(value) for value in stage_position]
    elif stage_xy is not None:
        values = [float(stage_xy[0]), float(stage_xy[1])]
    labels = ("X", "Y", "Z", "A", "B", "C")
    parts: list[str] = []
    for index, value in enumerate(values[: len(labels)]):
        if math.isfinite(value):
            parts.append(f"{labels[index]}={value:.4f}")
    return "stage " + " ".join(parts) if parts else ""


def _metadata_with_image_size(
    metadata: MicroscopeImageMetadata,
    image: QImage,
    scale: MicroscopeScaleCalibration,
) -> MicroscopeImageMetadata:
    fov_um = (
        float(image.width()) * scale.pixel_size_x_um,
        float(image.height()) * scale.pixel_size_y_um,
    )
    return MicroscopeImageMetadata(
        **{
            **asdict(metadata),
            "image_size_px": (int(image.width()), int(image.height())),
            "fov_um": fov_um,
        }
    )


def _sidecar_payload(
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
    image_name: str,
) -> dict[str, object]:
    return {
        "FileName": image_name,
        "MicroscopeImage": metadata.to_dict(),
        "Microscopy": scale.to_dict(),
    }


def _nice_length_um(target_um: float) -> float:
    if target_um <= 0.0 or not math.isfinite(target_um):
        return 1.0
    exponent = math.floor(math.log10(target_um))
    base = 10.0 ** exponent
    candidates = [1.0, 2.0, 5.0, 10.0]
    best = base
    for multiplier in candidates:
        value = multiplier * base
        if value <= target_um:
            best = value
    return float(best)


def _format_um(value_um: float) -> str:
    if value_um >= 1000.0:
        return f"{value_um / 1000.0:g} mm"
    if value_um >= 10.0:
        return f"{value_um:g} um"
    return f"{value_um:.3g} um"


def _axis_centers(minimum: float, maximum: float, fov: float, step: float) -> list[float]:
    span = max(0.0, float(maximum) - float(minimum))
    if span <= fov:
        return [(float(minimum) + float(maximum)) * 0.5]
    count = int(math.ceil((span - fov) / step)) + 1
    count = max(2, count)
    travel = span - fov
    return [
        float(minimum) + fov * 0.5 + travel * index / (count - 1)
        for index in range(count)
    ]


def _normalized_bounds(
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if len(bounds) != 4:
        raise ValueError("Bounds must contain left, bottom, right, top.")
    left = float(bounds[0])
    bottom = float(bounds[1])
    right = float(bounds[2])
    top = float(bounds[3])
    if not all(math.isfinite(value) for value in (left, bottom, right, top)):
        raise ValueError("Bounds contain non-finite values.")
    return (min(left, right), min(bottom, top), max(left, right), max(bottom, top))


def _positive_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number.") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{label} must be a positive number.")
    return number


def _deduplicated_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = path.with_name(f"{stem}_{index:03d}{suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"Unable to choose a unique file name near {path}.")


def _timestamp_for_filename(captured_at: str) -> str:
    text = str(captured_at or "").strip()
    text = text.replace(":", "").replace("-", "").replace("+", "_")
    text = text.replace("T", "_").replace(" ", "_")
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", text)
    return text.strip("_") or datetime.now().strftime("%Y%m%d_%H%M%S")


__all__ = [
    "MicroscopeCaptureResult",
    "MicroscopeImageMetadata",
    "MicroscopeScaleCalibration",
    "MicroscopeScanPlan",
    "MicroscopeScanTile",
    "build_design_scan_plan",
    "objective_scale_calibration",
    "render_microscope_overlay",
    "route_photo_filename",
    "save_microscope_image",
    "scan_tile_filename",
    "stage_bounds_from_design_bounds",
    "stitch_scan_tiles",
    "utc_timestamp",
]
