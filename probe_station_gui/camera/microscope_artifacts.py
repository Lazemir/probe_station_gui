"""Microscope artifact rendering, filenames, and persistence."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

from probe_station_gui.camera.imaging import (
    MicroscopeScaleCalibration,
    MicroscopeScanTile,
    Point2D,
)


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
    raw_image_path: Path | None = None


def save_microscope_image(
    *,
    frame: QImage,
    output_dir: str | Path,
    filename_stem: str,
    metadata: MicroscopeImageMetadata,
    scale: MicroscopeScaleCalibration,
    save_raw: bool = False,
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
    annotated.setText(
        "ProbeStationGUI.Metadata", json.dumps(sidecar, ensure_ascii=True)
    )
    if not annotated.save(str(image_path), "PNG"):
        raise OSError(f"Unable to save microscope image to {image_path}.")
    raw_image_path: Path | None = None
    if save_raw:
        raw_image_path = _deduplicated_path(
            image_path.with_name(f"{image_path.stem}_raw{image_path.suffix}")
        )
        if not raw.save(str(raw_image_path), "PNG"):
            raise OSError(f"Unable to save raw microscope image to {raw_image_path}.")
    with sidecar_path.open("w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=2, ensure_ascii=False)
    return MicroscopeCaptureResult(
        image_path=image_path,
        metadata_path=sidecar_path,
        raw_image=raw,
        metadata=sidecar,
        raw_image_path=raw_image_path,
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
    label = safe_filename_component(getattr(tile, "label", ""))
    label_part = f"_{label}" if label else ""
    return (
        f"{name}_tile_{tile.index:04d}_"
        f"r{tile.row + 1:03d}_c{tile.column + 1:03d}{label_part}_{stamp}"
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
    painter.drawLine(
        QPointF(0.0, float(band_top)), QPointF(float(width), float(band_top))
    )

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
    if metadata.magnification is not None and math.isfinite(
        float(metadata.magnification)
    ):
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
        design = (
            f" | design X={metadata.design_xy[0]:.3f} Y={metadata.design_xy[1]:.3f}"
        )
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
    base = 10.0**exponent
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
    "render_microscope_overlay",
    "route_photo_filename",
    "safe_filename_component",
    "save_microscope_image",
    "scan_tile_filename",
]
