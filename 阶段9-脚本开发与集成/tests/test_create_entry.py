"""简洁创建入口的离线检查，不连接 VMOS。"""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
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

    def run_entry(self, *arguments, operation="initialize", create_only=False):
        self.output = io.StringIO()
        previous = Path.cwd()
        def setup(host, **options):
            self.setup.mapping_path = options["mapping_path"]
            self.setup.output_dir = options["output_dir"]
            return self.setup
        with patch.object(entry, "ROOT", self.root), patch.object(entry, "DeviceCreator", return_value=self.creator), \
                patch.object(entry, "ProxySetup", side_effect=setup), \
                patch.object(entry, "TikTokInstaller", return_value=self.installer), \
                patch.object(entry, "check_apk", self.apk_check), redirect_stdout(self.output), redirect_stderr(self.output):
            result = entry.main(list(arguments), operation=operation, create_only=create_only)
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

    def test_single_name_targets_only_selected_existing_machine(self):
        self.creator._list_instances.return_value = [
            {"user_name": "151.1", "db_id": "EDGE1", "state": "running"},
            {"user_name": "151.2", "db_id": "EDGE2", "state": "stopped"}]
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.2", "--workers", "3"), 0)
        self.creator.create_batch.assert_not_called()
        self.setup.run.assert_called_once()
        self.assertEqual(self.setup.run.call_args.args[0], "EDGE2")
        self.installer.install_from_local.assert_called_once()
        self.assertEqual(self.installer.install_from_local.call_args.args[1], ["EDGE2"])
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE2"])
        job = next((self.root / "日志").iterdir())
        report = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["workers"], 1)
        self.assertEqual([d["name"] for d in report["devices"]], ["151.2"])
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8-sig")), self.config)

    def test_create_only_selects_multiple_names_without_proxy_or_apk_dependencies(self):
        self.config["devices"].append({"user_name": "151.3", "remark": "151.3"})
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.proxy_path.unlink()
        self.config_path.with_name("batch10_bindings.example.json").unlink()
        self.apk_check.side_effect = ValueError("APK不应被读取")
        def create():
            self.creator.creation_results = [{"status": "success"}] * 2
            self.creator.created_devices = [{"user_name": "151.1", "db_id": "EDGE1"},
                                            {"user_name": "151.3", "db_id": "EDGE3"}]
        self.creator.create_batch.side_effect = create
        self.assertEqual(self.run_entry("--create-only", "--names", "151.1", "151.3"), 0)
        job = next((self.root / "日志").iterdir())
        snapshot = json.loads((job / "devices_input.json").read_text(encoding="utf-8"))
        self.assertEqual([d["user_name"] for d in snapshot["devices"]], ["151.1", "151.3"])
        self.creator.create_batch.assert_called_once()
        self.setup.run.assert_not_called()
        self.installer.install_from_local.assert_not_called()
        self.apk_check.assert_not_called()

    def test_dedicated_create_entry_only_creates_selected_names(self):
        self.proxy_path.unlink()
        self.config_path.with_name("batch10_bindings.example.json").unlink()
        self.creator.creation_results = [{"status": "success"}]
        self.creator.created_devices = [{"user_name": "151.2", "db_id": "EDGE2"}]
        self.assertEqual(self.run_entry("--names", "151.2", "--workers", "3", create_only=True), 0)
        self.creator.create_batch.assert_called_once()
        self.setup.run.assert_not_called()
        self.installer.install_from_local.assert_not_called()
        self.apk_check.assert_not_called()
        job = next((self.root / "日志").iterdir())
        snapshot = json.loads((job / "devices_input.json").read_text(encoding="utf-8"))
        self.assertEqual([d["user_name"] for d in snapshot["devices"]], ["151.2"])
        report = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(report["operation"], "create")
        self.assertTrue(report["success"])

    def test_multiple_names_reject_duplicates_and_unknown_before_contacting_host(self):
        for names in (("151.1", "151.1"), ("151.1", "missing")):
            self.assertEqual(self.run_entry("--configure-only", "--names", *names), 1)
            self.creator._list_instances.assert_not_called()
            self.setup.run.assert_not_called()

    def test_proxy_only_accepts_multiple_names_without_region_or_apk(self):
        self.prepare_concurrent_devices(3)
        self.proxy_document["proxies"][0].pop("region")
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        self.setup.run.return_value = {"success": True, "verification": {"verified": True}}
        self.assertEqual(self.run_entry("--names", "151.1", "151.3", operation="proxy"), 0)
        self.assertEqual([c.args[0] for c in self.setup.run.call_args_list], ["EDGE1", "EDGE3"])
        self.assertTrue(all(c.kwargs["sync_system"] is False for c in self.setup.run.call_args_list))
        self.creator.create_batch.assert_not_called()
        self.installer.install_from_local.assert_not_called()
        self.apk_check.assert_not_called()

    def test_install_only_selects_multiple_targets_without_proxy_dependencies(self):
        self.prepare_concurrent_devices(3)
        self.proxy_path.unlink()
        self.config_path.with_name("batch10_bindings.example.json").unlink()
        self.assertEqual(self.run_entry("--names", "151.2", "151.3", operation="install"), 0)
        self.assertEqual([c.args[1] for c in self.installer.install_from_local.call_args_list], [["EDGE2"], ["EDGE3"]])
        self.assertTrue(all(c.kwargs["start_stopped"] is True for c in self.installer.install_from_local.call_args_list))
        self.assertEqual([c.args[0] for c in self.setup.device.restore_stopped.call_args_list], [["EDGE2"], ["EDGE3"]])
        self.setup.run.assert_not_called()
        self.creator.create_batch.assert_not_called()
        summary = json.loads(next((self.root / "日志").glob("*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertTrue(summary["success"])
        self.assertTrue(all(not d["system_requested"] and not d["network_verified"] for d in summary["devices"]))

    def test_region_only_uses_configured_region_and_preserves_running_state(self):
        self.prepare_concurrent_devices(2)
        self.creator._list_instances.return_value[0]["state"] = "running"
        self.proxy_document["proxies"][0]["region"].update(latitude=47.6062, longitude=-122.3321)
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        def configure(db, expected, report, previous=None):
            report.update(success=True, expected=expected, country_api_confirmed=True, gps_persisted=True)
        system = Mock()
        system.configure.side_effect = configure
        self.setup.device.wait_ready.side_effect = lambda ids: ids
        self.setup.device.post_action.return_value = {"code": 200}
        with patch.object(entry, "SystemSetup", return_value=system), patch.object(entry, "check_proxy") as probe:
            self.assertEqual(self.run_entry("--names", "151.1", "151.2", operation="region"), 0)
        self.assertEqual([c.args[0] for c in system.configure.call_args_list], ["EDGE1", "EDGE2"])
        self.setup.device.post_action.assert_called_once_with("run", ["EDGE2"])
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE2"])
        self.setup.run.assert_not_called()
        self.setup.apply.assert_not_called()
        self.installer.install_from_local.assert_not_called()
        self.apk_check.assert_not_called()
        probe.assert_not_called()
        mapping = json.loads((self.root / "数据/device_mapping.json").read_text(encoding="utf-8"))
        self.assertTrue(mapping["devices"]["EDGE2"]["system_config"]["gps_persisted"])

    def test_region_failure_stops_dispatch_and_restores_target(self):
        self.prepare_concurrent_devices(2)
        self.proxy_document["proxies"][0]["region"].update(latitude=47.6062, longitude=-122.3321)
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        self.setup.device.wait_ready.side_effect = lambda ids: ids
        self.setup.device.post_action.return_value = {"code": 200}
        with patch.object(entry, "SystemSetup") as system:
            system.return_value.configure.side_effect = ValueError("语言回查未确认")
            self.assertEqual(self.run_entry("--names", "151.1", "151.2", operation="region"), 1)
        self.setup.device.post_action.assert_called_once_with("run", ["EDGE1"])
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        summary = json.loads(next((self.root / "日志").glob("*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertEqual(summary["not_started"], ["151.2"])
        self.assertIn("语言回查未确认", summary["devices"][0]["error"])

    def test_region_missing_gps_requires_matching_existing_exit_without_setting_proxy(self):
        self.prepare_concurrent_devices(1)
        self.main_config["proxy"].update(probe_url="https://api.ipify.org?format=json", timeout=20)
        (self.root / "配置文件/config.json").write_text(json.dumps(self.main_config), encoding="utf-8")
        self.setup.device.wait_ready.side_effect = lambda ids: ids
        self.setup.device.post_action.return_value = {"code": 200}
        self.setup.cloud_json.return_value = {"ip": "192.0.2.2"}
        with patch.object(entry, "SystemSetup") as system, patch.object(entry, "check_proxy", return_value={"exit_ip": "192.0.2.1"}):
            self.assertEqual(self.run_entry("--name", "151.1", operation="region"), 1)
        system.return_value.configure.assert_not_called()
        self.setup.run.assert_not_called()
        self.setup.apply.assert_not_called()
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        self.assertIn("请先设置代理", self.output.getvalue())

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

    def test_install_rejection_detail_is_in_batch_and_console(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        self.installer.install_from_local.side_effect = None
        self.installer.install_from_local.return_value = {
            "success": False, "devices": {"EDGE1": {"status": "unconfirmed", "error": "主机安装队列忙（业务码201）"}}}
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
        self.assertIn("业务码201", self.output.getvalue())
        job = next((self.root / "日志").iterdir())
        record = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))["devices"][0]
        self.assertEqual(record["install_error"], "主机安装队列忙（业务码201）")
        self.assertTrue(record["state_restored"])

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

    def test_cleanup_failure_preserves_configuration_failure_and_restore_evidence(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        self.setup.run.return_value = {"success": False, "error": "重启或地区API就绪未确认"}
        self.setup.device.restore_stopped.return_value = {
            "success": False, "error": "等待启动结束超时", "states": {"EDGE1": "starting"}}
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
        latest = next((self.root / "日志").glob("*/batch_status.json"))
        device = json.loads(latest.read_text(encoding="utf-8"))["devices"][0]
        self.assertIn("重启或地区API就绪未确认", device["error"])
        self.assertIn("等待启动结束超时", device["error"])
        self.assertEqual(device["restore_state"]["states"], {"EDGE1": "starting"})
        self.assertFalse(device["state_restored"])

    def test_invalid_workers_never_contacts_host_and_single_target_caps_workers(self):
        for workers in ("0", "-2"):
            self.assertEqual(self.run_entry("--configure-only", "--workers", workers), 1)
            self.creator._list_instances.assert_not_called()
            self.setup.run.assert_not_called()
            self.assertIn("并发数", self.output.getvalue())
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "running"}]
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1", "--workers", "8"), 0)
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["workers"], 1)

    def prepare_concurrent_devices(self, count):
        self.config["devices"] = [{"user_name": f"151.{index}"} for index in range(1, count + 1)]
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.config_path.with_name("batch10_bindings.example.json").write_text(
            json.dumps({item["user_name"]: "151" for item in self.config["devices"]}), encoding="utf-8")
        self.creator._list_instances.return_value = [
            {"user_name": item["user_name"], "db_id": f"EDGE{index}", "state": "stopped"}
            for index, item in enumerate(self.config["devices"], 1)]

    def test_workers_overlap_with_bounded_tasks_and_preserve_each_result(self):
        self.prepare_concurrent_devices(5)
        barrier = threading.Barrier(2)
        guard = threading.Lock()
        active, peak = 0, 0
        started = []

        def configure(db_id, *args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
                started.append(db_id)
            if db_id in ("EDGE1", "EDGE2"):
                barrier.wait(timeout=5)
            return {"success": True, "system": {"success": True}, "verification": {"verified": True}}

        def final_instances():
            nonlocal active
            with guard:
                active -= 1
            return {item["db_id"]: item for item in self.creator._list_instances.return_value}

        self.setup.run.side_effect = configure
        self.setup.device.list_instances.side_effect = final_instances
        self.assertEqual(self.run_entry("--configure-only", "--workers", "2"), 0, self.output.getvalue())
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
        self.assertEqual(len(started), 5)
        self.assertEqual(self.installer.install_from_local.call_count, 5)
        self.assertEqual(self.setup.device.restore_stopped.call_count, 5)
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["success"])
        self.assertEqual(summary["workers"], 2)
        self.assertEqual(summary["not_started"], [])
        self.assertEqual([item["name"] for item in summary["devices"]], [f"151.{i}" for i in range(1, 6)])
        self.assertTrue(all(item["success"] and item["state_restored"] for item in summary["devices"]))
        mapping = json.loads((self.root / "数据/device_mapping.json").read_text(encoding="utf-8"))
        self.assertEqual(len(mapping["devices"]), 5)
        self.assertTrue(all(item["state"] == "stopped" for item in mapping["devices"].values()))

    def test_parallel_network_failure_stops_dispatch_before_install_finishes_and_drains_active_tasks(self):
        self.prepare_concurrent_devices(4)
        barrier = threading.Barrier(2)
        failed_install = threading.Event()
        other_restored = threading.Event()

        def configure(db_id, *args, **kwargs):
            barrier.wait(timeout=5)
            if db_id == "EDGE2":
                self.assertTrue(failed_install.wait(timeout=5))
            return {"success": db_id != "EDGE1", "system": {"success": True},
                    "verification": {"verified": db_id != "EDGE1"}}

        def install(source, ids, **options):
            if ids == ["EDGE1"]:
                failed_install.set()
                self.assertTrue(other_restored.wait(timeout=5))
            return {"success": True, "devices": {ids[0]: {"status": "installed", "package_verified": True}}}

        def restore(ids):
            if ids == ["EDGE2"]:
                other_restored.set()
            return {"success": True}

        self.setup.run.side_effect = configure
        self.installer.install_from_local.side_effect = install
        self.setup.device.restore_stopped.side_effect = restore
        self.assertEqual(self.run_entry("--configure-only", "--workers", "2"), 1)
        self.assertEqual(self.setup.run.call_count, 2)
        self.assertEqual(self.installer.install_from_local.call_count, 2)
        self.assertEqual(self.setup.device.restore_stopped.call_count, 2)
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["success"])
        self.assertEqual(summary["not_started"], ["151.3", "151.4"])
        self.assertFalse(summary["devices"][0]["network_verified"])
        self.assertTrue(summary["devices"][0]["tiktok_installed"])
        self.assertTrue(summary["devices"][1]["success"])
        self.assertTrue(all(item["state_restored"] for item in summary["devices"]))


if __name__ == "__main__":
    unittest.main()
