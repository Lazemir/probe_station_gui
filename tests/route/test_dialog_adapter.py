import types
import unittest
from pathlib import Path

from probe_station_gui.route.dialog_adapter import (
    route_dialog_defaults,
    route_dialog_restore_plan,
    route_measurement_session_cancel_plan,
    route_measurement_session_start_plan,
)


class RouteDialogAdapterTest(unittest.TestCase):
    def test_route_dialog_defaults_use_route_path_siblings(self) -> None:
        route = types.SimpleNamespace(
            path=Path("C:/data/routes/chip-17.route.json"),
        )

        defaults = route_dialog_defaults(route, document=None)

        self.assertEqual(
            defaults.csv_path,
            "C:\\data\\routes\\chip-17.route-measurements.csv",
        )
        self.assertEqual(
            defaults.photo_dir,
            "C:\\data\\routes\\chip-17.route-photos",
        )

    def test_route_dialog_defaults_use_document_directory_when_route_path_missing(
        self,
    ) -> None:
        document = types.SimpleNamespace(
            path=Path("C:/designs/probe/chip.gds"),
        )

        defaults = route_dialog_defaults(route=types.SimpleNamespace(path=None), document=document)

        self.assertEqual(
            defaults.csv_path,
            "C:\\designs\\probe\\probe_route_measurements.csv",
        )
        self.assertEqual(
            defaults.photo_dir,
            "C:\\designs\\probe\\probe_route_photos",
        )

    def test_route_dialog_defaults_fall_back_to_plain_names(self) -> None:
        defaults = route_dialog_defaults(
            route=types.SimpleNamespace(path=None),
            document=None,
        )

        self.assertEqual(defaults.csv_path, "probe_route_measurements.csv")
        self.assertEqual(defaults.photo_dir, "probe_route_photos")

    def test_restore_plan_invalidates_stale_route_settings(self) -> None:
        route = types.SimpleNamespace(
            name="route-a",
            path=Path("C:/routes/route-a.json"),
            points=[object(), object()],
        )
        state = {
            "measurement_session_active": True,
            "current_point": 5,
            "session_route_name": "route-b",
            "session_route_point_count": 2,
        }

        plan = route_dialog_restore_plan(state, route)

        self.assertFalse(plan.session_active)
        self.assertEqual(plan.current_point, 5)
        self.assertFalse(plan.open_dialog)

    def test_session_start_plan_rejects_active_thread(self) -> None:
        plan = route_measurement_session_start_plan(
            thread_active=True,
            dialog_configuration=None,
            current_point=None,
        )

        self.assertFalse(plan.accepted)
        self.assertEqual(plan.status_message, "Route measurement is already active.")
        self.assertEqual(plan.status_timeout_ms, 4000)

    def test_session_start_plan_uses_dialog_point_before_saved_point(self) -> None:
        configuration = types.SimpleNamespace(current_point=7)

        plan = route_measurement_session_start_plan(
            thread_active=False,
            dialog_configuration=configuration,
            current_point=3,
        )

        self.assertTrue(plan.accepted)
        self.assertEqual(plan.point_number, 7)
        self.assertTrue(plan.session_active)
        self.assertTrue(plan.pending)
        self.assertEqual(plan.status_message, "Route point set to point 7.")
        self.assertEqual(plan.status_timeout_ms, 5000)

    def test_session_cancel_plan_rejects_active_thread(self) -> None:
        plan = route_measurement_session_cancel_plan(thread_active=True)

        self.assertFalse(plan.accepted)
        self.assertEqual(
            plan.status_message,
            "Stop route measurement before canceling the session.",
        )
        self.assertEqual(plan.status_timeout_ms, 5000)

    def test_session_cancel_plan_resets_state_when_idle(self) -> None:
        plan = route_measurement_session_cancel_plan(thread_active=False)

        self.assertTrue(plan.accepted)
        self.assertFalse(plan.session_active)
        self.assertFalse(plan.pending)
        self.assertEqual(plan.point_number, 1)
        self.assertEqual(plan.status_message, "Route measurement session cancelled.")
        self.assertEqual(plan.status_timeout_ms, 5000)


if __name__ == "__main__":
    unittest.main()
