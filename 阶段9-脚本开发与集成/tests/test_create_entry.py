"""简洁创建入口的离线检查，不连接 VMOS。"""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("create_entry", ROOT / "创建云机.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class CreateEntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vmos create ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "阶段9-脚本开发与集成" / "batch10_create.example.json"
        self.config_path.parent.mkdir()
        self.config = {"host_ip": "192.0.2.10", "count": 10,
                       "default_config": {"image_repository": "test-image", "adiID": 1039},
                       "devices": [{"user_name": "151.1", "remark": "151.1"},
                                   {"user_name": "151.2", "remark": "151.2"}]}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8-sig")
        self.creator = Mock()
        self.creator._list_instances.return_value = []
        self.creator.creation_results = []
        self.creator.created_devices = []
        self.main_config = {"vmos": {"host_ip": "192.0.2.10", "port": 18182},
                            "proxy": {"config_file": "配置文件/proxy_list.json"},
                            "tiktok": {"package_name": "com.zhiliaoapp.musically", "apk_path": "test.apk"}}
        self.apk = {"path": str(self.root / "test.apk"), "package_name": "com.zhiliaoapp.musically",
                    "version_name": "43.7.2", "version_code": "1", "size_bytes": 3}
        self.apk_check = Mock(return_value=self.apk)
        config_dir = self.root / "配置文件"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(json.dumps(self.main_config), encoding="utf-8")
        self.proxy_path = config_dir / "proxy_list.json"
        self.proxy_document = {"proxies": [{"id": "151", "protocol": "socks5", "host": "192.0.2.151", "port": 9820,
                                            "username": "user", "password": "secret", "enabled": True,
                                            "region": {"country": "US", "language": "en", "timezone": "America/Los_Angeles"}},
                                           {"id": "last", "transit": {"protocol": "vless", "host": "192.0.2.20", "port": 443,
                                             "uuid": "00000000-0000-4000-8000-000000000000"}}]}
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        bindings_path = self.config_path.with_name("batch10_bindings.example.json")
        bindings_path.write_text(json.dumps({"151.1": "151", "151.2": "151"}), encoding="utf-8")
        self.setup = Mock()
        self.setup.run.return_value = {"success": True, "system": {"success": True}, "verification": {"verified": True}}
        self.setup.device.restore_stopped.return_value = {"success": True}
        self.setup.device.list_instances.side_effect = lambda: {
            item["db_id"]: item for item in self.creator._list_instances.return_value}
        self.installer = Mock()
        self.installer.install_from_local.side_effect = lambda source, ids, **options: {
            "success": True, "devices": {db_id: {"status": "installed", "package_verified": True} for db_id in ids}}

        def save():
            Path("creation_results.json").write_text(json.dumps(self.creator.creation_results), encoding="utf-8")

        self.creator.save_results.side_effect = save

    def run_entry(self, *arguments):
        self.output = io.StringIO()
        previous = Path.cwd()
        with patch.object(entry, "ROOT", self.root), patch.object(entry, "DeviceCreator", return_value=self.creator), \
                patch.object(entry, "ProxySetup", return_value=self.setup), \
                patch.object(entry, "TikTokInstaller", return_value=self.installer), \
                patch.object(entry, "check_apk", self.apk_check), redirect_stdout(self.output), redirect_stderr(self.output):
            result = entry.main(list(arguments))
        self.assertEqual(Path.cwd(), previous)
        return result

    def test_default_config_and_automatic_result_directory(self):
        def create():
            self.creator.creation_results = [{"status": "success"}] * 2
            self.creator.created_devices = [{"user_name": "151.1", "db_id": "EDGE1"},
                                            {"user_name": "151.2", "db_id": "EDGE2"}]
            self.creator._list_instances.return_value = [dict(device, state="stopped") for device in self.creator.created_devices]
        self.creator.create_batch.side_effect = create
        self.assertEqual(self.run_entry(), 0, self.output.getvalue())
        job = next((self.root / "日志").iterdir())
        snapshot = json.loads((job / "devices_input.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["count"], 2)
        self.assertEqual(snapshot["default_config"], self.config["default_config"])
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8-sig")), self.config)
        self.assertEqual(len(json.loads((job / "creation_results.json").read_text(encoding="utf-8"))), 2)
        self.assertIn("2/2", self.output.getvalue())
        self.assertEqual(self.setup.run.call_count, 2)
        self.assertEqual(self.installer.install_from_local.call_count, 2)
        self.assertEqual(self.setup.device.restore_stopped.call_count, 2)
        self.apk_check.assert_called_once_with(self.main_config["tiktok"], self.root)
        self.assertEqual(json.loads((job / "apk_input.json").read_text(encoding="utf-8")), self.apk)
        for call in self.setup.run.call_args_list:
            self.assertTrue(call.args[1]["transit"])
            self.assertFalse(call.kwargs["restore_state"])
            self.assertTrue(call.kwargs["sync_system"])
        for call in self.installer.install_from_local.call_args_list:
            self.assertEqual(call.args[0], self.apk["path"])
            self.assertTrue(call.kwargs["verify"])
            self.assertFalse(call.kwargs["start_stopped"])
            self.assertFalse(call.kwargs["restore_state"])
        report = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertTrue(report["success"])
        self.assertTrue(all(d["tiktok_installed"] and d["state_restored"] for d in report["devices"]))

    def test_existing_instance_or_failed_preflight_prevents_creation(self):
        for instances, error in (([{"db_id": "EDGE1", "user_name": "151.1"}], None), ([], ValueError("unreachable"))):
            self.creator.reset_mock()
            self.creator._list_instances.return_value = instances
            self.creator._list_instances.side_effect = error
            self.assertEqual(self.run_entry(), 1)
            self.creator.create_batch.assert_not_called()

    def test_pending_result_returns_failure_and_saves_report(self):
        self.creator.creation_results = [{"status": "unconfirmed"}]
        self.assertEqual(self.run_entry(), 1)
        self.creator.save_results.assert_called_once()
        self.assertIn("勿整批重跑", self.output.getvalue())
        self.assertEqual(len(list((self.root / "日志").glob("*/creation_results.json"))), 1)

    def test_creation_exception_still_saves_report(self):
        self.creator.create_batch.side_effect = ValueError("simulated failure")
        self.assertEqual(self.run_entry(), 1)
        self.creator.save_results.assert_called_once()
        self.assertIn("simulated failure", self.output.getvalue())

    def test_missing_file_or_duplicate_names_never_submits(self):
        self.config["devices"].append(self.config["devices"][0])
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertEqual(self.run_entry(), 1)
        self.creator._list_instances.assert_not_called()
        self.config_path.unlink()
        self.assertEqual(self.run_entry(), 1)
        self.creator.create_batch.assert_not_called()

    def test_configure_only_reuses_existing_ids_and_stops_after_proxy_failure(self):
        self.creator._list_instances.return_value = [
            {"user_name": "151.1", "db_id": "EDGE1", "state": "running"},
            {"user_name": "151.2", "db_id": "EDGE2", "state": "stopped"}]
        self.setup.run.return_value = {"success": False}
        self.assertEqual(self.run_entry("--configure-only"), 1)
        self.creator.create_batch.assert_not_called()
        self.assertEqual(self.setup.run.call_count, 1)
        self.installer.install_from_local.assert_not_called()
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["success"])
        self.assertEqual(summary["devices"][0]["db_id"], "EDGE1")

    def test_single_name_configure_only_and_missing_transit_before_creation(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "running"}]
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 0, self.output.getvalue())
        self.creator.create_batch.assert_not_called()
        self.assertEqual(self.setup.run.call_count, 1)
        self.proxy_path.write_text(json.dumps({"proxies": self.proxy_document["proxies"][:1]}), encoding="utf-8")
        self.creator.reset_mock()
        self.assertEqual(self.run_entry(), 1)
        self.creator._list_instances.assert_not_called()

    def test_present_region_is_applied_and_other_mapping_entries_preserved(self):
        self.proxy_document["proxies"][0]["region"] = {"country": "US", "language": "en", "timezone": "America/Los_Angeles"}
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        mapping_path = self.root / "数据" / "device_mapping.json"
        mapping_path.parent.mkdir()
        other = {"name": "other", "proxy_id": "unchanged", "state": "running"}
        mapping_path.write_text(json.dumps({"host": "http://192.0.2.10:18182", "devices": {"OTHER": other}}), encoding="utf-8")
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "running"}]
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 0)
        self.assertTrue(self.setup.run.call_args.kwargs["sync_system"])
        self.assertEqual(json.loads(mapping_path.read_text(encoding="utf-8"))["devices"]["OTHER"], other)

    def test_missing_region_blocks_both_creation_and_configure_only(self):
        self.proxy_document["proxies"][0].pop("region")
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        for arguments in ((), ("--configure-only",)):
            self.assertEqual(self.run_entry(*arguments), 1)
            self.creator._list_instances.assert_not_called()
            self.creator.create_batch.assert_not_called()
            self.setup.run.assert_not_called()
            self.assertIn("不能跳过地区初始化", self.output.getvalue())

    def test_network_failure_keeps_system_result_and_missing_steps_cannot_succeed(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        for result in ({"success": False, "system": {"success": True}, "verification": {"verified": False}},
                       {"success": True, "verification": {"verified": True}}):
            with self.subTest(result=result):
                self.setup.run.return_value = result
                self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
                self.assertTrue(self.setup.run.call_args.kwargs["sync_system"])
                latest = max((self.root / "日志").glob("*/batch_status.json"), key=lambda p: p.stat().st_mtime_ns)
                report = json.loads(latest.read_text(encoding="utf-8"))
                self.assertFalse(report["success"])
                self.assertTrue(report["devices"][0]["system_requested"])
                self.assertEqual(report["devices"][0]["system_configured"], result.get("system", {}).get("success") is True)
                self.assertEqual(report["devices"][0]["network_verified"], result["verification"]["verified"])
        self.creator.create_batch.assert_not_called()

    def test_missing_apk_blocks_creation_and_existing_device_initialization(self):
        self.apk_check.side_effect = ValueError("APK不存在或为空")
        for arguments in ((), ("--configure-only",)):
            self.assertEqual(self.run_entry(*arguments), 1)
            self.creator._list_instances.assert_not_called()
            self.creator.create_batch.assert_not_called()
            self.setup.run.assert_not_called()
            self.installer.install_from_local.assert_not_called()

    def test_web_failure_still_installs_local_apk_and_keeps_network_failed(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        self.setup.run.return_value = {"success": False, "system": {"success": True},
                                       "verification": {"verified": False}, "error": "Google timeout"}
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
        self.installer.install_from_local.assert_called_once()
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        job = next((self.root / "日志").iterdir())
        report = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        device = report["devices"][0]
        self.assertFalse(report["success"])
        self.assertTrue(device["tiktok_installed"])
        self.assertFalse(device["network_verified"])
        self.assertEqual(device["error"], "Google timeout")

    def test_install_is_between_configuration_and_restore_and_pending_stops_batch(self):
        self.creator._list_instances.return_value = [
            {"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"},
            {"user_name": "151.2", "db_id": "EDGE2", "state": "stopped"}]
        calls = Mock()
        calls.attach_mock(self.setup.run, "configure")
        calls.attach_mock(self.installer.install_from_local, "install")
        calls.attach_mock(self.setup.device.restore_stopped, "restore")
        self.installer.install_from_local.side_effect = None
        self.installer.install_from_local.return_value = {
            "success": False, "devices": {"EDGE1": {"status": "unconfirmed"}}}
        self.assertEqual(self.run_entry("--configure-only"), 1)
        self.assertEqual([call[0] for call in calls.mock_calls], ["configure", "install", "restore"])
        self.setup.run.assert_called_once()
        job = next((self.root / "日志").iterdir())
        report = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["devices"]), 1)
        self.assertFalse(report["devices"][0]["tiktok_installed"])
        self.assertEqual(report["devices"][0]["install_status"], "unconfirmed")

    def test_existing_package_requires_verified_status_and_running_device_is_kept(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "running"}]
        self.installer.install_from_local.side_effect = None
        for verified, expected_exit in ((False, 1), (True, 0)):
            self.installer.install_from_local.return_value = {
                "success": True, "devices": {"EDGE1": {"status": "already_installed", "package_verified": verified}}}
            self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), expected_exit)
        self.setup.device.restore_stopped.assert_not_called()

    def test_install_exception_and_cleanup_failure_never_report_success(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        self.installer.install_from_local.side_effect = OSError("upload error")
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        self.installer.install_from_local.side_effect = None
        self.installer.install_from_local.return_value = {
            "success": True, "devices": {"EDGE1": {"status": "installed", "package_verified": True}}}
        self.setup.device.restore_stopped.return_value = {"success": False}
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
        latest = max((self.root / "日志").glob("*/batch_status.json"), key=lambda p: p.stat().st_mtime_ns)
        report = json.loads(latest.read_text(encoding="utf-8"))
        self.assertTrue(report["devices"][0]["tiktok_installed"])
        self.assertFalse(report["devices"][0]["state_restored"])
        self.assertFalse(report["success"])

    def test_configuration_exception_restores_stopped_target_without_installing(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        self.setup.run.side_effect = OSError("configuration interrupted")
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        self.installer.install_from_local.assert_not_called()
        latest = next((self.root / "日志").glob("*/batch_status.json"))
        device = json.loads(latest.read_text(encoding="utf-8"))["devices"][0]
        self.assertEqual(device["error"], "configuration interrupted")
        self.assertTrue(device["state_restored"])
        self.assertFalse(device["success"])


if __name__ == "__main__":
    unittest.main()
