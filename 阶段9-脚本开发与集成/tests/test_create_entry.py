"""简洁创建入口的离线检查，不连接 VMOS。"""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("create_entry", ROOT / "创建云机.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
prepare_spec = importlib.util.spec_from_file_location("prepare_host_entry", ROOT / "准备主机.py")
host_prepare = importlib.util.module_from_spec(prepare_spec)
prepare_spec.loader.exec_module(host_prepare)


class CreateEntryTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = 0
        def advance(seconds):
            self.elapsed += seconds
        sleep = patch("check_vmos_connection.time.sleep", side_effect=advance)
        sleep.start()
        self.addCleanup(sleep.stop)
        clock = patch("check_vmos_connection.time.monotonic", side_effect=lambda: self.elapsed)
        clock.start()
        self.addCleanup(clock.stop)
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
        self.setup.run.return_value = {"success": True, "proxy_configured": True, "proxy_attempted": True, "rom_ready": True,
                                       "system": {"success": False, "settings_attempted": True}, "verification": {"verified": True}}
        self.setup.device.restore_stopped.return_value = {"success": True}
        self.setup.device.list_instances.side_effect = lambda: {
            item["db_id"]: item for item in self.creator._list_instances.return_value}
        self.installer = Mock()
        self.installer.list_instances.side_effect = lambda: {
            item["db_id"]: item for item in self.creator._list_instances.return_value}
        self.installer.install_from_local.side_effect = lambda source, ids, **options: {
            "success": True, "devices": {db_id: {"status": "installed", "package_verified": True} for db_id in ids}}

        def save():
            Path("creation_results.json").write_text(json.dumps(self.creator.creation_results), encoding="utf-8")

        self.creator.save_results.side_effect = save

    def run_entry(self, *arguments, operation="initialize", create_only=False):
        self.output = io.StringIO()
        previous = Path.cwd()
        def setup(host, **options):
            if hasattr(self, "setup_factory"):
                return self.setup_factory(host, **options)
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

    def prepare_host_profile(self, host="192.0.2.10", source=None, bindings=None):
        with patch.object(entry, "ROOT", self.root), patch.object(host_prepare, "ROOT", self.root), \
                patch.object(host_prepare, "host_directory", entry.host_directory), redirect_stdout(io.StringIO()):
            host_prepare.prepare(host, source, bindings)
        return self.root / "主机配置" / host

    def test_host_profile_uses_own_resources_mapping_and_logs_without_legacy_files(self):
        self.prepare_concurrent_devices(2)
        profile = self.prepare_host_profile()
        for path in (self.config_path, self.proxy_path, self.root / "配置文件/config.json",
                     self.config_path.with_name("batch10_bindings.example.json")):
            path.unlink()
        self.assertEqual(self.run_entry("--host", "192.0.2.10", "--configure-only", "--name", "151.2"), 0,
                         self.output.getvalue())
        mapping_path = self.root / "数据/192.0.2.10/device_mapping.json"
        self.assertEqual(self.setup.mapping_path, mapping_path)
        mapping = entry.read_json(mapping_path)
        self.assertEqual(set(mapping["devices"]), {"EDGE2"})
        self.assertEqual(mapping["host"], "http://192.0.2.10:18182")
        self.assertFalse((self.root / "数据/device_mapping.json").exists())
        job = next((self.root / "日志/192.0.2.10").iterdir())
        self.assertTrue(entry.read_json(job / "batch_status.json")["success"])
        self.assertIn("transit", entry.read_json(profile / "proxy_list.json")["proxies"][0])
        self.apk_check.assert_called_once_with(self.main_config["tiktok"], self.root)

    def test_host_override_rejects_wrong_batch_missing_profile_and_bad_address_before_requests(self):
        self.prepare_host_profile()
        arguments = [
            ["--host", "140"], ["--host", "http://192.0.2.10"], ["--host", "192.0.2.10:18182"],
            ["--host", "../escape"], ["--host", "192.0.2.11"],
        ]
        self.prepare_host_profile("192.0.2.11")
        # 已准备目录但显式传入其他主机清单，也必须拒绝。
        arguments[-1] = [str(self.config_path), "--host", "192.0.2.11"]
        arguments.append(["--host", "192.0.2.12"])
        for args in arguments:
            with self.subTest(args=args):
                self.assertEqual(self.run_entry(*args, operation="proxy"), 1)
        self.creator._list_instances.assert_not_called()
        self.setup.run.assert_not_called()
        self.assertFalse((self.root / "日志").exists())

    def test_host_install_remains_independent_of_proxy_and_bindings(self):
        self.prepare_concurrent_devices(2)
        profile = self.prepare_host_profile()
        (profile / "proxy_list.json").unlink()
        (profile / "bindings.json").unlink()
        mapping_path = self.root / "数据/192.0.2.10/device_mapping.json"
        previous = mapping_path.read_bytes()
        self.assertEqual(self.run_entry("--host", "192.0.2.10", operation="install"), 0, self.output.getvalue())
        self.assertEqual(mapping_path.read_bytes(), previous)
        self.assertEqual(len(list((self.root / "日志/192.0.2.10").glob("*/device_mapping.json"))), 1)
        self.setup.run.assert_not_called()

    def test_host_create_only_uses_selected_manifest_without_proxy_or_apk(self):
        profile = self.prepare_host_profile()
        (profile / "proxy_list.json").unlink()
        (profile / "bindings.json").unlink()
        self.creator.creation_results = [{"status": "success", "user_name": "151.1", "db_id": "EDGE1"}]
        self.creator.created_devices = [{"user_name": "151.1", "db_id": "EDGE1"}]
        self.assertEqual(self.run_entry("--host", "192.0.2.10", "--name", "151.1", create_only=True), 0,
                         self.output.getvalue())
        self.creator.create_batch.assert_called_once()
        self.apk_check.assert_not_called()
        self.setup.run.assert_not_called()

    def test_host_explicit_manifest_and_bindings_override_defaults(self):
        self.prepare_concurrent_devices(2)
        profile = self.prepare_host_profile()
        (profile / "batch_create.json").unlink()
        (profile / "bindings.json").unlink()
        bindings = self.config_path.with_name("batch10_bindings.example.json")
        self.assertEqual(self.run_entry(str(self.config_path), "--host", "192.0.2.10", "--bindings", str(bindings),
                                        "--name", "151.1", operation="proxy"), 0, self.output.getvalue())
        self.setup.run.assert_called_once()

    def test_host_preparation_preserves_sources_and_imports_only_matching_mapping(self):
        mapping_path = self.root / "数据/device_mapping.json"
        mapping_path.parent.mkdir()
        old = {"host": "http://192.0.2.10:18182", "devices": {"OLD": {"name": "old.1", "account_email": "saved"}}}
        entry.save_json(mapping_path, old)
        sources = [self.config_path, self.proxy_path, mapping_path, self.root / "配置文件/config.json"]
        before = {path: path.read_bytes() for path in sources}
        self.prepare_host_profile()
        other = self.prepare_host_profile("192.0.2.11")
        self.assertEqual(entry.read_json(self.root / "数据/192.0.2.10/device_mapping.json"), old)
        self.assertEqual(entry.read_json(self.root / "数据/192.0.2.11/device_mapping.json")["devices"], {})
        self.assertEqual(entry.read_json(other / "batch_create.json")["devices"], [])
        self.assertEqual(entry.read_json(other / "config.json")["vmos"]["host_ip"], "192.0.2.11")
        self.assertEqual(before, {path: path.read_bytes() for path in sources})
        with self.assertRaisesRegex(ValueError, "已存在"):
            self.prepare_host_profile()

    def test_host_preparation_pairs_timestamped_backups_and_rejects_wrong_host_or_bindings(self):
        backup = self.config_path.with_name(self.config_path.name + ".20261104-120000.bak")
        backup.write_bytes(self.config_path.read_bytes())
        bindings = backup.with_name(backup.name.replace("_create.", "_bindings."))
        entry.save_json(bindings, {"151.1": "151", "151.2": "151"})
        self.config_path.with_name("batch10_bindings.example.json").unlink()
        profile = self.prepare_host_profile(source=backup)
        self.assertEqual(entry.read_json(profile / "bindings.json"), entry.read_json(bindings))
        with self.assertRaisesRegex(ValueError, "host_ip 不一致"):
            self.prepare_host_profile("192.0.2.11", backup)
        bad = dict(self.config, host_ip="192.0.2.11")
        entry.save_json(backup, bad)
        entry.save_json(bindings, {"151.1": "missing", "151.2": "151"})
        with self.assertRaisesRegex(ValueError, "有效代理绑定"):
            self.prepare_host_profile("192.0.2.11", backup)
        self.assertFalse((self.root / "主机配置/192.0.2.11").exists())

    def test_two_processes_run_different_hosts_without_touching_shared_files(self):
        hosts = ("192.0.2.10", "192.0.2.11")
        for host in hosts:
            profile = self.prepare_host_profile(host)
            config = dict(self.config, host_ip=host, devices=self.config["devices"][:1], count=1)
            entry.save_json(profile / "batch_create.json", config)
            entry.save_json(profile / "bindings.json", {"151.1": "151"})
        sources = [self.config_path, self.proxy_path, self.root / "配置文件/config.json"]
        before = {path: path.read_bytes() for path in sources}
        # 两个真正独立进程只模拟API边界；磁盘配置、调度、映射和报告仍走正式实现。
        script = textwrap.dedent('''
            import importlib.util, json, sys, time
            from pathlib import Path
            from unittest.mock import Mock
            import requests
            requests.sessions.Session.request = Mock(side_effect=AssertionError("offline test"))
            spec = importlib.util.spec_from_file_location("entry", sys.argv[1])
            entry = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(entry)
            entry.ROOT = Path(sys.argv[2])
            host = sys.argv[3]
            device = {"db_id": "EDGE" + host.rsplit(".", 1)[1], "user_name": "151.1", "state": "running"}
            def creator(snapshot):
                assert json.loads(Path(snapshot).read_text(encoding="utf-8"))["host_ip"] == host
                result = Mock()
                result._list_instances.return_value = [device]
                return result
            def setup(url, **options):
                assert url == "http://" + host + ":18182"
                result = Mock(mapping_path=options["mapping_path"], output_dir=options["output_dir"])
                result.device.list_instances.return_value = {device["db_id"]: device}
                def run(*args, **kwargs):
                    (entry.ROOT / (host + ".ready")).touch()
                    deadline = time.monotonic() + 10
                    while len(list(entry.ROOT.glob("*.ready"))) < 2:
                        assert time.monotonic() < deadline, "other process did not arrive"
                        time.sleep(0.01)
                    return {"success": True, "proxy_configured": True, "proxy_attempted": True}
                result.run.side_effect = run
                return result
            entry.DeviceCreator = creator
            entry.ProxySetup = setup
            raise SystemExit(entry.main(["--host", host], operation="proxy"))
        ''')
        processes = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", script, str(ROOT / "创建云机.py"),
                                       str(self.root), host], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8") for host in hosts]
        try:
            for host, process in zip(hosts, processes):
                output, _ = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, output)
                mapping = entry.read_json(self.root / "数据" / host / "device_mapping.json")
                self.assertEqual(mapping["host"], f"http://{host}:18182")
                self.assertEqual(set(mapping["devices"]), {"EDGE" + host.rsplit(".", 1)[1]})
                reports = list((self.root / "日志" / host).glob("*/batch_status.json"))
                self.assertEqual(len(reports), 1)
                self.assertTrue(entry.read_json(reports[0])["success"])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        self.assertEqual(before, {path: path.read_bytes() for path in sources})
        self.assertFalse((self.root / "数据/device_mapping.json").exists())

    def test_default_config_and_automatic_result_directory(self):
        def create():
            self.creator.creation_results = [{"status": "success", "user_name": "151.1", "db_id": "EDGE1"},
                                             {"status": "success", "user_name": "151.2", "db_id": "EDGE2"}]
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
            self.assertFalse(call.kwargs["check_network"])
            self.assertFalse(call.kwargs["verify_system"])
            self.assertEqual(call.kwargs["wait_time"], 120)
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
        self.assertIn("勿重复创建", self.output.getvalue())
        self.assertEqual(len(list((self.root / "日志").glob("*/creation_results.json"))), 1)

    def test_partial_creation_continues_confirmed_device_and_saves_failed_names(self):
        def create():
            self.creator.creation_results = [
                {"user_name": "151.1", "db_id": "EDGE1", "status": "success"},
                {"user_name": "151.2", "status": "unconfirmed"}]
            self.creator.created_devices = [{"user_name": "151.1", "db_id": "EDGE1"}]
            self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        self.creator.create_batch.side_effect = create
        self.assertEqual(self.run_entry(), 1)
        self.setup.run.assert_called_once()
        self.assertEqual(self.setup.run.call_args.args[0], "EDGE1")
        self.installer.install_from_local.assert_called_once()
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual((summary["total"], summary["success_count"], summary["failed_count"]), (2, 1, 1))
        self.assertEqual(summary["failed_names"], ["151.2"])
        self.assertEqual((job / "failed_names.txt").read_text(encoding="utf-8"), "151.2\n")
        self.assertIn("1台成功、1台未完成", self.output.getvalue())

    def test_created_identity_can_appear_on_fifth_read_without_resubmitting(self):
        self.config["devices"] = self.config["devices"][:1]
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        reads = 0
        def list_instances():
            nonlocal reads
            reads += 1
            if reads < 6:
                return []
            return [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        self.creator._list_instances.side_effect = list_instances
        self.setup.device.list_instances.side_effect = lambda: {
            "EDGE1": {"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}}
        self.creator.creation_results = [{"user_name": "151.1", "db_id": "EDGE1", "status": "success"}]
        self.creator.created_devices = [{"user_name": "151.1", "db_id": "EDGE1"}]
        self.assertEqual(self.run_entry(), 0, self.output.getvalue())
        self.assertEqual(reads, 6)  # 一次创建前基线，五轮创建后确认。
        self.creator.create_batch.assert_called_once()
        self.setup.run.assert_called_once()

    def test_missing_created_identity_waits_two_minutes_but_other_device_continues(self):
        def create():
            self.creator.created_devices = [{"user_name": "151.1", "db_id": "EDGE1"},
                                            {"user_name": "151.2", "db_id": "EDGE2"}]
            self.creator.creation_results = [dict(item, status="success") for item in self.creator.created_devices]
            self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        self.creator.create_batch.side_effect = create
        self.assertEqual(self.run_entry(), 1)
        self.assertGreater(self.creator._list_instances.call_count, 20)
        self.assertEqual(self.elapsed, 120)
        self.setup.run.assert_called_once()
        self.assertEqual(self.setup.run.call_args.args[0], "EDGE1")
        self.creator.create_batch.assert_called_once()
        job = next((self.root / "日志").iterdir())
        report = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failed_names"], ["151.2"])

    def test_failed_restore_uses_five_read_evidence_without_extra_polling(self):
        self.prepare_concurrent_devices(2)
        self.setup.device.restore_stopped.return_value = {
            "success": False, "states": {"EDGE1": "running", "EDGE2": "running"},
            "error": "五轮回查仍未停止"}
        self.assertEqual(self.run_entry("--configure-only"), 1)
        self.assertEqual(self.setup.run.call_count, 2)
        self.setup.device.list_instances.assert_not_called()
        job = next((self.root / "日志").iterdir())
        report = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failed_count"], 2)
        self.assertTrue(all(item["final_state"] == "running" for item in report["devices"]))

    def test_ambiguous_created_identity_is_skipped_while_other_device_continues(self):
        def create():
            self.creator.created_devices = [{"user_name": "151.1", "db_id": "EDGE1"},
                                            {"user_name": "151.2", "db_id": "EDGE2"}]
            self.creator.creation_results = [dict(item, status="success") for item in self.creator.created_devices]
            self.creator._list_instances.return_value = [dict(item, state="stopped") for item in self.creator.created_devices]
            self.creator._list_instances.return_value.append({"user_name": "151.1", "db_id": "OTHER", "state": "stopped"})
        self.creator.create_batch.side_effect = create
        self.assertEqual(self.run_entry(), 1)
        self.assertGreater(self.creator._list_instances.call_count, 20)
        self.assertEqual(self.elapsed, 120)
        self.setup.run.assert_called_once()
        self.assertEqual(self.setup.run.call_args.args[0], "EDGE2")
        report = json.loads(next((self.root / "日志").glob("*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertEqual(report["failed_names"], ["151.1"])

    def test_duplicate_existing_id_never_reaches_write_operations(self):
        self.creator._list_instances.return_value = [
            {"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"},
            {"user_name": "other", "db_id": "EDGE1", "state": "stopped"}]
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
        self.setup.run.assert_not_called()
        self.setup.device.post_action.assert_not_called()
        self.installer.install_from_local.assert_not_called()

    def test_duplicate_id_skips_only_ambiguous_target_and_continues_other_devices(self):
        self.creator._list_instances.return_value = [
            {"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"},
            {"user_name": "151.3", "db_id": "EDGE1", "state": "stopped"},
            {"user_name": "151.2", "db_id": "EDGE2", "state": "running"}]
        self.assertEqual(self.run_entry("--configure-only"), 1)
        self.setup.run.assert_called_once()
        self.assertEqual(self.setup.run.call_args.args[0], "EDGE2")
        self.installer.install_from_local.assert_called_once()
        self.assertEqual(self.installer.install_from_local.call_args.args[1], ["EDGE2"])
        summary = json.loads(next((self.root / "日志").glob("*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertEqual(summary["failed_names"], ["151.1", "151.3"])
        self.assertEqual(summary["devices"][0]["name"], "151.2")
        self.assertEqual((summary["total"], summary["success_count"], summary["failed_count"]), (3, 1, 2))

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

    def test_configure_only_reuses_existing_ids_and_continues_after_proxy_failure(self):
        self.creator._list_instances.return_value = [
            {"user_name": "151.1", "db_id": "EDGE1", "state": "running"},
            {"user_name": "151.2", "db_id": "EDGE2", "state": "stopped"}]
        self.setup.run.return_value = {"success": False}
        self.assertEqual(self.run_entry("--configure-only"), 1)
        self.creator.create_batch.assert_not_called()
        self.assertEqual(self.setup.run.call_count, 2)
        self.installer.install_from_local.assert_not_called()
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["success"])
        self.assertEqual(summary["devices"][0]["db_id"], "EDGE1")
        self.assertEqual(summary["failed_names"], ["151.1", "151.2"])
        self.assertEqual(summary["not_started"], [])

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
            self.creator.creation_results = [{"status": "success", "user_name": "151.1", "db_id": "EDGE1"},
                                             {"status": "success", "user_name": "151.3", "db_id": "EDGE3"}]
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
        self.creator.creation_results = [{"status": "success", "user_name": "151.2", "db_id": "EDGE2"}]
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

    def test_duplicate_names_reject_before_contacting_host(self):
        self.assertEqual(self.run_entry("--configure-only", "--names", "151.1", "151.1"), 1)
        self.creator._list_instances.assert_not_called()
        self.setup.run.assert_not_called()

    def test_existing_operation_accepts_device_missing_from_creation_manifest(self):
        self.prepare_concurrent_devices(2)
        self.config["devices"] = self.config["devices"][:1]
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

        self.assertEqual(self.run_entry("--configure-only", "--name", "151.2"), 0, self.output.getvalue())

        self.assertEqual(self.setup.run.call_count, 1)
        self.assertEqual(self.setup.run.call_args.args[0], "EDGE2")
        inventory = entry.read_json(self.root / "数据/192.0.2.10/device_inventory.json")
        self.assertEqual([item["user_name"] for item in inventory["devices"]], ["151.1", "151.2"])

    def test_existing_operation_rejects_partial_live_list_match_before_changes(self):
        self.prepare_concurrent_devices(1)

        self.assertEqual(self.run_entry("--configure-only", "--names", "151.1", "151.2"), 1)

        self.assertIn("指定云机不在主机实时列表中：151.2", self.output.getvalue())
        self.setup.run.assert_not_called()
        self.assertTrue((self.root / "数据/192.0.2.10/device_inventory.json").is_file())

    def test_existing_proxy_operation_derives_group_without_bindings_file(self):
        self.prepare_concurrent_devices(2)
        self.config_path.with_name("batch10_bindings.example.json").unlink()

        self.assertEqual(self.run_entry("--configure-only", "--name", "151.2"), 0, self.output.getvalue())

        self.assertEqual(self.setup.run.call_args.args[1]["id"], "151")

    def test_proxy_only_accepts_multiple_names_without_region_or_apk(self):
        self.prepare_concurrent_devices(3)
        self.proxy_document["proxies"][0].pop("region")
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        self.setup.run.return_value = {"success": True, "proxy_configured": True, "verification": {"verified": True}}
        self.assertEqual(self.run_entry("--names", "151.1", "151.3", operation="proxy"), 0)
        self.assertEqual([c.args[0] for c in self.setup.run.call_args_list], ["EDGE1", "EDGE3"])
        self.assertTrue(all(c.kwargs["sync_system"] is False for c in self.setup.run.call_args_list))
        self.creator.create_batch.assert_not_called()
        self.installer.install_from_local.assert_not_called()
        self.apk_check.assert_not_called()

    def test_configure_submits_proxy_and_region_without_detection_or_install(self):
        self.prepare_concurrent_devices(2)
        self.setup.run.return_value = {"success": True, "proxy_attempted": True,
                                       "rom_ready": True,
                                       "system": {"settings_attempted": True}}
        self.assertEqual(self.run_entry("--names", "151.1", "151.2", operation="configure"), 0)
        self.assertEqual(self.setup.run.call_count, 2)
        for call in self.setup.run.call_args_list:
            self.assertTrue(call.kwargs["start_stopped"])
            self.assertTrue(call.kwargs["sync_system"])
            self.assertFalse(call.kwargs["check_network"])
            self.assertFalse(call.kwargs["verify_system"])
            self.assertFalse(call.kwargs["restore_state"])
        self.installer.install_from_local.assert_not_called()
        self.apk_check.assert_not_called()
        summary = json.loads(next((self.root / "日志").glob("*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertEqual(summary["operation"], "configure")
        self.assertTrue(summary["success"])
        self.assertTrue(all(device["system_requested"] for device in summary["devices"]))
        self.assertTrue(all(not device["system_configured"] and not device["proxy_configured"]
                            for device in summary["devices"]))

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

    def test_existing_initialization_submits_in_parallel_then_region_before_install_finishes(self):
        self.prepare_concurrent_devices(1)
        self.setup.device.post_action.return_value = {"code": 200}
        self.setup.device.wait_ready.side_effect = lambda ids: ids
        barrier = threading.Barrier(2)
        submitted, region_sent, installed = threading.Event(), threading.Event(), threading.Event()

        def install(source, ids, **options):
            self.assertFalse(options["start_stopped"])
            self.assertFalse(options["restore_state"])
            barrier.wait(timeout=5)
            submitted.set()
            options["on_submitted"]()
            self.assertTrue(region_sent.wait(timeout=5), "地区设置不应等待安装完成")
            installed.set()
            return {"success": True, "devices": {"EDGE1": {"status": "installed", "package_verified": True}}}

        def proxy(*args, **options):
            barrier.wait(timeout=5)
            self.assertFalse(options["start_stopped"])
            self.assertFalse(options["restore_state"])
            self.assertFalse(options["check_network"])
            self.assertFalse(options["verify_system"])
            self.assertFalse(options["sync_system"])
            return {"success": True, "proxy_attempted": True, "set_api": {"accepted": True}}

        def region(db_id, expected, report, **options):
            self.assertTrue(submitted.is_set())
            self.assertFalse(installed.is_set())
            self.assertFalse(options["verify_result"])
            report.update(settings_attempted=True, requests_accepted=True)
            region_sent.set()

        def restore(ids):
            self.assertTrue(installed.is_set(), "安装检查结束前不能停机")
            return {"success": True, "states": {"EDGE1": "stopped"}}

        self.installer.install_from_local.side_effect = install
        self.setup.run.side_effect = proxy
        self.setup.device.restore_stopped.side_effect = restore
        with patch.object(entry, "SystemSetup") as system:
            system.return_value.configure.side_effect = region
            self.assertEqual(self.run_entry(operation="initialize_existing"), 0, self.output.getvalue())
            system.return_value.reboot.assert_not_called()
        self.creator.create_batch.assert_not_called()
        self.setup.device.post_action.assert_called_once_with("run", ["EDGE1"])
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        report = json.loads(next((self.root / "日志").glob("initialize-*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertTrue(report["success"])
        target = report["devices"][0]
        self.assertTrue(target["proxy_requests_accepted"])
        self.assertTrue(target["system_requests_accepted"])
        self.assertFalse(target["proxy_configured"])
        self.assertFalse(target["system_configured"])
        self.assertFalse(target["network_check_requested"])
        self.assertTrue(Path(target["report"]).is_file())

    def test_existing_initialization_records_each_failure_continues_and_keeps_running_machine(self):
        self.prepare_concurrent_devices(3)
        self.creator._list_instances.return_value[1]["state"] = "running"
        self.setup.device.post_action.return_value = {"code": 200}
        self.setup.device.wait_ready.side_effect = lambda ids: ids

        def proxy(db_id, *args, **options):
            if db_id == "EDGE1":
                raise ValueError("proxy rejected")
            return {"success": True, "proxy_attempted": True, "set_api": {"accepted": True}}

        def install(source, ids, **options):
            if ids == ["EDGE2"]:
                raise ValueError("upload failed")
            return {"success": True, "devices": {ids[0]: {"status": "installed", "package_verified": True}}}

        def region(db_id, expected, report, **options):
            if db_id == "EDGE3":
                raise ValueError("region rejected")
            report.update(settings_attempted=True, requests_accepted=True)

        self.setup.run.side_effect = proxy
        self.installer.install_from_local.side_effect = install
        with patch.object(entry, "SystemSetup") as system:
            system.return_value.configure.side_effect = region
            self.assertEqual(self.run_entry(operation="initialize_existing"), 1, self.output.getvalue())
            self.assertEqual(system.return_value.configure.call_count, 3)
        self.assertEqual(self.setup.run.call_count, 3)
        self.assertEqual(self.installer.install_from_local.call_count, 3)
        self.assertEqual([c.args for c in self.setup.device.post_action.call_args_list],
                         [("run", ["EDGE1"]), ("run", ["EDGE3"])])
        self.assertEqual([c.args[0] for c in self.setup.device.restore_stopped.call_args_list], [["EDGE1"], ["EDGE3"]])
        summary = json.loads(next((self.root / "日志").glob("initialize-*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertEqual(summary["failed_names"], ["151.1", "151.2", "151.3"])
        self.assertEqual(summary["not_started"], [])
        self.assertEqual([list(d["errors"]) for d in summary["devices"]], [["proxy"], ["install"], ["region"]])
        self.assertTrue(all(d["state_restored"] for d in summary["devices"]))

    def test_existing_initialization_startup_failure_skips_requests_and_restores(self):
        self.prepare_concurrent_devices(1)
        self.setup.device.post_action.return_value = {"code": 200}
        self.setup.device.wait_ready.return_value = []
        with patch.object(entry, "SystemSetup") as system:
            self.assertEqual(self.run_entry(operation="initialize_existing"), 1)
            system.assert_not_called()
        self.setup.run.assert_not_called()
        self.installer.install_from_local.assert_not_called()
        self.setup.device.post_action.assert_called_once_with("run", ["EDGE1"])
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        summary = json.loads(next((self.root / "日志").glob("initialize-*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertIn("startup", summary["devices"][0]["errors"])
        self.assertFalse(summary["devices"][0]["installation_attempted"])

    def test_existing_initialization_uses_real_region_gps_requests_without_reboot(self):
        self.prepare_concurrent_devices(1)
        self.setup.host = "http://192.0.2.10:18182"
        self.creator._list_instances.return_value[0]["state"] = "running"
        self.setup.device.wait_ready.side_effect = lambda ids: ids
        self.setup.run.return_value = {"success": True, "proxy_attempted": True, "set_api": {"accepted": True}}
        self.proxy_document["proxies"][0]["region"].update(latitude=47.6062, longitude=-122.3321)
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        self.setup.session.post.return_value.json.return_value = {"code": 200}
        with patch.object(entry.SystemSetup, "request", return_value={}) as request, \
                patch.object(entry.SystemSetup, "snapshot", side_effect=AssertionError("地区不应回查")), \
                patch.object(entry.SystemSetup, "reboot", side_effect=AssertionError("不应重启")):
            self.assertEqual(self.run_entry(operation="initialize_existing"), 0, self.output.getvalue())
        self.assertEqual([c.args[0] for c in request.call_args_list],
                         ["country_set", "timezone_set", "language_set", "gps_inject"])
        self.assertEqual(request.call_args_list[-1].args[2], {"latitude": 47.6062, "longitude": -122.3321})
        self.assertFalse(self.setup.session.post.call_args.kwargs["json"]["isRestart"])
        self.setup.device.post_action.assert_not_called()
        self.setup.device.restore_stopped.assert_not_called()
        mapping = json.loads((self.root / "数据/device_mapping.json").read_text(encoding="utf-8"))
        self.assertTrue(mapping["devices"]["EDGE1"]["system_settings_submission"]["requests_accepted"])
        self.assertNotIn("system_config", mapping["devices"]["EDGE1"])

    def test_existing_initialization_workers_limit_machines_with_nested_parallel_requests(self):
        self.prepare_concurrent_devices(4)
        first_pair = threading.Barrier(2)
        guard = threading.Lock()
        active, peak = 0, 0
        setups = []

        def factory(host, **options):
            setup = Mock(mapping_path=options["mapping_path"], output_dir=options["output_dir"])
            setup.device.post_action.return_value = {"code": 200}
            setup.device.wait_ready.side_effect = lambda ids: ids
            def ready(db_id):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(active, peak)
                if db_id in ("EDGE1", "EDGE2"):
                    first_pair.wait(timeout=5)
            def restore(ids):
                nonlocal active
                with guard:
                    active -= 1
                return {"success": True, "states": {ids[0]: "stopped"}}
            setup.wait_android_ready.side_effect = ready
            setup.device.restore_stopped.side_effect = restore
            setup.run.return_value = {"success": True, "proxy_attempted": True, "set_api": {"accepted": True}}
            setups.append(setup)
            return setup
        self.setup_factory = factory
        with patch.object(entry, "SystemSetup") as system:
            system.return_value.configure.side_effect = lambda db, expected, report, **options: report.update(
                settings_attempted=True, requests_accepted=True)
            self.assertEqual(self.run_entry("--workers", "2", operation="initialize_existing"), 0, self.output.getvalue())
        self.assertEqual((active, peak), (0, 2))
        self.assertEqual(len(setups), 4)
        self.assertTrue(all(s.device.post_action.call_count == s.device.restore_stopped.call_count == 1 for s in setups))
        self.creator.create_batch.assert_not_called()

    def test_install_only_ignores_other_host_proxy_mapping_and_missing_proxy_config(self):
        self.prepare_concurrent_devices(2)
        self.proxy_path.unlink()
        self.config_path.with_name("batch10_bindings.example.json").unlink()
        self.main_config.pop("proxy")
        (self.root / "配置文件/config.json").write_text(json.dumps(self.main_config), encoding="utf-8")
        mapping_path = self.root / "数据/device_mapping.json"
        mapping_path.parent.mkdir(exist_ok=True)
        original_mapping = json.dumps({"host": "http://192.0.2.99:18182", "devices": {
            "EDGE1": {"name": "other-machine", "proxy_id": "other-proxy", "account_email": "other@example.com"}}})
        mapping_path.write_text(original_mapping, encoding="utf-8")

        self.assertEqual(self.run_entry("--names", "151.1", "151.2", "--workers", "2",
                                        operation="install"), 0, self.output.getvalue())
        self.assertEqual(self.installer.install_from_local.call_count, 2)
        self.setup.run.assert_not_called()
        self.creator.create_batch.assert_not_called()
        self.assertEqual(mapping_path.read_text(encoding="utf-8"), original_mapping)
        job = next((self.root / "日志").glob("install-*"))
        task_mapping = json.loads((job / "device_mapping.json").read_text(encoding="utf-8"))
        self.assertEqual(task_mapping["host"], "http://192.0.2.10:18182")
        self.assertEqual(set(task_mapping["devices"]), {"EDGE1", "EDGE2"})
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["success"])
        self.assertTrue(all(d["tiktok_installed"] and d["state_restored"] for d in summary["devices"]))

    def test_region_only_starts_submits_and_restores_state_without_region_readback(self):
        self.prepare_concurrent_devices(2)
        self.creator._list_instances.return_value[1]["state"] = "running"
        self.proxy_document["proxies"][0]["region"].update(latitude=47.6062, longitude=-122.3321)
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        def configure(db, expected, report, previous=None, verify_result=True):
            report.update(settings_attempted=True, requests_accepted=True, expected=expected)
        system = Mock()
        system.configure.side_effect = configure
        self.setup.device.wait_ready.side_effect = lambda ids: ids
        self.setup.device.post_action.return_value = {"code": 200}
        with patch.object(entry, "SystemSetup", return_value=system), patch.object(entry, "check_proxy") as probe:
            self.assertEqual(self.run_entry("--names", "151.1", "151.2", operation="region"), 0)
        self.assertEqual([c.args[0] for c in system.configure.call_args_list], ["EDGE1", "EDGE2"])
        self.assertTrue(all(c.kwargs["verify_result"] is False for c in system.configure.call_args_list))
        self.setup.device.post_action.assert_called_once_with("run", ["EDGE1"])
        self.setup.device.wait_ready.assert_called_once_with(["EDGE1"])
        self.setup.wait_android_ready.assert_called_once_with("EDGE1")
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        self.assertEqual(self.setup.device.list_instances.call_count, 2)
        self.setup.run.assert_not_called()
        self.setup.apply.assert_not_called()
        self.installer.install_from_local.assert_not_called()
        self.apk_check.assert_not_called()
        probe.assert_not_called()
        mapping = json.loads((self.root / "数据/device_mapping.json").read_text(encoding="utf-8"))
        self.assertNotIn("system_config", mapping["devices"]["EDGE2"])
        self.assertEqual(mapping["devices"]["EDGE1"]["state"], "stopped")
        self.assertEqual(mapping["devices"]["EDGE2"]["state"], "running")
        summary = json.loads(next((self.root / "日志").glob("*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertTrue(all(d["success"] and not d["system_verification_requested"] for d in summary["devices"]))

    def test_region_failure_continues_dispatch_without_state_checks(self):
        self.prepare_concurrent_devices(2)
        self.setup.device.wait_ready.side_effect = lambda ids: ids
        self.setup.device.post_action.return_value = {"code": 200}
        self.proxy_document["proxies"][0]["region"].update(latitude=47.6062, longitude=-122.3321)
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        with patch.object(entry, "SystemSetup") as system:
            system.return_value.configure.side_effect = ValueError("请求失败")
            self.assertEqual(self.run_entry("--names", "151.1", "151.2", operation="region"), 1)
        self.assertEqual(system.return_value.configure.call_count, 2)
        self.assertEqual(self.setup.device.post_action.call_count, 2)
        self.assertEqual(self.setup.device.restore_stopped.call_count, 2)
        summary = json.loads(next((self.root / "日志").glob("*/batch_status.json")).read_text(encoding="utf-8"))
        self.assertEqual(summary["not_started"], [])
        self.assertEqual(len(summary["devices"]), 2)
        self.assertIn("请求失败", summary["devices"][0]["error"])

    def test_region_missing_gps_skips_geo_lookup_and_submits_region_only(self):
        self.prepare_concurrent_devices(1)
        self.creator._list_instances.return_value[0]["state"] = "running"
        self.main_config["proxy"].update(probe_url="https://api.ipify.org?format=json", timeout=20)
        (self.root / "配置文件/config.json").write_text(json.dumps(self.main_config), encoding="utf-8")
        with patch.object(entry, "SystemSetup") as system, patch.object(entry, "check_proxy") as probe:
            system.return_value.configure.side_effect = lambda db, expected, report, **kw: report.update(
                settings_attempted=True, requests_accepted=True)
            self.assertEqual(self.run_entry("--name", "151.1", operation="region"), 0, self.output.getvalue())
        expected = self.proxy_document["proxies"][0]["region"]
        self.assertNotIn("latitude", expected)
        self.assertNotIn("latitude", system.return_value.configure.call_args.args[1])
        system.return_value.configure.assert_called_once()
        system.return_value.resolve.assert_not_called()
        probe.assert_not_called()
        self.setup.cloud_json.assert_not_called()
        self.setup.run.assert_not_called()
        self.setup.apply.assert_not_called()
        self.setup.device.list_instances.assert_called_once()
        self.setup.device.restore_stopped.assert_not_called()

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

    def test_host_switch_backs_up_old_mapping_and_never_reuses_its_bindings(self):
        self.prepare_concurrent_devices(1)
        mapping_path = self.root / "数据/device_mapping.json"
        mapping_path.parent.mkdir(exist_ok=True)
        old_bytes = json.dumps({"host": "http://192.0.2.99:18182", "devices": {
            "EDGE1": {"name": "other-machine", "proxy_id": "old-proxy", "account_email": "old@example.com",
                      "system_config": {"success": True}},
            "OTHER": {"name": "old-host-only"}}}).encode("utf-8")
        mapping_path.write_bytes(old_bytes)

        self.assertEqual(self.run_entry("--name", "151.1", operation="proxy"), 0, self.output.getvalue())
        job = next((self.root / "日志").glob("proxy-*"))
        self.assertEqual((job / "previous_device_mapping.json").read_bytes(), old_bytes)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(mapping["host"], "http://192.0.2.10:18182")
        self.assertEqual(set(mapping["devices"]), {"EDGE1"})
        self.assertEqual(mapping["devices"]["EDGE1"]["name"], "151.1")
        self.assertIsNone(mapping["devices"]["EDGE1"]["proxy_id"])
        self.assertIsNone(mapping["devices"]["EDGE1"]["account_email"])
        self.assertNotIn("system_config", mapping["devices"]["EDGE1"])
        self.setup.run.assert_called_once()
        self.assertEqual(self.setup.run.call_args.args[1]["id"], "151")

    def test_invalid_mapping_is_not_replaced_or_used_for_proxy_requests(self):
        self.prepare_concurrent_devices(1)
        mapping_path = self.root / "数据/device_mapping.json"
        mapping_path.parent.mkdir(exist_ok=True)
        original = json.dumps({"host": "http://192.0.2.99:18182", "devices": []})
        mapping_path.write_text(original, encoding="utf-8")
        self.assertEqual(self.run_entry("--name", "151.1", operation="proxy"), 1)
        self.assertEqual(mapping_path.read_text(encoding="utf-8"), original)
        self.setup.run.assert_not_called()

    def test_missing_region_blocks_both_creation_and_configure_only(self):
        self.proxy_document["proxies"][0].pop("region")
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        for arguments in ((), ("--configure-only",)):
            if arguments:
                self.creator._list_instances.return_value = [
                    {"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
            self.assertEqual(self.run_entry(*arguments), 1)
            if arguments:
                self.assertGreaterEqual(self.creator._list_instances.call_count, 1)
            self.creator.create_batch.assert_not_called()
            self.setup.run.assert_not_called()
            self.assertIn("不能跳过地区初始化", self.output.getvalue())

    def test_missing_configuration_steps_cannot_succeed(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        for result in ({"success": False, "system": {"success": True}, "verification": {"verified": False}},
                       {"success": True, "proxy_configured": True, "verification": {"verified": True}},
                       {"success": True, "system": {"success": True}, "verification": {"verified": True}}):
            with self.subTest(result=result):
                self.setup.run.return_value = result
                self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
                self.assertTrue(self.setup.run.call_args.kwargs["sync_system"])
                latest = max((self.root / "日志").glob("*/batch_status.json"), key=lambda p: p.stat().st_mtime_ns)
                report = json.loads(latest.read_text(encoding="utf-8"))
                self.assertFalse(report["success"])
                self.assertTrue(report["devices"][0]["system_requested"])
                self.assertEqual(report["devices"][0]["system_configured"], result.get("system", {}).get("success") is True)
                self.assertEqual(report["devices"][0]["proxy_configured"], result.get("proxy_configured") is True)
                self.assertFalse(report["devices"][0]["network_check_requested"])
        self.creator.create_batch.assert_not_called()

    def test_missing_apk_blocks_creation_and_existing_device_initialization(self):
        self.apk_check.side_effect = ValueError("APK不存在或为空")
        for arguments in ((), ("--configure-only",)):
            self.assertEqual(self.run_entry(*arguments), 1)
            if arguments:
                self.assertGreaterEqual(self.creator._list_instances.call_count, 1)
            self.creator.create_batch.assert_not_called()
            self.setup.run.assert_not_called()
            self.installer.install_from_local.assert_not_called()

    def test_region_submission_and_package_complete_without_region_verification(self):
        self.creator._list_instances.return_value = [{"user_name": "151.1", "db_id": "EDGE1", "state": "stopped"}]
        self.setup.run.return_value = {"success": True, "proxy_configured": True, "proxy_attempted": True, "rom_ready": True,
                                       "system": {"success": False, "settings_attempted": True,
                                                  "steps": [{"action": "language_set", "accepted": False,
                                                             "error": "ReadTimeout"}]},
                                       "network_check_requested": False,
                                       "verification": {"scope": "proxy_configuration", "verified": True}}
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 0)
        self.installer.install_from_local.assert_called_once()
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        job = next((self.root / "日志").iterdir())
        report = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        device = report["devices"][0]
        self.assertTrue(report["success"])
        self.assertTrue(device["proxy_configured"])
        self.assertTrue(device["tiktok_installed"])
        self.assertTrue(device["system_attempted"])
        self.assertFalse(device["system_configured"])
        self.assertFalse(device["system_verification_requested"])
        self.assertFalse(device["network_verified"])
        self.assertFalse(device["network_check_requested"])
        self.assertNotIn("error", device)
        self.assertNotIn("联网验证", self.output.getvalue())
        self.assertIn("地区设置请求已结束（未核验）", self.output.getvalue())

    def test_real_region_flow_reaches_install_without_reboot_or_readback_after_language_code_zero(self):
        self.prepare_concurrent_devices(1)
        self.creator._list_instances.return_value[0]["state"] = "running"
        self.proxy_document["proxies"][0]["region"].update(latitude=34.0522, longitude=-118.2437)
        self.proxy_path.write_text(json.dumps(self.proxy_document), encoding="utf-8")
        real_proxy = entry.ProxySetup
        setups = []
        def factory(host, **options):
            setup = real_proxy(host, **options)
            setup.device = Mock()
            setup.device.list_instances.side_effect = lambda: {
                item["db_id"]: item for item in self.creator._list_instances.return_value}
            setup.device.wait_ready.return_value = ["EDGE1"]
            setup.wait_android_ready = Mock()
            setup.current_proxy = Mock(return_value={"engineType": 0, "matches": True, "enabled": True})
            setup.api = Mock(side_effect=lambda method, action, *args, **kw: (
                200, {"code": 0 if action == "language_set" else 200, "data": {}}))
            setup.control_api = Mock(return_value=(200, {"code": 200, "data": {}}))
            setup.session = Mock()
            setup.session.post.return_value.json.return_value = {"code": 200}
            setup.cloud_json = Mock(side_effect=AssertionError("unexpected network test"))
            setups.append(setup)
            return setup
        self.setup_factory = factory
        def install(source, ids, **options):
            setup = setups[0]
            self.assertEqual([c.args[1] for c in setup.api.call_args_list],
                             ["country_set", "language_set", "gps_inject"])
            self.assertEqual([c.args[1] for c in setup.control_api.call_args_list],
                             ["proxy/set_enabled", "proxy/set_enabled", "system/update_settings"])
            self.assertTrue(all(c.args[0] == "POST" for c in setup.control_api.call_args_list))
            setup.current_proxy.assert_not_called()
            setup.wait_android_ready.assert_not_called()
            setup.session.post.assert_called_once()
            setup.device.post_action.assert_not_called()
            return {"success": True, "devices": {"EDGE1": {"status": "installed", "package_verified": True}}}
        self.installer.install_from_local.side_effect = install
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 0, self.output.getvalue())
        self.installer.install_from_local.assert_called_once()
        report_path = next((self.root / "日志").glob("*/EDGE1/proxy_status.json"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["system"]["settings_attempted"])
        self.assertFalse(report["system"]["requests_accepted"])
        self.assertFalse(report["system"]["success"])
        self.assertFalse(report["system"]["verification_requested"])
        self.assertNotIn("reboot_verified", report["system"])
        self.assertNotIn("after", report["system"])
        mapping = json.loads(setups[0].mapping_path.read_text(encoding="utf-8"))["devices"]["EDGE1"]
        self.assertNotIn("system_config", mapping)
        self.assertFalse(mapping["system_settings_submission"]["requests_accepted"])

    def test_missing_gps_and_uncertain_proxy_writes_still_reach_local_install(self):
        self.prepare_concurrent_devices(1)
        self.creator._list_instances.return_value[0]["state"] = "running"
        real_proxy = entry.ProxySetup
        setups = []
        def factory(host, **options):
            setup = real_proxy(host, **options)
            setup.device = Mock()
            setup.device.list_instances.side_effect = lambda: {
                item["db_id"]: item for item in self.creator._list_instances.return_value}
            setup.device.wait_ready.return_value = ["EDGE1"]
            setup.current_proxy = Mock(side_effect=AssertionError("proxy readback"))
            setup.wait_android_ready = Mock(side_effect=AssertionError("Android property test"))
            setup.verify = Mock(side_effect=AssertionError("network test"))
            setup.api = Mock(return_value=(200, {"code": 0, "data": {}}))
            setup.control_api = Mock(side_effect=[entry.requests.ReadTimeout(), (200, {"code": 0}),
                                                 (200, {"code": 0})])
            setup.session = Mock()
            setups.append(setup)
            return setup
        self.setup_factory = factory
        with patch("setup_proxy.check_proxy", side_effect=AssertionError("exit probe")) as probe, \
                patch("setup_system.SystemSetup.resolve", side_effect=AssertionError("GPS geolocation")) as geo:
            self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 0, self.output.getvalue())
        probe.assert_not_called()
        geo.assert_not_called()
        self.installer.install_from_local.assert_called_once()
        self.assertEqual(self.installer.install_from_local.call_args.kwargs["wait_time"], 120)
        self.assertEqual(self.installer.install_from_local.call_args.kwargs["request_wait"], 120)
        report_path = next((self.root / "日志").glob("*/EDGE1/proxy_status.json"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["proxy_attempted"])
        self.assertFalse(report["proxy_configured"])
        self.assertFalse(report["verification"]["verified"])
        self.assertFalse(report["set_api"]["accepted"])
        self.assertIn("gps_skipped", report["system"])
        self.assertTrue(report["system"]["settings_attempted"])
        self.assertEqual([c.args[1] for c in setups[0].control_api.call_args_list],
                         ["proxy/set_enabled", "proxy/set_enabled", "system/update_settings"])
        self.assertEqual([c.args[1] for c in setups[0].api.call_args_list], ["country_set", "language_set"])
        setups[0].session.post.assert_not_called()
        mapping = json.loads(setups[0].mapping_path.read_text(encoding="utf-8"))["devices"]["EDGE1"]
        self.assertEqual(mapping["proxy_id"], "151")
        self.assertNotIn("proxy_configured_at", mapping)
        self.assertFalse(mapping["proxy_settings_submission"]["requests_accepted"])

    def test_configuration_failure_after_rom_ready_does_not_block_install(self):
        self.prepare_concurrent_devices(1)
        self.setup.run.return_value = {"success": False, "rom_ready": True, "error": "代理接口超时"}
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1"), 1)
        self.installer.install_from_local.assert_called_once()
        report_path = next((self.root / "日志").glob("*/batch_status.json"))
        device = json.loads(report_path.read_text(encoding="utf-8"))["devices"][0]
        self.assertTrue(device["tiktok_installed"])
        self.assertEqual(device["error"], "代理接口超时")

    def test_install_is_between_configuration_and_restore_and_pending_continues_batch(self):
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
        self.assertEqual([call[0] for call in calls.mock_calls], ["configure", "install", "restore"] * 2)
        self.assertEqual(self.setup.run.call_count, 2)
        job = next((self.root / "日志").iterdir())
        report = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["devices"]), 2)
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
            return {"success": True, "proxy_configured": True, "proxy_attempted": True, "rom_ready": True,
                    "system": {"success": False, "settings_attempted": True}, "verification": {"verified": True}}

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

    def test_parallel_configuration_failure_continues_dispatch_and_drains_active_tasks(self):
        self.prepare_concurrent_devices(4)
        barrier = threading.Barrier(2)
        failed_install = threading.Event()
        other_restored = threading.Event()

        def configure(db_id, *args, **kwargs):
            if db_id in ("EDGE1", "EDGE2"):
                barrier.wait(timeout=5)
            if db_id == "EDGE2":
                self.assertTrue(failed_install.wait(timeout=5))
            return {"success": db_id != "EDGE1", "proxy_configured": db_id != "EDGE1", "proxy_attempted": True, "rom_ready": True,
                    "system": {"success": False, "settings_attempted": True},
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
        self.assertEqual(self.setup.run.call_count, 4)
        self.assertEqual(self.installer.install_from_local.call_count, 4)
        self.assertEqual(self.setup.device.restore_stopped.call_count, 4)
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["success"])
        self.assertEqual(summary["not_started"], [])
        self.assertEqual(summary["failed_names"], ["151.1"])
        self.assertEqual(summary["success_count"], 3)
        self.assertEqual(summary["failed_count"], 1)
        self.assertFalse(summary["devices"][0]["network_verified"])
        self.assertTrue(summary["devices"][0]["tiktok_installed"])
        self.assertTrue(summary["devices"][1]["success"])
        self.assertTrue(all(item["state_restored"] for item in summary["devices"]))

    def test_retry_failed_waits_for_first_round_and_preserves_both_rounds(self):
        self.prepare_concurrent_devices(4)
        first_barrier = threading.Barrier(3)
        retry_barrier = threading.Barrier(2)
        lock = threading.Lock()
        calls, first_restored = [], set()
        active, peak = {1: 0, 2: 0}, {1: 0, 2: 0}

        def setup_factory(host, **options):
            setup = Mock()
            folder = options["output_dir"]
            setup.mapping_path = options["mapping_path"]
            setup.output_dir = folder
            attempt = 2 if "retry-1" in folder.parts else 1
            job = folder.parents[1] if attempt == 2 else folder.parent
            setup.device.list_instances.side_effect = lambda: {
                item["db_id"]: item for item in self.creator._list_instances.return_value}

            def configure(db_id, *args, **kwargs):
                if attempt == 2:
                    first = json.loads((job / "first_pass_status.json").read_text(encoding="utf-8"))
                    self.assertEqual(len(first["devices"]), 4)
                    self.assertTrue(all(item["state_restored"] for item in first["devices"]))
                    self.assertEqual(first_restored, {"EDGE1", "EDGE2", "EDGE3", "EDGE4"})
                    self.assertEqual((job / "first_failed_names.txt").read_text(encoding="utf-8"), "151.1\n151.2\n")
                with lock:
                    calls.append((attempt, db_id))
                    active[attempt] += 1
                    peak[attempt] = max(peak[attempt], active[attempt])
                try:
                    if attempt == 1 and db_id in ("EDGE1", "EDGE2", "EDGE3"):
                        first_barrier.wait(timeout=5)
                    elif attempt == 2:
                        retry_barrier.wait(timeout=5)
                    success = attempt == 2 or db_id not in ("EDGE1", "EDGE2")
                    result = {"success": success, "proxy_configured": success, "proxy_attempted": True, "rom_ready": True,
                              "system": {"success": False, "settings_attempted": True}, "verification": {"verified": success}}
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / "proxy_status.json").write_text(json.dumps(dict(result, attempt=attempt)), encoding="utf-8")
                    return result
                finally:
                    with lock:
                        active[attempt] -= 1

            def restore(ids):
                if attempt == 1:
                    with lock:
                        first_restored.add(ids[0])
                return {"success": True, "states": {ids[0]: "stopped"}}

            setup.run.side_effect = configure
            setup.device.restore_stopped.side_effect = restore
            return setup

        self.setup_factory = setup_factory
        self.assertEqual(self.run_entry("--configure-only", "--workers", "3", "--retry-failed"), 0, self.output.getvalue())
        self.creator.create_batch.assert_not_called()
        self.assertEqual(peak, {1: 3, 2: 2})
        self.assertCountEqual(calls, [(1, "EDGE1"), (1, "EDGE2"), (1, "EDGE3"), (1, "EDGE4"), (2, "EDGE1"), (2, "EDGE2")])
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["success"])
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["success_count"], 4)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["retried_names"], ["151.1", "151.2"])
        self.assertEqual(summary["retry_rounds"], 1)
        self.assertEqual(summary["first_failed_names"], ["151.1", "151.2"])
        self.assertEqual((job / "failed_names.txt").read_text(encoding="utf-8"), "")
        for record in summary["devices"]:
            attempt = 2 if record["name"] in summary["retried_names"] else 1
            self.assertEqual(record["attempt"], attempt)
            self.assertEqual(json.loads(Path(record["report"]).read_text(encoding="utf-8"))["attempt"], attempt)
            self.assertEqual(json.loads((job / record["db_id"] / "proxy_status.json").read_text(encoding="utf-8"))["attempt"], 1)

    def test_retry_failed_stops_after_one_round_and_keeps_final_failures(self):
        self.prepare_concurrent_devices(2)
        self.setup.run.return_value = {"success": False, "error": "代理探测未确认"}
        self.assertEqual(self.run_entry("--configure-only", "--workers", "3", "--retry-failed"), 1)
        self.assertEqual(self.setup.run.call_count, 4)
        self.assertEqual(self.setup.device.restore_stopped.call_count, 4)
        self.installer.install_from_local.assert_not_called()
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["retry_rounds"], 1)
        self.assertEqual(summary["failed_names"], ["151.1", "151.2"])
        self.assertEqual(summary["failed_count"], 2)
        self.assertTrue(all(record["attempt"] == 2 for record in summary["devices"]))
        self.assertEqual((job / "failed_names.txt").read_text(encoding="utf-8"), "151.1\n151.2\n")
        first = json.loads((job / "first_pass_status.json").read_text(encoding="utf-8"))
        self.assertTrue(all(record["attempt"] == 1 for record in first["devices"]))

    def test_retry_failed_does_not_repeat_successful_batch(self):
        self.prepare_concurrent_devices(2)
        self.assertEqual(self.run_entry("--configure-only", "--retry-failed"), 0)
        self.assertEqual(self.setup.run.call_count, 2)
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["retry_rounds"], 0)
        self.assertEqual(summary["retried_names"], [])
        self.assertEqual(summary["first_failed_names"], [])
        self.assertFalse((job / "retry-1").exists())

    def test_retry_failed_rejects_replaced_identity_without_operating_on_it(self):
        self.prepare_concurrent_devices(2)
        self.setup.run.return_value = {"success": False, "error": "代理探测未确认"}
        self.setup.device.restore_stopped.return_value = {"success": True, "states": {"EDGE1": "stopped"}}
        self.setup.device.list_instances.side_effect = lambda: {
            "EDGEREPLACED": {"user_name": "151.1", "db_id": "EDGEREPLACED", "state": "stopped"}}
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1", "--retry-failed"), 1)
        self.setup.run.assert_called_once()
        self.setup.device.restore_stopped.assert_called_once_with(["EDGE1"])
        self.assertGreater(self.setup.device.list_instances.call_count, 20)
        self.assertIn("等待窗口120秒", self.output.getvalue())
        self.installer.install_from_local.assert_not_called()
        self.assertIn("候补前原云机名称或ID", self.output.getvalue())

    def test_retry_failed_restores_first_round_original_state_after_cleanup_failure(self):
        self.prepare_concurrent_devices(2)
        success = {"success": True, "proxy_configured": True, "proxy_attempted": True, "rom_ready": True,
                   "system": {"success": False, "settings_attempted": True}, "verification": {"verified": True}}
        self.setup.run.side_effect = [dict(success, original_state="stopped", started_by_script=True),
                                     dict(success, original_state="running", started_by_script=False)]
        self.setup.device.restore_stopped.side_effect = [
            {"success": False, "states": {"EDGE1": "running"}, "error": "第一次停机超时"},
            {"success": True, "states": {"EDGE1": "stopped"}}]
        self.setup.device.list_instances.side_effect = lambda: {
            "EDGE1": {"db_id": "EDGE1", "user_name": "151.1", "state": "running"}}
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1", "--retry-failed"), 0, self.output.getvalue())
        self.assertEqual(self.setup.device.restore_stopped.call_count, 2)
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["devices"][0]["original_state"], "stopped")
        self.assertEqual(summary["devices"][0]["final_state"], "stopped")
        self.assertTrue(summary["devices"][0]["state_restored"])
        first = json.loads((job / "first_pass_status.json").read_text(encoding="utf-8"))
        self.assertIn("第一次停机超时", first["devices"][0]["error"])

    def test_retry_failed_retains_unresolved_identity_failure(self):
        self.prepare_concurrent_devices(2)
        self.creator._list_instances.return_value.pop()
        self.assertEqual(self.run_entry("--configure-only", "--retry-failed"), 0)
        self.setup.run.assert_called_once()
        job = next((self.root / "日志").iterdir())
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["first_failed_names"], [])
        self.assertEqual(summary["failed_names"], [])
        self.assertEqual(summary["retry_skipped_names"], [])
        self.assertEqual(summary["retry_rounds"], 0)

    def test_retry_failed_handles_session_setup_exception_as_a_device_failure(self):
        self.prepare_concurrent_devices(2)

        def setup_factory(host, **options):
            if "retry-1" not in options["output_dir"].parts:
                raise ValueError("建立会话失败")
            self.setup.mapping_path = options["mapping_path"]
            self.setup.output_dir = options["output_dir"]
            return self.setup

        self.setup_factory = setup_factory
        self.assertEqual(self.run_entry("--configure-only", "--name", "151.1", "--retry-failed"), 0, self.output.getvalue())
        self.setup.run.assert_called_once()
        job = next((self.root / "日志").iterdir())
        first = json.loads((job / "first_pass_status.json").read_text(encoding="utf-8"))
        self.assertEqual(first["failed_names"], ["151.1"])
        self.assertEqual(first["devices"][0]["error"], "建立会话失败")
        summary = json.loads((job / "batch_status.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["success"])
        self.assertEqual(summary["failed_names"], [])

    def test_retry_failed_requires_configure_only_before_any_network_operation(self):
        for arguments in (["--retry-failed"], ["--create-only", "--retry-failed"]):
            self.assertEqual(self.run_entry(*arguments), 1)
            self.assertIn("仅支持", self.output.getvalue())
        self.creator._list_instances.assert_not_called()
        self.creator.create_batch.assert_not_called()
        self.setup.run.assert_not_called()
        self.apk_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
