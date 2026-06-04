import json
import tempfile
import unittest
from pathlib import Path

from probe_station_gui.api_keys import (
    API_PERMISSION_ROUTE_MEASURE,
    API_PERMISSION_STAGE_READ,
    API_PERMISSION_STAGE_WRITE,
    ApiKeyStore,
    default_api_user_name,
    masked_api_key,
)


class ApiKeyStoreTest(unittest.TestCase):
    def test_create_stores_hash_and_authorizes_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "api-keys.json"
            store = ApiKeyStore(path)

            api_key, record = store.create_key(
                user_name="operator",
                key_name="bench",
                permissions={
                    API_PERMISSION_STAGE_READ: True,
                    API_PERMISSION_STAGE_WRITE: False,
                    API_PERMISSION_ROUTE_MEASURE: True,
                },
            )

            self.assertTrue(api_key.startswith("psk_"))
            self.assertEqual(record.user_name, "operator")
            raw = json.loads(path.read_text(encoding="utf-8"))
            saved = raw["keys"][0]
            self.assertNotIn(api_key, path.read_text(encoding="utf-8"))
            self.assertEqual(saved["key_prefix"], api_key[:12])
            self.assertEqual(saved["key_suffix"], api_key[-6:])
            self.assertRegex(saved["key_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                masked_api_key(saved["key_prefix"], saved["key_suffix"]),
                f"{api_key[:12]}...{api_key[-6:]}",
            )

            read_result = store.authorize(api_key, API_PERMISSION_STAGE_READ)
            write_result = store.authorize(api_key, API_PERMISSION_STAGE_WRITE)

            self.assertTrue(read_result["accepted"])
            self.assertEqual(write_result["status_code"], 403)

    def test_disabled_and_unknown_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ApiKeyStore(Path(tmpdir) / "api-keys.json")
            api_key, record = store.create_key(user_name="operator")
            store.update_key(record.id, enabled=False)

            disabled = store.authorize(api_key, API_PERMISSION_STAGE_READ)
            unknown = store.authorize("psk_missing", API_PERMISSION_STAGE_READ)
            missing = store.authorize(None, API_PERMISSION_STAGE_READ)

            self.assertEqual(disabled["status_code"], 401)
            self.assertEqual(unknown["status_code"], 401)
            self.assertEqual(missing["status_code"], 401)

    def test_same_user_can_have_multiple_keys_with_separate_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ApiKeyStore(Path(tmpdir) / "api-keys.json")

            read_key, _read_record = store.create_key(
                user_name="operator",
                key_name="read client",
                permissions={
                    API_PERMISSION_STAGE_READ: True,
                    API_PERMISSION_STAGE_WRITE: False,
                },
            )
            write_key, _write_record = store.create_key(
                user_name="operator",
                key_name="write client",
                permissions={
                    API_PERMISSION_STAGE_READ: False,
                    API_PERMISSION_STAGE_WRITE: True,
                },
            )

            records = store.list_keys()

            self.assertEqual([record.user_name for record in records], ["operator", "operator"])
            self.assertEqual([record.key_name for record in records], ["read client", "write client"])
            self.assertTrue(store.authorize(read_key, API_PERMISSION_STAGE_READ)["accepted"])
            self.assertEqual(
                store.authorize(read_key, API_PERMISSION_STAGE_WRITE)["status_code"],
                403,
            )
            self.assertTrue(store.authorize(write_key, API_PERMISSION_STAGE_WRITE)["accepted"])
            self.assertEqual(
                store.authorize(write_key, API_PERMISSION_STAGE_READ)["status_code"],
                403,
            )

    def test_empty_user_name_uses_system_user_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ApiKeyStore(Path(tmpdir) / "api-keys.json")

            _api_key, record = store.create_key(user_name="")

            self.assertEqual(record.user_name, default_api_user_name())


if __name__ == "__main__":
    unittest.main()
