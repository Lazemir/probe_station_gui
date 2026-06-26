"""Route Telegram/photo state and formatting helpers."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from probe_station_gui.route.finish_flow import (
    RouteFinishTelegramPlan,
    route_finish_telegram_text,
)
from probe_station_gui.route.measurement_records import (
    RouteMeasurementPoint,
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

    def should_capture_pre_contact_photo(self, *, route_attention_enabled: bool) -> bool:
        with self._lock:
            return self._contact_photo_requested or bool(route_attention_enabled)

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
        record: RouteMeasurementRecord,
        position: int,
        *,
        saved: bool,
        contact_attention: bool,
        photo: TelegramCaptionedPhoto,
    ) -> None:
        before_photo = self.matching_pre_contact_photo(point, position)
        with self._lock:
            if not saved or contact_attention:
                self._last_contact_failure_before_photo = before_photo
                self._last_contact_failure_photo = photo
            if self._contact_photo_requested:
                self._contact_photo_requested = False
                self._pending_contact_before_photo = before_photo
                self._pending_contact_photo = photo

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


__all__ = [
    "RouteTelegramPhotoState",
    "TelegramCaptionedPhoto",
    "TelegramPhoto",
    "combine_telegram_contact_photos",
    "route_attention_telegram_text",
    "route_contact_photo_caption",
    "route_finish_telegram_payload",
    "route_pre_contact_photo_caption",
    "route_requested_photo_caption",
    "route_start_telegram_text",
    "telegram_contact_photo_payload",
]
