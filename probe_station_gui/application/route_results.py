from __future__ import annotations

import csv
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

from probe_station_gui.design import route_editing
from probe_station_gui.route.api_artifacts import final_api_route_session_status
from probe_station_gui.route.finish_flow import (
    route_finish_outcome_plan,
    route_finish_signal_plan,
)
from probe_station_gui.route.formatting import (
    format_route_ohm as _format_route_ohm,
)
from probe_station_gui.route.formatting import (
    format_route_percent as _format_route_percent,
)
from probe_station_gui.route.measurement import RouteMeasurementRecord
from probe_station_gui.route.telegram_adapter import (
    combine_telegram_contact_photos,
    route_finish_telegram_payload,
    telegram_contact_photo_payload,
)
from probe_station_gui.views import main_window_connection_flow as connection_flow

logger = logging.getLogger("main")

if TYPE_CHECKING:
    from probe_station_gui.route.measurement_config import (
        RouteMeasurementRunConfiguration,
    )


class _MainRouteResultsMixin:
    def _on_route_measurement_result(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        self._last_route_measurement_result = (
            record,
            int(position),
            int(total),
            bool(saved),
        )
        self._route_runtime_presenter().set_result(record, position, total, saved)
        pending_contact_photos = (
            self._telegram_runtime.route_photos.take_pending_contact_photos()
        )
        if pending_contact_photos is not None:
            before_photo, after_photo = pending_contact_photos
            photo, caption = telegram_contact_photo_payload(
                before_photo,
                after_photo,
                combine_photos=lambda before_bytes, after_bytes: (
                    combine_telegram_contact_photos(
                        before_bytes,
                        after_bytes,
                        encode_image=self._qimage_telegram_photo,
                    )
                ),
            )
            if before_photo is not None and photo[1] != "route-contact-comparison.jpg":
                logger.warning("Unable to combine route contact photos for Telegram.")
            self._telegram_runtime.send_bot_message(
                caption,
                photo=photo,
                reply_markup=self._telegram_runtime.default_markup(
                    route_waiting=self._route_measurement_waiting
                ),
            )
        if not saved:
            message = self._format_route_measurement_record(
                record,
                position,
                total,
                saved=saved,
            )
            self._show_status(message, 8000)
            self._route_runtime_presenter().unsaved_result_status(message)

    @staticmethod
    def _route_record_needs_contact_attention(
        record: RouteMeasurementRecord,
    ) -> bool:
        if record.status == "bad_contact":
            return True
        contact = record.contact_quality
        return contact is not None and contact.good is False

    def _send_route_waiting_attention_from_last_result(self) -> None:
        last_result = self._last_route_measurement_result
        if last_result is None:
            return
        record, position, total, saved = last_result
        if not self._route_record_needs_contact_attention(record):
            return
        message = self._format_route_measurement_record(
            record,
            position,
            total,
            saved=saved,
        )
        self._send_route_attention_alert(message, include_contact_photos=True)

    def _send_route_attention_alert(
        self,
        message: str,
        *,
        include_contact_photos: bool = False,
    ) -> None:
        def build_contact_photo_payload(
            before_photo: tuple[bytes, str, str] | None,
            after_photo: tuple[bytes, str, str],
        ) -> tuple[tuple[bytes, str], str]:
            photo, caption = telegram_contact_photo_payload(
                before_photo,
                after_photo,
                combine_photos=lambda before_bytes, after_bytes: (
                    combine_telegram_contact_photos(
                        before_bytes,
                        after_bytes,
                        encode_image=self._qimage_telegram_photo,
                    )
                ),
            )
            if before_photo is not None and photo[1] != "route-contact-comparison.jpg":
                logger.warning("Unable to combine route contact photos for Telegram.")
            return photo, caption

        self._telegram_runtime.route_photos.send_route_attention_alert(
            message,
            include_contact_photos=include_contact_photos,
            failure_photos=(
                self._telegram_runtime.route_photos.latest_contact_failure_photos()
                if include_contact_photos
                else None
            ),
            contact_photo_payload=build_contact_photo_payload,
            send_alert=self._telegram_runtime.send_alert,
            route_actions_markup=self._telegram_runtime.route_actions_markup(),
        )

    def _on_route_measurement_recorded(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
    ) -> None:
        message = self._format_route_measurement_record(
            record,
            position,
            total,
            saved=True,
        )
        self._show_status(message)
        self._route_runtime_presenter().recorded_result_status(message)
        next_point_number = self._route_measurement_next_point_number(position)
        if next_point_number is not None:
            self._set_route_measurement_resume_point(next_point_number)

    @staticmethod
    def _format_route_measurement_record(
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        *,
        saved: bool,
    ) -> str:
        prefix = "Measured" if saved else "Rejected"
        if saved and record.status == "short":
            prefix = "Short"
        contact = record.contact_quality
        contact_text = _MainRouteResultsMixin._format_route_contact_diagnostics(contact)
        return (
            f"{prefix} route point {position}/{total}: "
            f"n={int(record.n_measurements)}, "
            f"R={_format_route_ohm(record.resistance_ohm)}, "
            f"RMS={_format_route_ohm(record.resistance_rms_ohm)}, "
            f"rel={_format_route_percent(record.relative_rms)}, "
            f"status={record.status}{contact_text}."
        )

    @staticmethod
    def _format_route_contact_diagnostics(contact: object | None) -> str:
        if contact is None or not bool(getattr(contact, "assessed", False)):
            return ""
        reasons = tuple(getattr(contact, "reasons", ()) or ())
        reason_text = (
            ", ".join(str(reason) for reason in reasons) if reasons else "none"
        )
        failure_criteria = tuple(getattr(contact, "failure_criteria", ()) or ())
        failure_text = (
            ", failed_criterion=" + " | ".join(str(item) for item in failure_criteria)
            if failure_criteria
            else ""
        )
        return (
            f", contact={getattr(contact, 'status', 'unknown')}, "
            f"reasons={reason_text}, "
            f"median={_format_route_ohm(float(getattr(contact, 'median_ohm', math.nan)))}, "
            f"MAD={_format_route_ohm(float(getattr(contact, 'mad_sigma_ohm', math.nan)))}, "
            f"p95_step={_format_route_ohm(float(getattr(contact, 'p95_abs_step_ohm', math.nan)))}, "
            f"span={_format_route_ohm(float(getattr(contact, 'span_ohm', math.nan)))}, "
            f"compliance_hits={int(getattr(contact, 'compliance_hits', 0))}, "
            "polarity_mismatches="
            f"{int(getattr(contact, 'polarity_sign_mismatch_count', 0))}"
            f"{failure_text}"
        )

    def _on_route_measurement_finished(self, *args: object) -> None:
        finish_signal = route_finish_signal_plan(
            args,
            current_runner=self._route_measurement_runner,
        )
        if finish_signal.ignored:
            return
        success = finish_signal.success
        message = finish_signal.message
        csv_path = finish_signal.csv_path
        measure_enabled = self._route_measurement_measure_enabled
        context_close_requested = bool(
            getattr(self, "_route_measurement_context_close_requested", False)
        )
        finish_plan = route_finish_outcome_plan(
            success=success,
            message=message,
            csv_path=csv_path,
            measure_enabled=measure_enabled,
            context_close_requested=context_close_requested,
            point_numbers=list(self._route_measurement_point_numbers),
            current_point=self._route_measurement_current_point,
        )
        self._route_measurement_context_close_requested = False
        self._join_finished_route_measurement_thread()
        runner = self._route_measurement_runner
        self._store_final_api_route_session_status(runner)
        self._clear_finished_route_measurement_state()
        self._update_stage_coordinate_apply_state()
        self._route_runtime_presenter().finished_ui(success, message)

        session_measurement_count = (
            self._route_measurement_csv_record_count(csv_path)
            if finish_plan.needs_csv_record_count
            else None
        )
        if finish_plan.resume_point is not None:
            self._set_route_measurement_resume_point(finish_plan.resume_point)
        self._set_route_measurement_pending(finish_plan.pending)
        if finish_plan.clear_point_numbers:
            self._route_measurement_point_numbers = []
        self._show_status(finish_plan.status_text, finish_plan.status_timeout_ms)
        telegram_payload = route_finish_telegram_payload(
            finish_plan.telegram,
            session_measurement_count=session_measurement_count,
        )
        if telegram_payload is not None and finish_plan.telegram is not None:
            telegram_message, telegram_kwargs = telegram_payload
            self._telegram_runtime.send_alert(
                finish_plan.telegram.key,
                telegram_message,
                **telegram_kwargs,
            )

    def _join_finished_route_measurement_thread(self) -> None:
        thread = self._route_measurement_thread
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._route_measurement_thread = None

    def _store_final_api_route_session_status(self, runner: object | None) -> None:
        status = final_api_route_session_status(
            getattr(self, "_api_route_session_id", None), runner, logger=logger
        )
        if status is not None:
            self._api_route_last_status = status

    def _clear_finished_route_measurement_state(self) -> None:
        self._route_measurement_runner = None
        self._api_route_lcr_controller = None
        self._route_measurement_runtime_configuration = None
        self._route_measurement_waiting = False
        self._route_measurement_waiting_reason = ""
        self._last_route_measurement_result = None
        self._pending_route_measure_point = None
        self._route_measurement_photo_enabled = False
        self._route_measurement_measure_enabled = False
        self._resume_resistance_standby_polling()
        self._telegram_runtime.route_photos.clear_for_route_finish()

    @staticmethod
    def _route_measurement_csv_record_count(csv_path: str | Path) -> int | None:
        try:
            path = Path(csv_path).expanduser()
        except TypeError:
            return None
        if not path.exists():
            return 0
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                return sum(1 for row in reader if row)
        except OSError:
            logger.exception("Failed to count route measurement CSV records.")
            return None

    def _route_measurement_next_point_number(self, position: int) -> int | None:
        try:
            ordinal = int(position)
        except (TypeError, ValueError):
            return None
        index = ordinal
        if index < 0 or index >= len(self._route_measurement_point_numbers):
            return None
        return int(self._route_measurement_point_numbers[index])

    def _set_route_measurement_resume_point(self, point_number: int) -> None:
        try:
            value = int(point_number)
        except (TypeError, ValueError):
            return
        if value < 1:
            return
        self._route_measurement_current_point = value
        self._select_route_point_for_measurement(value)
        if not self._route_runtime_presenter().set_current_point(value):
            self._save_route_measurement_current_point(value)

    def _select_route_point_for_measurement(self, point_number: int) -> None:
        plan = route_editing.select_route_point_for_measurement(
            self._design_session,
            point_number,
        )
        if not plan.selected:
            return
        self._last_selected_design_point = plan.last_selected_design_point
        self._refresh_design_panel()
        connection_flow.persist_controller_state_if_available(self)

    def _save_route_measurement_current_point(self, point_number: int) -> None:
        try:
            self._route_measurement_settings_store().save_current_point(
                int(point_number),
                session_active=bool(self._route_measurement_session_active),
            )
        except OSError:
            logger.exception("Failed to persist route measurement resume point.")

    def _set_route_measurement_pending(self, pending: bool) -> None:
        self._route_measurement_session_active = bool(pending)
        if not self._route_runtime_presenter().set_measurement_session_active(
            bool(pending)
        ):
            self._save_route_measurement_pending(bool(pending))

    def _save_route_measurement_pending(self, pending: bool) -> None:
        try:
            self._route_measurement_settings_store().save_pending(bool(pending))
        except OSError:
            logger.exception("Failed to persist route measurement pending state.")

    def _save_route_measurement_session_metadata(
        self,
        configuration: RouteMeasurementRunConfiguration | None,
    ) -> None:
        try:
            self._route_measurement_settings_store().save_session_metadata(
                route=self._design_session.route,
                configuration=configuration,
                session_active=bool(self._route_measurement_session_active),
            )
        except OSError:
            logger.exception("Failed to persist route measurement session metadata.")

    def _select_route_point(self, index: int) -> None:
        if not self._design_mutation_ready():
            return
        plan = route_editing.select_route_point(self._design_session, index)
        self._last_selected_design_point = plan.last_selected_design_point
        self._refresh_design_panel()

    def _set_route_needle_offsets(
        self,
        needle_1_dx: float,
        needle_1_dy: float,
        needle_2_dx: float,
        needle_2_dy: float,
    ) -> None:
        if not self._design_mutation_ready():
            return
        route = self._design_session.route
        if route is None:
            return
        route.set_needle_offsets(
            needle_1_dx,
            needle_1_dy,
            needle_2_dx,
            needle_2_dy,
        )
        self._refresh_design_panel()

    def _set_route_edit_enabled(self, enabled: bool) -> None:
        if self.design_layout_window is not None:
            self.design_layout_window.set_route_edit_enabled(enabled)
