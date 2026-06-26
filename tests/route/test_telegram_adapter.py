import struct
import threading
import unittest
import zlib
from pathlib import Path

from PySide6.QtGui import QImage

from main import Main
from probe_station_gui.route.finish_flow import RouteFinishTelegramPlan
from probe_station_gui.route.measurement import (
    RouteContactQuality,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)
from probe_station_gui.route.telegram_adapter import (
    RouteTelegramPhotoState,
    combine_telegram_contact_photos,
    route_finish_telegram_payload,
    route_start_telegram_text,
    telegram_contact_photo_payload,
)


def _telegram_test_photo_bytes(width: int, height: int, fill: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    red = (fill >> 16) & 0xFF
    green = (fill >> 8) & 0xFF
    blue = fill & 0xFF
    row = b"\x00" + bytes((red, green, blue)) * int(width)
    header = struct.pack(">IIBBBBB", int(width), int(height), 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * int(height)))
        + chunk(b"IEND", b"")
    )


def _route_point(index: int = 3, label: str = "P003") -> RouteMeasurementPoint:
    return RouteMeasurementPoint(
        index=index,
        point_id=f"p{index:03d}",
        label=label,
        design_center=(0.0, 0.0),
        stage_xy=(1.0, 2.0),
        needle_1_design=(0.0, 0.0),
        needle_2_design=(0.0, 0.0),
    )


def _bad_contact_record() -> RouteMeasurementRecord:
    return RouteMeasurementRecord(
        timestamp="2026-06-03T17:21:46",
        structure_number=3,
        nplc="1",
        measurement_type="test",
        n_measurements=250,
        resistance_ohm=298000.0,
        resistance_rms_ohm=19900.0,
        relative_rms=0.0666,
        status="bad_contact",
        contact_quality=RouteContactQuality(
            assessed=True,
            good=False,
            status="bad_contact",
            median_ohm=297000.0,
            mad_sigma_ohm=19100.0,
            p95_abs_step_ohm=48300.0,
            span_ohm=102000.0,
            compliance_hits=0,
            polarity_sign_mismatch_count=2,
            reasons=("mad_sigma_too_high", "step_noise_too_high"),
        ),
    )


class RouteTelegramAdapterTest(unittest.TestCase):
    def test_route_start_telegram_text_for_gui_route(self) -> None:
        text = route_start_telegram_text(
            "Point 1/9 ready.",
            "C:/data/run.csv",
        )

        self.assertEqual(
            text,
            "Probe route started:\nPoint 1/9 ready.\nCSV: C:/data/run.csv",
        )

    def test_route_start_telegram_text_for_api_session(self) -> None:
        text = route_start_telegram_text(
            "Point 1/9 ready.",
            api_session=True,
        )

        self.assertEqual(
            text,
            "Probe route API session started:\nPoint 1/9 ready.",
        )

    def test_route_finish_telegram_payload_for_completed_route(self) -> None:
        plan = RouteFinishTelegramPlan(
            key="route_completed",
            heading="Probe route completed:",
            message="All done.",
            csv_path="C:/data/run.csv",
            include_csv_record_count=True,
            document_path="C:/data/run.csv",
        )

        payload = route_finish_telegram_payload(
            plan,
            session_measurement_count=7,
        )

        assert payload is not None
        text, kwargs = payload
        self.assertEqual(
            text,
            "Probe route completed:\nAll done.\n"
            "Session total: 7 measurements in CSV.\n"
            "CSV: C:/data/run.csv",
        )
        self.assertEqual(kwargs["document_path"], Path("C:/data/run.csv"))
        self.assertNotIn("attach_photo", kwargs)

    def test_route_finish_telegram_payload_for_failed_route_with_photo(self) -> None:
        plan = RouteFinishTelegramPlan(
            key="route_failed",
            heading="Probe route stopped or failed:",
            message="Failed.",
            attach_photo=True,
        )

        payload = route_finish_telegram_payload(
            plan,
            session_measurement_count=None,
        )

        assert payload is not None
        text, kwargs = payload
        self.assertEqual(text, "Probe route stopped or failed:\nFailed.")
        self.assertEqual(kwargs, {"attach_photo": True})

    def test_telegram_contact_photo_payload_returns_after_photo_when_before_missing(
        self,
    ) -> None:
        after = (
            b"after-bytes",
            "after.jpg",
            "Route contact attempt photo:\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )

        photo, caption = telegram_contact_photo_payload(
            None,
            after,
            combine_photos=lambda _before, _after: None,
        )

        self.assertEqual(photo, (b"after-bytes", "after.jpg"))
        self.assertEqual(
            caption,
            "Route contact attempt photo:\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )

    def test_telegram_contact_photo_payload_combines_before_and_after(self) -> None:
        before = (
            b"before-bytes",
            "before.jpg",
            "Route contact before needle press:\nPoint 1/2, structure 3, P003.",
        )
        after = (
            b"after-bytes",
            "after.jpg",
            "Route contact attempt photo:\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )

        photo, caption = telegram_contact_photo_payload(
            before,
            after,
            combine_photos=lambda _before, _after: (
                b"combined-bytes",
                "route-contact-comparison.jpg",
            ),
        )

        self.assertEqual(photo, (b"combined-bytes", "route-contact-comparison.jpg"))
        self.assertEqual(
            caption,
            "Route contact check:\n"
            "Left: before needle press. Right: contact attempt.\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )

    def test_telegram_contact_photo_payload_falls_back_to_after_photo_when_combination_fails(
        self,
    ) -> None:
        before = (
            b"before-bytes",
            "before.jpg",
            "Route contact before needle press:\nPoint 1/2, structure 3, P003.",
        )
        after = (
            b"after-bytes",
            "after.jpg",
            "Route contact attempt photo:\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )

        photo, caption = telegram_contact_photo_payload(
            before,
            after,
            combine_photos=lambda _before, _after: None,
        )

        self.assertEqual(photo, (b"after-bytes", "after.jpg"))
        self.assertEqual(
            caption,
            "Route contact check:\n"
            "Left: before needle press. Right: contact attempt.\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )

    def test_combine_telegram_contact_photos_side_by_side(self) -> None:
        combined = combine_telegram_contact_photos(
            _telegram_test_photo_bytes(8, 4, 0x00FF0000),
            _telegram_test_photo_bytes(2, 4, 0x0000FF00),
            encode_image=Main._qimage_telegram_photo,
        )

        self.assertIsNotNone(combined)
        assert combined is not None
        photo_bytes, photo_name = combined
        image = QImage()
        self.assertTrue(image.loadFromData(photo_bytes))
        self.assertEqual(photo_name, "route-contact-comparison.jpg")
        self.assertEqual(image.width(), 10)
        self.assertEqual(image.height(), 4)

    def test_pending_contact_photos_are_cleared_but_failure_photos_are_preserved(
        self,
    ) -> None:
        state = RouteTelegramPhotoState(lock=threading.Lock())
        point = _route_point()
        before = (
            b"before-bytes",
            "before.jpg",
            "Route contact before needle press:\nPoint 1/2, structure 3, P003.",
        )
        after = (
            b"after-bytes",
            "after.jpg",
            "Route contact attempt photo:\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )

        state.request_contact_photo()
        state.store_pre_contact_photo(point, 1, before)
        state.store_contact_photo(
            point,
            _bad_contact_record(),
            1,
            saved=False,
            contact_attention=True,
            photo=after,
        )

        self.assertEqual(state.take_pending_contact_photos(), (before, after))
        self.assertIsNone(state.take_pending_contact_photos())
        self.assertEqual(state.latest_contact_failure_photos(), (before, after))

    def test_matching_pre_contact_photo_requires_same_position_and_point_index(
        self,
    ) -> None:
        state = RouteTelegramPhotoState(lock=threading.Lock())
        point = _route_point(index=3, label="P003")
        before = (
            b"before-bytes",
            "before.jpg",
            "Route contact before needle press:\nPoint 1/2, structure 3, P003.",
        )

        state.store_pre_contact_photo(point, 1, before)

        self.assertEqual(state.matching_pre_contact_photo(point, 1), before)
        self.assertIsNone(state.matching_pre_contact_photo(point, 2))
        self.assertIsNone(
            state.matching_pre_contact_photo(_route_point(index=4, label="P004"), 1)
        )


if __name__ == "__main__":
    unittest.main()
