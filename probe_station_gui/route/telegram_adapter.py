"""Route Telegram/photo state and formatting helpers."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from threading import Lock
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from probe_station_gui.camera.microscope_artifacts import (
    MicroscopeImageMetadata,
    route_photo_filename,
    save_microscope_image,
)
from probe_station_gui.camera.imaging import utc_timestamp as default_utc_timestamp
from probe_station_gui.route.artifact_rows import (
    ROUTE_PHOTO_FOCUS_MAP_FIELDS,
    route_photo_focus_map_path,
    route_photo_focus_map_row,
)
from probe_station_gui.route.finish_flow import (
    RouteFinishTelegramPlan,
    route_finish_telegram_text,
)
from probe_station_gui.route.measurement_records import (
    RouteMeasurementPoint,
    RoutePhotoRecord,
    RouteMeasurementRecord,
)

TelegramPhoto = tuple[bytes, str]
TelegramCaptionedPhoto = tuple[bytes, str, str]
_StoredPreContactPhoto = tuple[int, int, bytes, str, str]


def route_start_telegram_text(
    start_message: str,
    csv_path: str | None = None,
    *,
    api_session: bool = False,
) -> str:
    heading = (
        "Probe route API session started:"
        if api_session
        else "Probe route started:"
    )
    text = f"{heading}\n{start_message}"
    if not api_session and csv_path:
        text = f"{text}\nCSV: {csv_path}"
    return text


def route_finish_telegram_payload(
    telegram: RouteFinishTelegramPlan | None,
    *,
    session_measurement_count: int | None,
) -> tuple[str, dict[str, object]] | None:
    if telegram is None:
        return None
    telegram_message = route_finish_telegram_text(
        telegram,
        csv_record_count=session_measurement_count,
    )
    telegram_kwargs: dict[str, object] = {}
    if telegram.document_path is not None or telegram.key == "route_completed":
        telegram_kwargs["document_path"] = (
            Path(telegram.document_path) if telegram.document_path else None
        )
    if telegram.attach_photo:
        telegram_kwargs["attach_photo"] = True
    return telegram_message, telegram_kwargs


def route_photo_focus_payload(focus_result: object | None) -> dict[str, object] | None:
    if focus_result is None:
        return None
    if isinstance(focus_result, dict):
        return dict(focus_result)
    to_dict = getattr(focus_result, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return dict(data) if isinstance(data, dict) else None
    return None


def capture_route_photo(
    point: RouteMeasurementPoint,
    position: int,
    total: int,
    *,
    photo_only_mode: bool,
    photo_output_dir: str | Path,
    photo_autofocus_enabled: bool,
    photo_autofocus_range_mm: float,
    route_name: str,
    focus_result: object | None,
    active_microscope_scale: Callable[[], object | None],
    latest_camera_counter: Callable[[], int],
    wait_for_camera_frame: Callable[..., tuple[QImage | None, int]],
    timestamp_utc: Callable[[], str] = default_utc_timestamp,
    active_objective_metadata: Callable[[], tuple[str, float | None]] = lambda: ("", None),
    stage_position_for_image_metadata: Callable[..., tuple[float, ...] | None] = lambda **_kwargs: None,
    save_image: Callable[..., object] = save_microscope_image,
    route_photo_focus_payload: Callable[[object | None], dict[str, object] | None] = route_photo_focus_payload,
) -> str:
    scale = active_microscope_scale()
    if scale is None:
        raise RuntimeError("Active objective has no calibrated microscope scale.")
    before_counter = latest_camera_counter()
    frame, _counter = wait_for_camera_frame(
        after_counter=before_counter,
        timeout_s=2.0,
    )
    if frame is None:
        raise RuntimeError("Camera frame is unavailable.")
    captured_at = timestamp_utc()
    objective_name, magnification = active_objective_metadata()
    filename = route_photo_filename(
        route_name=route_name,
        point_index=int(point.index),
        point_label=point.label,
        captured_at=captured_at,
    )
    photo_stage_xy = (
        point.photo_stage_xy
        if point.photo_stage_xy is not None
        else point.stage_xy
    )
    stage_position = stage_position_for_image_metadata(stage_xy=photo_stage_xy)
    focus_data = route_photo_focus_payload(focus_result)
    metadata = MicroscopeImageMetadata(
        title="Probe Station Microscope",
        mode="route photo" if photo_only_mode else "route photo before measurement",
        captured_at=captured_at,
        objective_name=objective_name,
        magnification=magnification,
        route_name=route_name,
        route_point_index=int(point.index),
        route_point_label=point.label,
        route_position=int(position),
        route_total=int(total),
        design_xy=point.design_center,
        stage_position=stage_position,
        stage_xy=photo_stage_xy,
        notes=(
            "needles raised before capture",
            *(("local autofocus before capture",) if photo_autofocus_enabled else ()),
        ),
        extra={
            "point_id": point.point_id,
            "needle_1_design": list(point.needle_1_design),
            "needle_2_design": list(point.needle_2_design),
            "photo_autofocus_enabled": bool(photo_autofocus_enabled),
            "photo_autofocus_range_mm": float(photo_autofocus_range_mm),
            "autofocus": focus_data,
        },
    )
    result = save_image(
        frame=frame,
        output_dir=photo_output_dir,
        filename_stem=filename,
        metadata=metadata,
        scale=scale,
    )
    return str(getattr(result, "image_path"))


def telegram_contact_photo_payload(
    before_photo: TelegramCaptionedPhoto | None,
    after_photo: TelegramCaptionedPhoto,
    *,
    combine_photos: Callable[[bytes, bytes], TelegramPhoto | None],
) -> tuple[TelegramPhoto, str]:
    caption = _combined_route_contact_caption(
        before_photo[2] if before_photo is not None else "",
        after_photo[2],
    )
    if before_photo is None:
        return (after_photo[0], after_photo[1]), caption
    combined_photo = combine_photos(before_photo[0], after_photo[0])
    if combined_photo is None:
        return (after_photo[0], after_photo[1]), caption
    return combined_photo, caption


def combine_telegram_contact_photos(
    before_bytes: bytes,
    after_bytes: bytes,
    *,
    encode_image: Callable[[QImage | None], TelegramPhoto | None],
) -> TelegramPhoto | None:
    before_image = QImage()
    after_image = QImage()
    if not before_image.loadFromData(before_bytes):
        return None
    if not after_image.loadFromData(after_bytes):
        return None
    if before_image.isNull() or after_image.isNull():
        return None
    target_height = min(before_image.height(), after_image.height())
    if target_height <= 0:
        return None
    if before_image.height() != target_height:
        before_image = before_image.scaledToHeight(
            target_height,
            Qt.TransformationMode.SmoothTransformation,
        )
    if after_image.height() != target_height:
        after_image = after_image.scaledToHeight(
            target_height,
            Qt.TransformationMode.SmoothTransformation,
        )
    combined = QImage(
        before_image.width() + after_image.width(),
        target_height,
        QImage.Format.Format_RGB32,
    )
    combined.fill(Qt.GlobalColor.black)
    painter = QPainter(combined)
    painter.drawImage(0, 0, before_image)
    painter.drawImage(before_image.width(), 0, after_image)
    painter.end()
    encoded = encode_image(combined)
    if encoded is None:
        return None
    return encoded[0], "route-contact-comparison.jpg"


class RouteTelegramPhotoState:
    def __init__(self, *, lock: Lock | None = None) -> None:
        self._lock = lock or Lock()
        self._route_photo_requested = False
        self._contact_photo_requested = False
        self._pending_contact_before_photo: TelegramCaptionedPhoto | None = None
        self._pending_contact_photo: TelegramCaptionedPhoto | None = None
        self._last_pre_contact_photo: _StoredPreContactPhoto | None = None
        self._last_contact_failure_photo: TelegramCaptionedPhoto | None = None
        self._last_contact_failure_before_photo: TelegramCaptionedPhoto | None = None
        self._last_attention_message = ""

    def reset_for_route_start(self) -> None:
        with self._lock:
            self._pending_contact_photo = None
            self._pending_contact_before_photo = None
            self._last_pre_contact_photo = None
            self._last_contact_failure_photo = None
            self._last_contact_failure_before_photo = None
            self._last_attention_message = ""

    def clear_for_route_finish(self) -> None:
        with self._lock:
            self._route_photo_requested = False
            self._contact_photo_requested = False
            self._pending_contact_before_photo = None
            self._pending_contact_photo = None
            self._last_pre_contact_photo = None

    def request_route_photo(self) -> None:
        with self._lock:
            self._route_photo_requested = True

    def request_contact_photo(self) -> None:
        with self._lock:
            self._contact_photo_requested = True

    def consume_route_photo_request(self) -> bool:
        with self._lock:
            requested = self._route_photo_requested
            if requested:
                self._route_photo_requested = False
            return requested

    def record_route_photo(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
        *,
        route_name: str,
        send_bot_message: Callable[..., None],
        default_markup: object | None,
    ) -> None:
        self._maybe_send_requested_route_photo(
            record,
            position,
            total,
            send_bot_message=send_bot_message,
            default_markup=default_markup,
        )
        if not record.focus:
            return
        try:
            path = route_photo_focus_map_path(record)
            path.parent.mkdir(parents=True, exist_ok=True)
            exists = path.exists() and path.stat().st_size > 0
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=ROUTE_PHOTO_FOCUS_MAP_FIELDS,
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    route_photo_focus_map_row(
                        record,
                        position,
                        total,
                        route_name=route_name,
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            return

    def maybe_send_requested_route_photo(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
        *,
        send_bot_message: Callable[..., None],
        default_markup: object | None,
    ) -> None:
        self._maybe_send_requested_route_photo(
            record,
            position,
            total,
            send_bot_message=send_bot_message,
            default_markup=default_markup,
        )

    def _maybe_send_requested_route_photo(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
        *,
        send_bot_message: Callable[..., None],
        default_markup: object | None,
    ) -> None:
        if not self.consume_route_photo_request():
            return
        path = Path(record.path).expanduser()
        try:
            photo = (path.read_bytes(), path.name)
        except OSError:
            send_bot_message(
                f"Route photo is saved but could not be read for Telegram: {path}",
                reply_markup=default_markup,
            )
            return
        send_bot_message(
            route_requested_photo_caption(
                position=int(position),
                total=int(total),
                structure_number=int(record.structure_number),
                label=record.label,
            ),
            photo=photo,
            reply_markup=default_markup,
        )

    def should_capture_pre_contact_photo(self, *, route_attention_enabled: bool) -> bool:
        with self._lock:
            return self._contact_photo_requested or bool(route_attention_enabled)

    def capture_pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        structure_number: int,
        route_attention_enabled: bool,
        latest_camera_counter: Callable[[], int],
        wait_for_camera_frame: Callable[..., tuple[QImage | None, int]],
        qimage_telegram_photo: Callable[[QImage | None], TelegramPhoto | None],
        latest_camera_frame_photo: Callable[[], TelegramPhoto | None],
    ) -> None:
        if not self.should_capture_pre_contact_photo(
            route_attention_enabled=route_attention_enabled
        ):
            return
        before_counter = latest_camera_counter()
        frame, _counter = wait_for_camera_frame(
            after_counter=before_counter,
            timeout_s=0.5,
        )
        photo = qimage_telegram_photo(frame) or latest_camera_frame_photo()
        if photo is None:
            return
        caption = route_pre_contact_photo_caption(
            position=int(position),
            total=int(total),
            structure_number=int(structure_number),
            label=point.label,
        )
        self.store_pre_contact_photo(point, position, (photo[0], photo[1], caption))

    def should_capture_contact_photo(
        self,
        *,
        saved: bool,
        contact_attention: bool,
        route_attention_enabled: bool,
    ) -> bool:
        with self._lock:
            return bool(
                self._contact_photo_requested
                or not saved
                or (contact_attention and route_attention_enabled)
            )

    def store_pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        photo: TelegramCaptionedPhoto,
    ) -> None:
        with self._lock:
            self._last_pre_contact_photo = (
                int(position),
                int(point.index),
                photo[0],
                photo[1],
                photo[2],
            )

    def matching_pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> TelegramCaptionedPhoto | None:
        with self._lock:
            pre_photo = self._last_pre_contact_photo
        if pre_photo is None:
            return None
        photo_position, point_index, photo_bytes, photo_name, caption = pre_photo
        if photo_position != int(position) or point_index != int(point.index):
            return None
        return photo_bytes, photo_name, caption

    def store_contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord | None,
        position: int,
        *,
        saved: bool,
        contact_attention: bool,
        photo: TelegramCaptionedPhoto,
    ) -> None:
        _ = record
        before_photo = self.matching_pre_contact_photo(point, position)
        with self._lock:
            if not saved or contact_attention:
                self._last_contact_failure_before_photo = before_photo
                self._last_contact_failure_photo = photo
            if self._contact_photo_requested:
                self._contact_photo_requested = False
                self._pending_contact_before_photo = before_photo
                self._pending_contact_photo = photo

    def capture_contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        *,
        saved: bool,
        contact_attention: bool,
        route_attention_enabled: bool,
        latest_camera_counter: Callable[[], int],
        wait_for_camera_frame: Callable[..., tuple[QImage | None, int]],
        qimage_telegram_photo: Callable[[QImage | None], TelegramPhoto | None],
        latest_camera_frame_photo: Callable[[], TelegramPhoto | None],
    ) -> None:
        if not self.should_capture_contact_photo(
            saved=bool(saved),
            contact_attention=bool(contact_attention),
            route_attention_enabled=bool(route_attention_enabled),
        ):
            return
        before_counter = latest_camera_counter()
        frame, _counter = wait_for_camera_frame(
            after_counter=before_counter,
            timeout_s=1.0,
        )
        photo = qimage_telegram_photo(frame) or latest_camera_frame_photo()
        if photo is None:
            return
        caption = route_contact_photo_caption(
            position=int(position),
            total=int(total),
            structure_number=int(record.structure_number),
            label=point.label,
            status=str(record.status),
        )
        self.store_contact_photo(
            point,
            record,
            position,
            saved=bool(saved),
            contact_attention=bool(contact_attention),
            photo=(photo[0], photo[1], caption),
        )

    def take_pending_contact_photos(
        self,
    ) -> tuple[TelegramCaptionedPhoto | None, TelegramCaptionedPhoto] | None:
        with self._lock:
            before_photo = self._pending_contact_before_photo
            after_photo = self._pending_contact_photo
            self._pending_contact_before_photo = None
            self._pending_contact_photo = None
            if after_photo is None:
                return None
            return before_photo, after_photo

    def latest_contact_failure_photos(
        self,
    ) -> tuple[TelegramCaptionedPhoto | None, TelegramCaptionedPhoto] | None:
        with self._lock:
            after_photo = self._last_contact_failure_photo
            if after_photo is None:
                return None
            return self._last_contact_failure_before_photo, after_photo

    def should_send_attention(self, message: str) -> bool:
        with self._lock:
            if message == self._last_attention_message:
                return False
            self._last_attention_message = message
            return True

    def send_route_attention_alert(
        self,
        message: str,
        *,
        include_contact_photos: bool,
        failure_photos: tuple[TelegramCaptionedPhoto | None, TelegramCaptionedPhoto]
        | None = None,
        contact_photo_payload: Callable[
            [TelegramCaptionedPhoto | None, TelegramCaptionedPhoto],
            tuple[TelegramPhoto, str],
        ],
        send_alert: Callable[..., None],
        route_actions_markup: object | None,
    ) -> None:
        if not self.should_send_attention(message):
            return
        if include_contact_photos and failure_photos is None:
            failure_photos = self.latest_contact_failure_photos()
        before_photo = failure_photos[0] if failure_photos is not None else None
        failure_photo = failure_photos[1] if failure_photos is not None else None
        alert_photo: TelegramPhoto | None = None
        contact_caption = ""
        if failure_photo is not None:
            alert_photo, contact_caption = contact_photo_payload(
                before_photo,
                failure_photo,
            )
        send_alert(
            "route_attention",
            route_attention_telegram_text(message, contact_caption),
            attach_photo=alert_photo is None,
            photo=alert_photo,
            reply_markup=route_actions_markup,
        )


def route_requested_photo_caption(
    *,
    position: int,
    total: int,
    structure_number: int,
    label: str,
) -> str:
    return (
        "Next route structure photo:\n"
        f"Point {position}/{total}, structure {structure_number}, {label}."
    )


def route_pre_contact_photo_caption(
    *,
    position: int,
    total: int,
    structure_number: int,
    label: str,
) -> str:
    return (
        "Route contact before needle press:\n"
        f"Point {position}/{total}, structure {structure_number}, {label}."
    )


def route_contact_photo_caption(
    *,
    position: int,
    total: int,
    structure_number: int,
    label: str,
    status: str,
) -> str:
    return (
        "Route contact attempt photo:\n"
        f"Point {position}/{total}, structure {structure_number}, "
        f"{label}, status={status}."
    )


def route_attention_telegram_text(message: str, contact_caption: str = "") -> str:
    alert_message = f"Probe route needs attention:\n{message}"
    if contact_caption:
        return f"{alert_message}\n{contact_caption}"
    return alert_message


def _combined_route_contact_caption(before_caption: str, after_caption: str) -> str:
    after_detail = "\n".join(str(after_caption or "").splitlines()[1:]).strip()
    if not after_detail:
        after_detail = str(after_caption or "").strip()
    if before_caption:
        prefix = (
            "Route contact check:\n"
            "Left: before needle press. Right: contact attempt."
        )
        return f"{prefix}\n{after_detail}" if after_detail else prefix
    return str(after_caption or "").strip()


def route_telegram_state_from_legacy_owner(owner: object) -> RouteTelegramPhotoState:
    state = RouteTelegramPhotoState(lock=getattr(owner, "_telegram_photo_lock", None))
    if getattr(owner, "_telegram_route_photo_requested", False):
        state.request_route_photo()
    if getattr(owner, "_telegram_contact_photo_requested", False):
        state.request_contact_photo()
    state._pending_contact_before_photo = getattr(
        owner,
        "_telegram_pending_contact_before_photo",
        None,
    )
    state._pending_contact_photo = getattr(
        owner,
        "_telegram_pending_contact_photo",
        None,
    )
    state._last_pre_contact_photo = getattr(
        owner,
        "_last_route_pre_contact_photo",
        None,
    )
    state._last_contact_failure_photo = getattr(
        owner,
        "_last_route_contact_failure_photo",
        None,
    )
    state._last_contact_failure_before_photo = getattr(
        owner,
        "_last_route_contact_failure_before_photo",
        None,
    )
    state._last_attention_message = str(
        getattr(owner, "_last_telegram_attention_message", "") or ""
    )
    return state


__all__ = [
    "RouteTelegramPhotoState",
    "TelegramCaptionedPhoto",
    "TelegramPhoto",
    "combine_telegram_contact_photos",
    "capture_route_photo",
    "route_attention_telegram_text",
    "route_contact_photo_caption",
    "route_finish_telegram_payload",
    "route_photo_focus_payload",
    "route_pre_contact_photo_caption",
    "route_requested_photo_caption",
    "route_start_telegram_text",
    "route_telegram_state_from_legacy_owner",
    "telegram_contact_photo_payload",
]
