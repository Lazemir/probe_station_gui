import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtGui import QImage

from main import Main
from probe_station_gui.camera.microscope_artifacts import MicroscopeCaptureResult
from probe_station_gui.route.finish_flow import RouteFinishTelegramPlan
from probe_station_gui.route.measurement import (
    RouteContactQuality,
    RouteMeasurementPoint,
    RoutePhotoRecord,
    RouteMeasurementRecord,
)
from probe_station_gui.route.telegram_adapter import (
    RouteTelegramPhotoState,
    combine_telegram_contact_photos,
    capture_route_photo,
    route_telegram_state_from_legacy_owner,
    route_finish_telegram_payload,
    route_photo_focus_payload,
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
    def test_route_telegram_state_from_legacy_owner_preserves_flags_and_photos(
        self,
    ) -> None:
        owner = SimpleNamespace(
            _telegram_photo_lock=threading.Lock(),
            _telegram_route_photo_requested=True,
            _telegram_contact_photo_requested=True,
            _telegram_pending_contact_before_photo=(b"before", "before.jpg", ""),
            _telegram_pending_contact_photo=(b"after", "after.jpg", ""),
            _last_route_pre_contact_photo=(1, 2, b"pre", "pre.jpg", ""),
            _last_route_contact_failure_photo=(b"failure", "failure.jpg", ""),
            _last_route_contact_failure_before_photo=(b"before-failure", "bf.jpg", ""),
            _last_telegram_attention_message="attention",
        )

        state = route_telegram_state_from_legacy_owner(owner)

        self.assertTrue(state.consume_route_photo_request())
        self.assertEqual(
            state.take_pending_contact_photos(),
            ((b"before", "before.jpg", ""), (b"after", "after.jpg", "")),
        )
        self.assertEqual(
            state.latest_contact_failure_photos(),
            ((b"before-failure", "bf.jpg", ""), (b"failure", "failure.jpg", "")),
        )
        self.assertFalse(state.should_send_attention("attention"))

    def test_capture_route_photo_builds_route_metadata_and_returns_saved_path(
        self,
    ) -> None:
        point = RouteMeasurementPoint(
            index=3,
            point_id="p003",
            label="P003",
            design_center=(10.0, 20.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(9.5, 20.5),
            needle_2_design=(10.5, 19.5),
            photo_stage_xy=(1.5, 2.5),
        )
        saved: list[tuple[object, object, object, object]] = []
        frame = QImage(8, 4, QImage.Format.Format_RGB32)
        frame.fill(0x00FF00)

        path = capture_route_photo(
            point,
            1,
            2,
            photo_only_mode=True,
            photo_output_dir="photos",
            photo_autofocus_enabled=True,
            photo_autofocus_range_mm=0.03,
            route_name="route-a",
            focus_result={"focus_best_z_mm": 10.0},
            active_microscope_scale=lambda: 1.25,
            latest_camera_counter=lambda: 4,
            wait_for_camera_frame=lambda *, after_counter, timeout_s: (frame, 5),
            timestamp_utc=lambda: "2026-06-26T20:00:00Z",
            active_objective_metadata=lambda: ("X20", 20.0),
            stage_position_for_image_metadata=lambda *, stage_xy: (
                stage_xy[0],
                stage_xy[1],
                3.0,
            ),
            save_image=lambda *, frame, output_dir, filename_stem, metadata, scale: (
                saved.append((frame, output_dir, filename_stem, metadata, scale))
                or MicroscopeCaptureResult(
                    image_path=Path(output_dir) / f"{filename_stem}.jpg",
                    metadata_path=Path(output_dir) / f"{filename_stem}.json",
                    raw_image=frame,
                    metadata={},
                )
            ),
            route_photo_focus_payload=route_photo_focus_payload,
        )

        self.assertTrue(path.endswith(".jpg"))
        self.assertEqual(len(saved), 1)
        _saved_frame, output_dir, filename_stem, metadata, scale = saved[0]
        self.assertEqual(output_dir, "photos")
        self.assertEqual(scale, 1.25)
        self.assertIn("route-a", filename_stem)
        self.assertEqual(metadata.route_name, "route-a")
        self.assertEqual(metadata.route_position, 1)
        self.assertEqual(metadata.route_total, 2)
        self.assertEqual(metadata.route_point_label, "P003")
        self.assertEqual(metadata.stage_xy, (1.5, 2.5))
        self.assertEqual(metadata.extra["autofocus"]["focus_best_z_mm"], 10.0)

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

    def test_record_route_photo_writes_focus_map_and_sends_requested_photo(self) -> None:
        state = RouteTelegramPhotoState(lock=threading.Lock())
        sent: list[tuple[str, tuple[bytes, str] | None, object | None]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            photo_path = Path(tmpdir) / "photos" / "point-001.png"
            photo_path.parent.mkdir(parents=True, exist_ok=True)
            photo_path.write_bytes(b"route-photo")
            record = RoutePhotoRecord(
                timestamp="2026-05-29T12:00:00+03:00",
                path=str(photo_path),
                structure_number=1,
                point_index=1,
                point_id="p001",
                label="P001",
                design_center=(10.0, 20.0),
                stage_xy=(1.0, 2.0),
                focus={
                    "objective_name": "X20",
                    "focus_start_z_mm": 9.98,
                    "focus_best_z_mm": 10.0,
                    "focus_delta_um": 20.0,
                    "focus_score": 12.5,
                    "focus_sample_count": 7,
                    "focus_edge_peak": False,
                    "autofocus_range_mm": 0.03,
                    "autofocus_fine_step_mm": 0.005,
                    "autofocus_lower_z_mm": 9.95,
                    "autofocus_upper_z_mm": 10.01,
                },
            )

            state.request_route_photo()
            state.record_route_photo(
                record,
                1,
                3,
                route_name="route-a",
                send_bot_message=lambda message, *, photo=None, reply_markup=None: sent.append(
                    (message, photo, reply_markup)
                ),
                default_markup="markup",
            )

            focus_map_path = photo_path.parent / "route-photo-focus-map.csv"
            rows = focus_map_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(sent), 1)
        self.assertIn("Next route structure photo:", sent[0][0])
        self.assertEqual(sent[0][1], (b"route-photo", "point-001.png"))
        self.assertEqual(sent[0][2], "markup")
        self.assertTrue(any("route_name" in line for line in rows))
        self.assertTrue(any("route-a" in line for line in rows))

    def test_send_route_attention_alert_deduplicates_and_attaches_contact_photo(
        self,
    ) -> None:
        state = RouteTelegramPhotoState(lock=threading.Lock())
        alerts: list[tuple[str, str, dict[str, object]]] = []
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

        state.send_route_attention_alert(
            "Measured route point 1/2: status=bad_contact.",
            include_contact_photos=True,
            contact_photo_payload=lambda _before, _after: (
                (b"combined", "route-contact-comparison.jpg"),
                "Route contact check:\n"
                "Left: before needle press. Right: contact attempt.\n"
                "Point 1/2, structure 3, P003, status=bad_contact.",
            ),
            send_alert=lambda key, text, **kwargs: alerts.append((key, text, kwargs)),
            route_actions_markup="actions",
        )
        state.send_route_attention_alert(
            "Measured route point 1/2: status=bad_contact.",
            include_contact_photos=True,
            contact_photo_payload=lambda _before, _after: (
                (b"combined", "route-contact-comparison.jpg"),
                "ignored",
            ),
            send_alert=lambda key, text, **kwargs: alerts.append((key, text, kwargs)),
            route_actions_markup="actions",
        )

        self.assertEqual(len(alerts), 1)
        key, text, kwargs = alerts[0]
        self.assertEqual(key, "route_attention")
        self.assertIn("Probe route needs attention:", text)
        self.assertIn("Left: before needle press. Right: contact attempt.", text)
        self.assertFalse(kwargs["attach_photo"])
        self.assertEqual(kwargs["photo"], (b"combined", "route-contact-comparison.jpg"))
        self.assertEqual(kwargs["reply_markup"], "actions")


if __name__ == "__main__":
    unittest.main()
