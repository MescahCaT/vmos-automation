import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import create_devices
from create_devices import DeviceCreator
from device_config import OUTPUT_FILES
from status_checker import StatusChecker


def response(data=None, status=200, text=None):
    result = requests.Response()
    result.status_code = status
    result.url = "http://vmos.test/api"
    result._content = (text if text is not None else json.dumps(data)).encode("utf-8")
    return result


def instance_list(items, nested=False):
    return response({"code": "200", "data": {"list": items} if nested else items})


class CreateResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config_file = Path(self.temp.name) / "devices_input.json"
        config_file.write_text(json.dumps({"host_ip": "vmos.test", "count": 1}), encoding="utf-8")
        self.creator = DeviceCreator(str(config_file))
        self.spec = {"user_name": "test-001", "remark": "test"}
        for target in ("builtins.print", "create_devices.time.sleep"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_compatible_create_responses(self):
        variants = [
            {"data": {"db_ids": ["EDGE001"]}},
            {"data": {"db_id": "EDGE001"}},
            {"data": {"db_ids": "EDGE001"}},
            {"data": ["EDGE001"]},
            {"data": [{"db_id": "EDGE001"}]},
            {"data": {"list": [{"db_id": "EDGE001"}]}},
            {"data": "EDGE001"},
            {"db_id": "EDGE001"},
            {"db_ids": ["EDGE001"]},
        ]
        for payload in variants:
            with self.subTest(payload=payload), patch(
                "create_devices.requests.get", return_value=instance_list([])
            ) as get, patch(
                "create_devices.requests.post", return_value=response({"code": "200", **payload})
            ) as post:
                device = self.creator.create_single(1, self.spec)
                self.assertEqual(device["db_id"], "EDGE001")
                self.assertEqual(device["creation_source"], "接口返回")
                self.assertEqual(get.call_count, 1)
                self.assertEqual(post.call_count, 1)

    def test_timeout_recovers_only_after_creation_finishes(self):
        item = {"user_name": "test-001", "db_id": "EDGE001", "state": "stopped"}
        with patch("create_devices.requests.get", side_effect=[
            instance_list([]), instance_list([{**item, "state": "creating"}]),
            instance_list([item], nested=True)
        ]), patch("create_devices.requests.post", side_effect=requests.ReadTimeout("read timeout")) as post:
            device = self.creator.create_single(1, self.spec)
        self.assertEqual(device["db_id"], "EDGE001")
        self.assertEqual(device["creation_source"], "实例列表回查")
        self.assertEqual(post.call_count, 1)
        self.assertIn("request_error", self.creator.creation_results[-1])

    def test_connection_timeout_stops_batch_without_marking_pending(self):
        self.creator.config_loader.config["count"] = 3
        with patch("create_devices.requests.get", return_value=instance_list([])) as get, patch(
            "create_devices.requests.post", side_effect=requests.ConnectTimeout("connect timeout")
        ) as post:
            self.assertEqual(self.creator.create_batch(), [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(self.creator.creation_results), 1)
        self.assertEqual(self.creator.creation_results[0]["status"], "connection_failed")
        self.assertTrue(any("0 待确认，1 连接失败，2 未执行" in line for line in self.creator.logs))

    def test_error_or_missing_id_is_reconciled_with_new_instance(self):
        variants = [
            response({"code": 200, "data": None}),
            response({"code": 500, "data": {"db_ids": ["EDGE_WRONG"]}}),
            response({"code": 200, "data": {"db_ids": ["EDGE_WRONG"]}}, status=500),
            response(text="upstream disconnected"),
            response({"code": 200, "data": {"db_ids": ["EDGE001"]}}, status=202),
        ]
        item = {"user_name": "test-001", "db_id": "EDGE001", "status": "stopped"}
        for create_response in variants:
            with self.subTest(status=create_response.status_code, body=create_response.text), patch(
                "create_devices.requests.get", side_effect=[instance_list([]), instance_list([item])]
            ), patch("create_devices.requests.post", return_value=create_response) as post:
                device = self.creator.create_single(1, self.spec)
                self.assertEqual(device["db_id"], "EDGE001")
                self.assertEqual(post.call_count, 1)

    def test_existing_same_name_is_not_counted_as_new_creation(self):
        old = {"user_name": "test-001", "db_id": "EDGE_OLD", "state": "stopped"}
        with patch("create_devices.requests.get", return_value=instance_list([old])), patch(
            "create_devices.requests.post", return_value=response({"code": 1, "msg": "name exists"})
        ) as post:
            self.assertIsNone(self.creator.create_single(1, self.spec))
        self.assertEqual(self.creator.created_devices, [])
        self.assertEqual(self.creator.creation_results[-1]["status"], "unconfirmed")
        self.assertEqual(post.call_count, 1)

    def test_unfinished_or_failed_instance_is_not_success(self):
        for state in ("creating", "failed", "error", "deleting"):
            item = {"user_name": "test-001", "db_id": "EDGE001", "state": state}
            with self.subTest(state=state), patch(
                "create_devices.requests.get", side_effect=[instance_list([])] + [instance_list([item])] * 20
            ), patch("create_devices.requests.post", return_value=response({"code": 200, "data": None})):
                self.assertIsNone(self.creator.create_single(1, self.spec))
        self.assertEqual(self.creator.created_devices, [])

    def test_multiple_new_matches_remain_unconfirmed(self):
        items = [{"user_name": "test-001", "db_id": db_id} for db_id in ("EDGE001", "EDGE002")]
        with patch("create_devices.requests.get", side_effect=[instance_list([]), instance_list(items)]), patch(
            "create_devices.requests.post", return_value=response({"code": 200, "data": None})
        ):
            self.assertIsNone(self.creator.create_single(1, self.spec))
        self.assertIn("多个", self.creator.creation_results[-1]["query_error"])

    def test_missing_baseline_does_not_claim_existing_device(self):
        with patch("create_devices.requests.get", side_effect=requests.Timeout("query timeout")) as get, patch(
            "create_devices.requests.post", side_effect=[
                requests.Timeout("fallback query timeout"), response({"code": 200, "data": None})
            ]
        ):
            self.assertIsNone(self.creator.create_single(1, self.spec))
        self.assertEqual(get.call_count, 1)
        self.assertIn("before_query_error", self.creator.creation_results[-1])

    def test_list_names_failure_falls_back_to_get_db(self):
        item = {"user_name": "test-001", "db_id": "EDGE001", "state": "stopped"}
        calls = []

        def post(url, **kwargs):
            calls.append(url)
            if url.endswith("/create"):
                return response({"code": 200, "data": None})
            if url.endswith("/get_db"):
                return instance_list([item] if any(call.endswith("/create") for call in calls) else [], nested=True)
            raise AssertionError(f"unexpected request: {url}")

        with patch("create_devices.requests.get", return_value=response(status=404)), patch(
            "create_devices.requests.post", side_effect=post
        ):
            device = self.creator.create_single(1, self.spec)
        self.assertEqual(device["db_id"], "EDGE001")
        self.assertEqual(sum(url.endswith("/create") for url in calls), 1)
        self.assertEqual(sum(url.endswith("/get_db") for url in calls), 2)

    def test_multiple_response_ids_require_query_confirmation(self):
        item = {"user_name": "test-001", "db_id": "EDGE001", "state": "stopped"}
        with patch("create_devices.requests.get", side_effect=[instance_list([]), instance_list([item])]), patch(
            "create_devices.requests.post", return_value=response({"code": 200, "data": ["EDGE001", "EDGE002"]})
        ):
            device = self.creator.create_single(1, self.spec)
        self.assertEqual(device["db_id"], "EDGE001")
        self.assertEqual(device["creation_source"], "实例列表回查")

    def test_unconfirmed_response_does_not_erase_remote_error(self):
        with patch("create_devices.requests.get", return_value=instance_list([])), patch(
            "create_devices.requests.post", return_value=response({"code": 500, "data": {"db_id": "EDGE_WRONG"}})
        ):
            self.assertIsNone(self.creator.create_single(1, self.spec))
        self.assertEqual(self.creator.created_devices, [])
        self.assertEqual(self.creator.creation_results[-1]["response"]["data"]["db_id"], "EDGE_WRONG")

    def test_port_record_failure_does_not_erase_success(self):
        with patch("create_devices.requests.get", return_value=instance_list([])), patch(
            "create_devices.requests.post", return_value=response({"code": 200, "data": {"db_id": "EDGE001"}})
        ), patch.object(self.creator.port_allocator, "allocate", side_effect=KeyError("video")):
            device = self.creator.create_single(1, self.spec)
        self.assertEqual(device["db_id"], "EDGE001")
        self.assertEqual(device["ports"], {})
        self.assertEqual(self.creator.creation_results[-1]["status"], "success")
        self.assertIn("port_error", self.creator.creation_results[-1])

    def test_disabling_auto_ports_does_not_erase_success(self):
        self.creator.port_allocator.current = {}
        with patch("create_devices.requests.get", return_value=instance_list([])), patch(
            "create_devices.requests.post", return_value=response({"code": 200, "data": {"db_id": "EDGE001"}})
        ):
            device = self.creator.create_single(1, self.spec)
        self.assertEqual(device["ports"], {})
        self.assertNotIn("port_error", self.creator.creation_results[-1])

    def test_main_saves_diagnostics_even_with_no_confirmed_devices(self):
        output_paths = {key: str(Path(self.temp.name) / name) for key, name in OUTPUT_FILES.items()}
        with patch.object(create_devices, "DeviceCreator", return_value=self.creator), patch(
            "create_devices.OUTPUT_FILES", output_paths
        ), patch("create_devices.requests.get", return_value=instance_list([])), patch(
            "create_devices.requests.post", return_value=response({"code": 500, "msg": "rejected"})
        ) as post, patch.object(sys, "argv", ["create_devices.py"]):
            create_devices.main()
        results = json.loads(Path(output_paths["results"]).read_text(encoding="utf-8"))
        self.assertEqual(results[0]["response"]["code"], 500)
        self.assertEqual(results[0]["status"], "unconfirmed")
        self.assertTrue(Path(output_paths["log"]).is_file())
        self.assertEqual(post.call_count, 1)

    def test_running_and_rom_checks_accept_wrapped_data_and_string_codes(self):
        checker = StatusChecker("http://vmos.test")
        with patch("status_checker.requests.post", return_value=instance_list(
            [{"db_id": "EDGE001", "state": None, "status": "running"}], nested=True
        )), patch("status_checker.requests.get", return_value=response({"code": "200"})):
            self.assertEqual(checker.wait_for_running(["EDGE001"]), {"EDGE001": True})
            self.assertEqual(checker.check_instance_state("EDGE001"), "running")
            self.assertEqual(checker.wait_for_rom_ready(["EDGE001"]), {"EDGE001": True})

    def test_start_accepts_2xx_and_string_business_code(self):
        with patch("create_devices.requests.post", return_value=response({"code": "200"}, status=202)), patch.object(
            self.creator.status_checker, "wait_for_running", return_value={"EDGE001": True}
        ):
            self.assertEqual(self.creator.start_batch(["EDGE001"]), {"EDGE001": True})

    def test_create_only_saves_result_without_starting_device(self):
        output_paths = {key: str(Path(self.temp.name) / name) for key, name in OUTPUT_FILES.items()}
        with patch.object(create_devices, "DeviceCreator", return_value=self.creator), patch(
            "create_devices.OUTPUT_FILES", output_paths
        ), patch("create_devices.requests.get", return_value=instance_list([])), patch(
            "create_devices.requests.post", return_value=response({"code": 200, "data": {"db_id": "EDGE001"}})
        ) as post, patch.object(self.creator, "start_batch") as start, patch.object(
            sys, "argv", ["create_devices.py", "--create-only"]
        ):
            create_devices.main()
        start.assert_not_called()
        self.assertEqual(post.call_count, 1)
        devices = json.loads(Path(output_paths["devices"]).read_text(encoding="utf-8"))
        self.assertEqual(devices[0]["db_id"], "EDGE001")


if __name__ == "__main__":
    unittest.main()
