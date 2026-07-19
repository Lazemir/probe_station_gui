import sys
import unittest
from types import ModuleType

from probe_station_gui.api.keys import (
    API_PERMISSION_CAMERA_READ,
    API_PERMISSION_CAMERA_WRITE,
    API_PERMISSION_ROUTE_MEASURE,
    API_PERMISSION_STAGE_READ,
    API_PERMISSION_STAGE_WRITE,
)
from probe_station_gui.api.server import _axis_targets_from_payload
from probe_station_gui.api.server import _coordinate_mode_from_payload
from probe_station_gui.api.server import _feedrate_from_payload
from probe_station_gui.api.server import _voltage_sweep_from_payload
from probe_station_gui.api.server import ProbeStationApiServer
from probe_station_gui.camera.exposure_policy import (
    ExposurePolicyBusyError,
    ExposurePolicyError,
)


class ApiServerPayloadTest(unittest.TestCase):
    def test_axis_targets_accept_top_level_axes(self) -> None:
        targets = _axis_targets_from_payload({"x": 1.25, "Y": 2.5})

        self.assertEqual(targets, {"X": 1.25, "Y": 2.5})

    def test_axis_targets_accept_coordinates_map(self) -> None:
        targets = _axis_targets_from_payload(
            {"coordinates": {"x": "1.25", "Z": 3.0}}
        )

        self.assertEqual(targets, {"X": 1.25, "Z": 3.0})

    def test_top_level_axis_overrides_coordinates_map(self) -> None:
        targets = _axis_targets_from_payload(
            {"coordinates": {"X": 1.25, "Y": 2.5}, "x": 9.0}
        )

        self.assertEqual(targets, {"X": 9.0, "Y": 2.5})

    def test_move_payload_accepts_mode_and_feedrate(self) -> None:
        payload = {"mode": "relative", "feedrate_mm_min": "123.4"}

        self.assertEqual(_coordinate_mode_from_payload(payload), "G91")
        self.assertEqual(_feedrate_from_payload(payload), 123.4)

    def test_voltage_sweep_payload_accepts_finite_array(self) -> None:
        self.assertEqual(
            _voltage_sweep_from_payload({"voltages_v": ["-0.1", 0, 0.1]}),
            [-0.1, 0.0, 0.1],
        )

    def test_voltage_sweep_payload_rejects_missing_or_nonfinite_values(self) -> None:
        with self.assertRaises(ValueError):
            _voltage_sweep_from_payload({})
        with self.assertRaises(ValueError):
            _voltage_sweep_from_payload({"voltages_v": [float("nan")]})


class ApiServerHttpTest(unittest.TestCase):
    def _client(
        self,
        *,
        move_callback=None,
        status_callback=None,
        command_callback=None,
        auth_callback=None,
        camera_settings_read_callback=None,
        camera_settings_write_callback=None,
        camera_frame_callback=None,
        camera_exposure_policy_snapshot_callback=None,
        camera_exposure_policy_set_callback=None,
        camera_exposure_once_callback=None,
        raise_server_exceptions=True,
    ):
        from fastapi.testclient import TestClient

        server = ProbeStationApiServer(
            move_callback=move_callback or (lambda request: {"accepted": True, **request}),
            status_callback=status_callback or (lambda: {"accepted": True, "state": "Idle"}),
            command_callback=command_callback,
            auth_callback=auth_callback,
            camera_settings_read_callback=camera_settings_read_callback,
            camera_settings_write_callback=camera_settings_write_callback,
            camera_frame_callback=camera_frame_callback,
            camera_exposure_policy_snapshot_callback=(
                camera_exposure_policy_snapshot_callback
            ),
            camera_exposure_policy_set_callback=camera_exposure_policy_set_callback,
            camera_exposure_once_callback=camera_exposure_once_callback,
        )
        app, _uvicorn = server._create_app()
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    def test_health_and_status_endpoints_return_callback_payloads(self) -> None:
        client = self._client(
            status_callback=lambda: {
                "accepted": True,
                "state": "Idle",
                "position": {"X": 1.0},
            }
        )

        self.assertEqual(client.get("/health").json(), {"status": "ok"})
        docs_index = client.get("/")
        self.assertEqual(docs_index.status_code, 200)
        self.assertIn('href="/docs"', docs_index.text)
        self.assertIn('href="/redoc"', docs_index.text)

        response = client.get("/api/v1/stage/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"accepted": True, "state": "Idle", "position": {"X": 1.0}},
        )

    def test_move_endpoint_passes_json_body_targets_to_callback(self) -> None:
        calls = []

        client = self._client(
            move_callback=lambda request: (
                calls.append(request) or {"accepted": True, **request}
            )
        )

        response = client.post(
            "/api/v1/stage/move",
            json={
                "coordinates": {"x": "1.25"},
                "z": 3.0,
                "mode": "relative",
                "feedrate": 45.0,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            calls,
            [{"targets": {"X": 1.25, "Z": 3.0}, "mode": "G91", "feedrate": 45.0}],
        )
        self.assertEqual(response.json()["targets"], {"X": 1.25, "Z": 3.0})
        self.assertEqual(response.json()["mode"], "G91")
        self.assertEqual(response.json()["feedrate"], 45.0)

    def test_local_focus_endpoint_delegates_to_command_callback(self) -> None:
        calls = []

        client = self._client(
            command_callback=lambda request: (
                calls.append(request) or {"accepted": True, "focus": {"z": 1.2}}
            )
        )

        response = client.post(
            "/api/v1/stage/focus/local",
            json={"range_mm": 0.03, "step_mm": 0.002},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            calls,
            [
                {
                    "action": "stage_local_focus",
                    "payload": {"range_mm": 0.03, "step_mm": 0.002},
                }
            ],
        )
        self.assertEqual(response.json()["focus"], {"z": 1.2})

    def test_area_scan_endpoint_delegates_to_command_callback(self) -> None:
        calls = []

        client = self._client(
            command_callback=lambda request: (
                calls.append(request)
                or {"accepted": True, "status_code": 202, "output_dir": "C:/scan"}
            )
        )

        response = client.post(
            "/api/v1/camera/area-scan",
            json={"rows": 3, "columns": 3, "overlap_fraction": 0.0},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            calls,
            [
                {
                    "action": "microscope_area_scan",
                    "payload": {
                        "rows": 3,
                        "columns": 3,
                        "overlap_fraction": 0.0,
                    },
                }
            ],
        )
        self.assertEqual(response.json()["output_dir"], "C:/scan")

    def test_area_scan_rejects_removed_auto_exposure_payload(self) -> None:
        calls = []
        client = self._client(
            command_callback=lambda request: calls.append(request)
            or {"accepted": True, "status_code": 202}
        )

        response = client.post(
            "/api/v1/camera/area-scan",
            json={"rows": 3, "columns": 3, "auto_exposure": False},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(calls, [])
        self.assertIn("auto_exposure", response.json()["detail"]["message"])

    def test_area_scan_default_camera_lock_requires_only_stage_write(self) -> None:
        auth_calls = []

        def authorize(_api_key, permission):
            auth_calls.append(permission)
            return {
                "accepted": permission == API_PERMISSION_STAGE_WRITE,
                "status_code": 403,
                "message": "camera write denied",
            }

        client = self._client(
            auth_callback=authorize,
            command_callback=lambda _request: {
                "accepted": True,
                "status_code": 202,
            },
        )

        response = client.post(
            "/api/v1/camera/area-scan",
            json={"rows": 2, "columns": 2},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(auth_calls, [API_PERMISSION_STAGE_WRITE])

    def test_area_scan_custom_camera_lock_requires_camera_write(self) -> None:
        auth_calls = []
        dispatch_calls = []

        def authorize(_api_key, permission):
            auth_calls.append(permission)
            return {
                "accepted": permission == API_PERMISSION_STAGE_WRITE,
                "status_code": 403,
                "message": "camera write denied",
            }

        client = self._client(
            auth_callback=authorize,
            command_callback=lambda request: dispatch_calls.append(request)
            or {"accepted": True, "status_code": 202},
        )

        response = client.post(
            "/api/v1/camera/area-scan",
            json={
                "camera_lock": {
                    "enabled": True,
                    "settings": [
                        {"node_name": "GainAuto", "value": "Off"},
                    ],
                }
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            auth_calls,
            [API_PERMISSION_STAGE_WRITE, API_PERMISSION_CAMERA_WRITE],
        )
        self.assertEqual(dispatch_calls, [])

    def test_area_scan_custom_allowed_lock_accepts_both_write_permissions(
        self,
    ) -> None:
        auth_calls = []
        dispatch_calls = []
        client = self._client(
            auth_callback=lambda _key, permission: auth_calls.append(permission)
            or {"accepted": True},
            command_callback=lambda request: dispatch_calls.append(request)
            or {"accepted": True, "status_code": 202},
        )
        payload = {
            "camera_lock": {
                "enabled": True,
                "settings": [
                    {"node_name": "Gain", "value": 4.0},
                ],
            }
        }

        response = client.post("/api/v1/camera/area-scan", json=payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            auth_calls,
            [API_PERMISSION_STAGE_WRITE, API_PERMISSION_CAMERA_WRITE],
        )
        self.assertEqual(
            dispatch_calls,
            [{"action": "microscope_area_scan", "payload": payload}],
        )

    def test_area_scan_preserves_rejected_command_status(self) -> None:
        client = self._client(
            command_callback=lambda _request: {
                "accepted": False,
                "status_code": 409,
                "message": "stage became busy",
            }
        )

        response = client.post("/api/v1/camera/area-scan", json={})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["message"], "stage became busy")

    def test_lens_reset_preserves_rejected_command_status(self) -> None:
        client = self._client(
            command_callback=lambda _request: {
                "accepted": False,
                "status_code": 409,
                "message": "lens correction is busy",
            }
        )

        response = client.post(
            "/api/v1/calibration/lens-distortion",
            json={"reset": True},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["message"],
            "lens correction is busy",
        )

    def test_click_force_reset_preserves_rejected_command_status(self) -> None:
        client = self._client(
            command_callback=lambda _request: {
                "accepted": False,
                "status_code": 409,
                "message": "objective calibration is busy",
            }
        )

        response = client.post(
            "/api/v1/calibration/click-to-move",
            json={"force": True},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["message"],
            "objective calibration is busy",
        )

    def test_camera_settings_endpoints_preserve_names_and_ordered_writes(self) -> None:
        reads = []
        writes = []
        client = self._client(
            camera_settings_read_callback=lambda names: (
                reads.append(names)
                or {"accepted": True, "camera_ready": True, "nodes": []}
            ),
            camera_settings_write_callback=lambda settings: (
                writes.append(settings)
                or {
                    "accepted": True,
                    "nodes": settings,
                    "frame_counter_at_completion": 9,
                }
            ),
        )

        read_response = client.get(
            "/api/v1/camera/settings",
            params=[("name", "ExposureTime"), ("name", "Gain")],
        )
        write_response = client.patch(
            "/api/v1/camera/settings",
            json={
                "settings": [
                    {"name": "Gain", "value": 0.0},
                    {"name": "ExposureTime", "value": 1800.0},
                ]
            },
        )

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(reads, [["ExposureTime", "Gain"]])
        self.assertEqual(write_response.status_code, 200)
        self.assertEqual(
            writes,
            [
                [
                    {"name": "Gain", "value": 0.0},
                    {"name": "ExposureTime", "value": 1800.0},
                ]
            ],
        )
        self.assertEqual(write_response.json()["frame_counter_at_completion"], 9)

    def test_camera_frame_endpoint_returns_png_metadata_headers(self) -> None:
        calls = []
        client = self._client(
            camera_frame_callback=lambda space, after_counter, timeout_s: (
                calls.append((space, after_counter, timeout_s))
                or {
                    "accepted": True,
                    "data": b"png-data",
                    "content_type": "image/png",
                    "counter": 21,
                    "space": space,
                    "width": 640,
                    "height": 480,
                }
            )
        )

        response = client.get(
            "/api/v1/camera/frame",
            params={"space": "raw", "after_counter": 20, "timeout_ms": 1500},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"png-data")
        self.assertEqual(calls, [("raw", 20, 1.5)])
        self.assertEqual(response.headers["X-Camera-Frame-Counter"], "21")
        self.assertEqual(response.headers["X-Camera-Frame-Space"], "raw")
        self.assertEqual(response.headers["X-Camera-Frame-Width"], "640")
        self.assertEqual(response.headers["X-Camera-Frame-Height"], "480")

    def test_exposure_policy_endpoints_return_and_update_controller_state(self) -> None:
        policy_updates = []
        client = self._client(
            camera_exposure_policy_snapshot_callback=lambda: {
                "auto_enabled": True,
                "engine": "software",
                "busy": False,
            },
            camera_exposure_policy_set_callback=lambda *, auto_enabled, engine: (
                policy_updates.append((auto_enabled, engine))
                or {
                    "auto_enabled": auto_enabled,
                    "engine": engine,
                    "busy": False,
                }
            ),
        )

        read_response = client.get("/api/v1/camera/exposure-policy")
        write_response = client.put(
            "/api/v1/camera/exposure-policy",
            json={"auto_enabled": False, "engine": "camera"},
        )

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(read_response.json()["engine"], "software")
        self.assertEqual(write_response.status_code, 200)
        self.assertEqual(write_response.json()["engine"], "camera")
        self.assertEqual(policy_updates, [(False, "camera")])

    def test_exposure_policy_rejects_invalid_engine_before_controller_call(self) -> None:
        calls = []
        client = self._client(
            camera_exposure_policy_set_callback=lambda **policy: calls.append(policy)
        )

        response = client.put(
            "/api/v1/camera/exposure-policy",
            json={"auto_enabled": True, "engine": "invalid"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(calls, [])

    def test_exposure_policy_commands_report_controller_busy_as_conflict(self) -> None:
        def busy(*_args, **_kwargs):
            raise ExposurePolicyBusyError("Camera exposure policy is busy.")

        client = self._client(
            camera_exposure_policy_set_callback=busy,
            camera_exposure_once_callback=busy,
        )

        update = client.put(
            "/api/v1/camera/exposure-policy",
            json={"auto_enabled": False, "engine": "software"},
        )
        once = client.post("/api/v1/camera/exposure-once", json={})

        self.assertEqual(update.status_code, 409)
        self.assertEqual(once.status_code, 409)

    def test_exposure_policy_errors_return_structured_service_unavailable(self) -> None:
        def unavailable(*_args, **_kwargs):
            raise ExposurePolicyError("Camera settings write failed.")

        client = self._client(
            camera_exposure_policy_set_callback=unavailable,
            camera_exposure_once_callback=unavailable,
            raise_server_exceptions=False,
        )

        update = client.put(
            "/api/v1/camera/exposure-policy",
            json={"auto_enabled": False, "engine": "software"},
        )
        once = client.post("/api/v1/camera/exposure-once", json={})

        self.assertEqual(update.status_code, 503)
        self.assertEqual(once.status_code, 503)
        self.assertEqual(update.json()["detail"]["message"], "Camera settings write failed.")

    def test_exposure_policy_classifies_exception_from_reloaded_module(self) -> None:
        module_name = "probe_station_gui.camera.exposure_policy"
        stale_module = ModuleType(module_name)
        stale_module.ExposurePolicyError = type(
            "ExposurePolicyError",
            (RuntimeError,),
            {},
        )
        stale_module.ExposurePolicyBusyError = type(
            "ExposurePolicyBusyError",
            (stale_module.ExposurePolicyError,),
            {},
        )
        current_module = ModuleType(module_name)
        current_module.ExposurePolicyError = type(
            "ExposurePolicyError",
            (RuntimeError,),
            {},
        )
        current_module.ExposurePolicyBusyError = type(
            "ExposurePolicyBusyError",
            (current_module.ExposurePolicyError,),
            {},
        )

        def busy_after_reload(*_args, **_kwargs):
            sys.modules[module_name] = current_module
            raise current_module.ExposurePolicyBusyError(
                "Camera exposure policy is busy."
            )

        client = self._client(
            camera_exposure_once_callback=busy_after_reload,
            raise_server_exceptions=False,
        )
        original_module = sys.modules[module_name]
        sys.modules[module_name] = stale_module
        try:
            response = client.post("/api/v1/camera/exposure-once", json={})
        finally:
            sys.modules[module_name] = original_module

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["message"],
            "Camera exposure policy is busy.",
        )

    def test_exposure_policy_endpoints_preserve_camera_permissions(self) -> None:
        client = self._client(
            auth_callback=lambda _key, permission: {
                "accepted": permission != API_PERMISSION_CAMERA_WRITE,
                "status_code": 403,
                "message": "camera write denied",
            },
            camera_exposure_policy_snapshot_callback=lambda: {"busy": False},
            camera_exposure_policy_set_callback=lambda **_policy: {"busy": False},
            camera_exposure_once_callback=lambda: {"busy": False},
        )

        read = client.get("/api/v1/camera/exposure-policy")
        update = client.put(
            "/api/v1/camera/exposure-policy",
            json={"auto_enabled": False, "engine": "software"},
        )
        once = client.post("/api/v1/camera/exposure-once", json={})

        self.assertEqual(read.status_code, 200)
        self.assertEqual(update.status_code, 403)
        self.assertEqual(once.status_code, 403)

    def test_old_auto_exposure_route_is_removed(self) -> None:
        response = self._client().post("/api/v1/camera/auto-exposure", json={})

        self.assertEqual(response.status_code, 404)

    def test_camera_endpoints_require_separate_camera_permissions(self) -> None:
        auth_calls = []

        def auth_callback(api_key, permission):
            auth_calls.append((api_key, permission))
            if permission == API_PERMISSION_CAMERA_WRITE:
                return {
                    "accepted": False,
                    "status_code": 403,
                    "message": "camera write denied",
                }
            return {"accepted": True}

        client = self._client(
            auth_callback=auth_callback,
            camera_settings_read_callback=lambda names: {
                "accepted": True,
                "nodes": [],
            },
            camera_settings_write_callback=lambda settings: {
                "accepted": True,
                "nodes": [],
            },
            camera_frame_callback=lambda space, after_counter, timeout_s: {
                "accepted": True,
                "data": b"png",
                "counter": 1,
                "space": space,
                "width": 1,
                "height": 1,
            },
        )

        read = client.get("/api/v1/camera/settings")
        frame = client.get("/api/v1/camera/frame")
        write = client.patch(
            "/api/v1/camera/settings",
            json={"settings": [{"name": "Gain", "value": 0.0}]},
        )

        self.assertEqual(read.status_code, 200)
        self.assertEqual(frame.status_code, 200)
        self.assertEqual(write.status_code, 403)
        self.assertIn((None, API_PERMISSION_CAMERA_READ), auth_calls)
        self.assertIn((None, API_PERMISSION_CAMERA_WRITE), auth_calls)

    def test_click_to_move_calibration_endpoint_delegates_to_command_callback(self) -> None:
        calls = []

        client = self._client(
            command_callback=lambda request: (
                calls.append(request)
                or {"accepted": True, "status_code": 202, "message": "started"}
            )
        )

        response = client.post(
            "/api/v1/calibration/click-to-move",
            json={"dx_px": 0, "dy_px": 0},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            calls,
            [
                {
                    "action": "click_to_move_calibration",
                    "payload": {"dx_px": 0, "dy_px": 0},
                }
            ],
        )
        self.assertEqual(response.json()["message"], "started")

    def test_move_endpoint_rejects_empty_or_rejected_moves(self) -> None:
        client = self._client(
            move_callback=lambda _request: {
                "accepted": False,
                "status_code": 409,
                "message": "busy",
            }
        )

        empty = client.post("/api/v1/stage/move", json={})
        rejected = client.post("/api/v1/stage/move", json={"x": 1.0})

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(
            empty.json()["detail"],
            {"message": "Provide at least one target coordinate."},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()["detail"]["message"], "busy")

    def test_contact_raw_sweep_and_route_session_endpoints_delegate(self) -> None:
        calls = []

        def command_callback(request):
            calls.append(request)
            if request["action"] == "route_contact_photo":
                return {
                    "accepted": True,
                    "filename": "contact_007_photo.jpg",
                    "content_type": "image/jpeg",
                    "data": b"contact-photo",
                }
            if request["action"] == "route_session_artifact":
                return {
                    "accepted": True,
                    "artifact_id": request["payload"]["artifact_id"],
                    "filename": "contact.jpg",
                    "content_type": "image/jpeg",
                    "data": b"jpeg-bytes",
                }
            if request["action"] == "api_route_control_status":
                return {"accepted": True, "active": False, "paused": False}
            return {"accepted": True, "echo": request}

        client = self._client(command_callback=command_callback)

        contacts = client.get("/api/v1/route/contacts")
        move = client.post(
            "/api/v1/route/contacts/7/move",
            json={"lower_needles": True},
        )
        needles = client.post(
            "/api/v1/route/contacts/7/needles",
            json={"action": "lift"},
        )
        check = client.post(
            "/api/v1/route/contacts/7/check",
            json={
                "check_sample_count": 10,
                "contact_quality": {"max_mad_sigma_ohm": 900},
            },
        )
        photo = client.get("/api/v1/route/contacts/7/photo")
        seek = client.post(
            "/api/v1/route/contacts/7/seek",
            json={
                "contact_seek_range_mm": 0.003,
                "contact_quality": {"max_relative_mad_sigma": 0.08},
            },
        )
        api_route_control_status = client.get("/api/v1/route/control")
        api_route_control_pause = client.post(
            "/api/v1/route/control",
            json={"action": "pause"},
        )
        configure = client.post(
            "/api/v1/meter/configure",
            json={"meter_type": "keithley"},
        )
        sweep = client.post(
            "/api/v1/measurements/raw-sweep",
            json={"contact_number": 7, "voltages_v": [-0.1, 0.1]},
        )
        session = client.post(
            "/api/v1/route/sessions",
            json={
                "initial_measurement_count": 10,
                "contact_quality": {"max_p95_abs_step_ohm": 2500},
            },
        )
        status = client.get("/api/v1/route/sessions/current")
        action = client.post(
            "/api/v1/route/sessions/current/actions",
            json={"action": "pause"},
        )
        result = client.post(
            "/api/v1/route/sessions/current/result",
            json={"status": "ok", "summary": {"points": 31}},
        )
        session_seek = client.post("/api/v1/route/sessions/current/seek", json={})
        artifact = client.get("/api/v1/route/sessions/current/artifacts/a1")

        self.assertEqual(contacts.status_code, 200)
        self.assertEqual(move.status_code, 200)
        self.assertEqual(needles.status_code, 200)
        self.assertEqual(check.status_code, 200)
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.content, b"contact-photo")
        self.assertEqual(photo.headers["x-contact-number"], "7")
        self.assertEqual(seek.status_code, 200)
        self.assertEqual(api_route_control_status.status_code, 200)
        self.assertEqual(api_route_control_pause.status_code, 200)
        self.assertEqual(configure.status_code, 200)
        self.assertEqual(sweep.status_code, 200)
        self.assertEqual(session.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(action.status_code, 200)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(session_seek.status_code, 200)
        self.assertEqual(artifact.status_code, 200)
        self.assertEqual(artifact.content, b"jpeg-bytes")
        self.assertEqual(artifact.headers["x-artifact-id"], "a1")
        self.assertEqual(
            [call["action"] for call in calls],
            [
                "list_contacts",
                "move_to_contact",
                "contact_needles",
                "check_contact",
                "route_contact_photo",
                "contact_seek",
                "api_route_control_status",
                "api_route_control_action",
                "configure_meter",
                "raw_voltage_sweep",
                "start_route_session",
                "route_session_status",
                "route_session_action",
                "route_session_result",
                "route_session_seek",
                "route_session_artifact",
            ],
        )
        self.assertEqual(calls[1]["payload"]["contact_number"], 7)
        self.assertEqual(calls[3]["payload"]["check_sample_count"], 10)
        self.assertEqual(
            calls[3]["payload"]["contact_quality"],
            {"max_mad_sigma_ohm": 900},
        )
        self.assertEqual(calls[4]["payload"]["contact_number"], 7)
        self.assertEqual(calls[5]["payload"]["contact_seek_range_mm"], 0.003)
        self.assertEqual(
            calls[5]["payload"]["contact_quality"],
            {"max_relative_mad_sigma": 0.08},
        )
        self.assertEqual(calls[7]["payload"]["action"], "pause")
        self.assertEqual(calls[9]["payload"]["voltages_v"], [-0.1, 0.1])
        self.assertEqual(calls[10]["payload"]["initial_measurement_count"], 10)
        self.assertEqual(
            calls[10]["payload"]["contact_quality"],
            {"max_p95_abs_step_ohm": 2500},
        )
        self.assertEqual(calls[12]["payload"]["action"], "pause")
        self.assertEqual(calls[13]["payload"]["summary"], {"points": 31})

    def test_visa_endpoints_delegate_roles_and_operations(self) -> None:
        calls = []

        def command_callback(request):
            calls.append(request)
            if request["action"] == "visa_list_resources":
                return {
                    "accepted": True,
                    "resources": [{"role": "meter.source"}],
                }
            payload = request["payload"]
            if payload["operation"] == "query":
                return {
                    "accepted": True,
                    "role": payload["role"],
                    "operation": "query",
                    "response": "Keithley,2400",
                }
            if payload["operation"] == "read_raw":
                return {
                    "accepted": True,
                    "role": payload["role"],
                    "operation": "read_raw",
                    "data": b"raw-bytes",
                }
            return {
                "accepted": True,
                "role": payload["role"],
                "operation": payload["operation"],
            }

        client = self._client(command_callback=command_callback)

        resources = client.get("/api/v1/visa/resources")
        write = client.post(
            "/api/v1/visa/resources/meter.source/write",
            json={"command": "*CLS", "timeout_ms": 1000},
        )
        query = client.post(
            "/api/v1/visa/resources/meter.source/query",
            json={"command": "*IDN?"},
        )
        raw = client.post(
            "/api/v1/visa/resources/meter.source/read-raw",
            json={},
        )
        clear = client.post(
            "/api/v1/visa/resources/meter.source/clear",
            json={},
        )

        self.assertEqual(resources.status_code, 200)
        self.assertEqual(write.status_code, 200)
        self.assertEqual(query.status_code, 200)
        self.assertEqual(query.json()["response"], "Keithley,2400")
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.content, b"raw-bytes")
        self.assertEqual(clear.status_code, 200)
        self.assertEqual(
            [call["action"] for call in calls],
            [
                "visa_list_resources",
                "visa_operation",
                "visa_operation",
                "visa_operation",
                "visa_operation",
            ],
        )
        self.assertEqual(calls[1]["payload"]["role"], "meter.source")
        self.assertEqual(calls[1]["payload"]["operation"], "write")
        self.assertEqual(calls[1]["payload"]["command"], "*CLS")
        self.assertEqual(calls[2]["payload"]["operation"], "query")
        self.assertEqual(calls[3]["payload"]["operation"], "read_raw")

    def test_authenticated_endpoints_require_matching_permissions(self) -> None:
        auth_calls = []

        def auth_callback(api_key, permission):
            auth_calls.append((api_key, permission))
            if api_key != "secret":
                return {
                    "accepted": False,
                    "status_code": 401,
                    "message": "bad key",
                }
            if permission == API_PERMISSION_STAGE_WRITE:
                return {
                    "accepted": False,
                    "status_code": 403,
                    "message": "missing permission",
                }
            return {"accepted": True}

        client = self._client(
            auth_callback=auth_callback,
            command_callback=lambda request: {"accepted": True, "echo": request},
        )

        missing = client.get("/api/v1/stage/status")
        status = client.get(
            "/api/v1/stage/status",
            headers={"Authorization": "Bearer secret"},
        )
        move = client.post(
            "/api/v1/stage/move",
            headers={"Authorization": "Bearer secret"},
            json={"x": 1.0},
        )
        seek = client.post(
            "/api/v1/route/contacts/3/seek",
            headers={"X-API-Key": "secret"},
            json={"check_sample_count": 10},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(move.status_code, 403)
        self.assertEqual(seek.status_code, 200)
        self.assertIn(("secret", API_PERMISSION_STAGE_READ), auth_calls)
        self.assertIn(("secret", API_PERMISSION_STAGE_WRITE), auth_calls)
        self.assertIn(("secret", API_PERMISSION_ROUTE_MEASURE), auth_calls)


if __name__ == "__main__":
    unittest.main()

