"""离线检查安装响应、真实包列表、超时回查和指定设备边界。"""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apk_uploader import ApkUploader
from install_verifier import InstallVerifier, parse_app_list
from install_tiktok import INSTALL_LOCK, TikTokInstaller, device_ids, parse_instances
from permission_manager import PermissionManager
from check_vmos_connection import time_only_waits


DB = "EDGE123456"
PACKAGE = "com.zhiliaoapp.musically"


class InstallTests(unittest.TestCase):
    def test_submission_callback_precedes_package_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "app.apk"
            apk.write_bytes(b"apk")
            installer = self.make_installer(temp)
            installer.verifier.get_app_list.return_value = []
            installer.uploader.upload_local.return_value = {"http_status": 200, "code": 200}
            submitted = Mock()
            def verify(*args):
                submitted.assert_called_once_with()
                return {"success": [DB], "unconfirmed": []}
            installer.verifier.wait_for_install.side_effect = verify
            result = installer.install_from_local(apk, [DB], on_submitted=submitted)
            self.assertTrue(result["success"])
            installer.uploader.upload_local.assert_called_once()

    def test_initialization_rom_package_and_stop_waits_exceed_old_round_caps(self):
        elapsed = [0]
        def sleep(seconds):
            elapsed[0] += seconds
        installer = TikTokInstaller("http://host")
        installer.log = Mock()
        installer.session.get = Mock(side_effect=[Mock(json=Mock(return_value={"code": 0}))] * 24
                                     + [Mock(json=Mock(return_value={"code": 200}))])
        verifier = InstallVerifier("http://host")
        verifier.get_app_list = Mock(side_effect=[[]] * 24 + [[PACKAGE]])
        installer.list_instances = Mock(side_effect=[{DB: {"state": "running"}}] * 24
                                        + [{DB: {"state": "stopped"}}])
        installer.post_action = Mock(return_value={"code": 200})
        with patch("check_vmos_connection.time.monotonic", side_effect=lambda: elapsed[0]), \
                patch("check_vmos_connection.time.sleep", side_effect=sleep), time_only_waits():
            self.assertEqual(installer.wait_ready([DB]), [DB])
            self.assertEqual(verifier.wait_for_install([DB], log=Mock())["success"], [DB])
            self.assertTrue(installer.restore_stopped([DB])["success"])
            self.assertEqual(installer.session.get.call_count, 25)
            self.assertEqual(verifier.get_app_list.call_count, 25)
            self.assertEqual(installer.list_instances.call_count, 25)
            installer.post_action.assert_called_once_with("stop", [DB])
            elapsed[0] = 0
            verifier.get_app_list = Mock(return_value=[])
            self.assertEqual(verifier.wait_for_install([DB], log=Mock())["unconfirmed"], [DB])
            self.assertEqual(elapsed[0], 120)

    def test_initialization_upload_and_queue_wait_in_two_minute_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "app.apk"
            apk.write_bytes(b"apk")
            installer = self.make_installer(temp)
            installer.verifier.get_app_list.return_value = None
            installer.uploader.upload_local.return_value = {"code": 200, "success": True}
            installer.verifier.wait_for_install.return_value = {"success": [DB], "unconfirmed": []}
            self.assertTrue(installer.install_from_local(apk, [DB], wait_time=120, request_wait=120)["success"])
            installer.uploader.upload_local.assert_called_once_with(str(apk.resolve()), [DB], timeout=120)
            installer.uploader.upload_local.return_value = {"http_status": 200, "code": 201,
                                                          "msg": "当前有安装任务进行中，请稍后再试"}
            installer.uploader.upload_local.reset_mock()
            installer.uploader.upload_local.side_effect = [
                {"http_status": 200, "code": 201, "msg": "当前有安装任务进行中，请稍后再试"},
                {"http_status": 200, "code": 201, "msg": "当前有安装任务进行中，请稍后再试"},
                {"http_status": 200, "code": 200, "success": True},
            ]
            installer.verifier.wait_for_install.reset_mock()
            installer.verifier.wait_for_install.side_effect = [
                {"success": [], "unconfirmed": [DB]},
                {"success": [], "unconfirmed": [DB]},
                {"success": [DB], "unconfirmed": []},
            ]
            self.assertTrue(installer.install_from_local(apk, [DB], wait_time=120, request_wait=120)["success"])
            self.assertEqual(installer.uploader.upload_local.call_count, 3)
            self.assertEqual(installer.verifier.wait_for_install.call_args.args[1], 120)
            self.assertEqual(installer.verifier.wait_for_install.call_count, 3)
            with patch("install_tiktok.INSTALL_LOCK") as queue:
                queue.acquire.side_effect = [False, True]
                installer.uploader.upload_local.side_effect = None
                installer.uploader.upload_local.return_value = {"http_status": 200, "code": 200, "success": True}
                installer.verifier.wait_for_install.side_effect = None
                installer.verifier.wait_for_install.return_value = {"success": [DB], "unconfirmed": []}
                result = installer.install_from_local(apk, [DB], wait_time=120, request_wait=120)
                self.assertTrue(result["success"])
                self.assertEqual(queue.acquire.call_args_list, [call(timeout=120)] * 2)
                queue.release.assert_called_once()
            uploader = ApkUploader("http://host")
            with patch("apk_uploader.subprocess.run", return_value=Mock(returncode=0, stdout='{"code":200}\n200')) as run:
                uploader.upload_local(apk, [DB], timeout=120)
            self.assertEqual(run.call_args.kwargs["timeout"], 120)
            self.assertIn("120", run.call_args.args[0])

    def setUp(self):
        sleep = patch("install_tiktok.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_app_response_formats_and_business_failure(self):
        for data in ({"app": [PACKAGE]}, {"apps": [PACKAGE]}, {"apps": [{"package_name": PACKAGE}]}, [PACKAGE]):
            self.assertEqual(parse_app_list({"code": "200", "data": data}), [PACKAGE])
        self.assertEqual(parse_app_list({"code": 200, "data": {"app": []}}), [])
        for payload in ({"code": 201, "data": {"app": [PACKAGE]}}, {"code": 200, "data": {}}, {"code": 200, "data": {"app": [{}]}}):
            with self.assertRaises(ValueError):
                parse_app_list(payload)
        session = Mock()
        session.get.return_value.json.return_value = {"code": 201, "data": {}}
        self.assertEqual(InstallVerifier("http://host", session).batch_verify([DB])["unconfirmed"], [DB])

    def test_native_upload_normalizes_response_and_does_not_read_apk_into_memory(self):
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "app.apk"
            apk.write_bytes(b"apk")
            uploader = ApkUploader("http://host")
            with patch("apk_uploader.subprocess.run", return_value=Mock(returncode=0, stdout='{"code":200,"data":{"list":[]}}\n200')) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(uploader.upload_local(apk, [DB])["success"])
                args = run.call_args.args[0]
                self.assertIn("db_ids=" + DB, args)
                self.assertIn('file=@"app.apk"', args)
                self.assertEqual(run.call_args.kwargs["cwd"], apk.parent)
            for returncode, stdout in ((0, '{"code":500}\n200'), (0, '{"code":200}\n500'), (28, ""), (0, "bad response")):
                with patch("apk_uploader.subprocess.run", return_value=Mock(returncode=returncode, stdout=stdout)), contextlib.redirect_stdout(io.StringIO()):
                    self.assertFalse(uploader.upload_local(apk, [DB])["success"])
            with self.assertRaises(FileNotFoundError):
                uploader.upload_local(Path(temp) / "missing.apk", [DB])

    def test_url_and_permissions_use_vmos_code_and_app_field(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"code": "200"}
        self.assertTrue(ApkUploader("http://host", session).install_from_url("http://apk", [DB])["success"])
        permissions = PermissionManager("http://host", session)
        self.assertTrue(permissions.grant_overlay_permission(DB))
        self.assertEqual(session.post.call_args.kwargs["json"], {"db_id": DB, "app": PACKAGE})
        self.assertTrue(permissions.grant_root_permission(DB))
        self.assertEqual(session.post.call_args.kwargs["json"], {"db_ids": DB, "app": PACKAGE})
        session.post.return_value.json.return_value = {"code": 0, "success": True}
        self.assertFalse(permissions.grant_overlay_permission(DB))

    def test_polling_distinguishes_unconfirmed_from_absent(self):
        verifier = InstallVerifier("http://host")
        with patch.object(verifier, "get_app_list", return_value=None):
            result = verifier.wait_for_install([DB], timeout=0, log=lambda _: None)
            self.assertEqual(result["unconfirmed"], [DB])
            self.assertEqual(result["last_query"][DB], "unavailable")
        with patch.object(verifier, "get_app_list", return_value=[PACKAGE]):
            result = verifier.wait_for_install([DB], timeout=0, log=lambda _: None)
            self.assertEqual(result["success"], [DB])
            self.assertEqual(result["last_query"][DB], "package_present")

    def test_install_package_can_appear_on_fifth_read_and_absence_stops_at_five(self):
        verifier = InstallVerifier("http://host")
        verifier.get_app_list = Mock(side_effect=[None, [], [], [], [PACKAGE]])
        self.assertEqual(verifier.wait_for_install([DB], log=Mock())["success"], [DB])
        self.assertEqual(verifier.get_app_list.call_count, 5)
        verifier.get_app_list = Mock(return_value=[])
        self.assertEqual(verifier.wait_for_install([DB], log=Mock())["unconfirmed"], [DB])
        self.assertEqual(verifier.get_app_list.call_count, 5)

    def test_rom_wait_uses_twenty_reads_and_restore_stays_at_five_outside_initialization(self):
        installer = TikTokInstaller("http://host")
        installer.log = Mock()
        installer.session.get = Mock(return_value=Mock(json=Mock(return_value={"code": 0})))
        self.assertEqual(installer.wait_ready([DB]), [])
        self.assertEqual(installer.session.get.call_count, 20)
        installer.list_instances = Mock(return_value={DB: {"state": "running"}})
        installer.post_action = Mock(side_effect=requests.ReadTimeout())
        self.assertFalse(installer.restore_stopped([DB])["success"])
        self.assertEqual(installer.list_instances.call_count, 5)
        installer.post_action.assert_called_once_with("stop", [DB])

    def test_rom_can_become_ready_on_twentieth_read(self):
        installer = TikTokInstaller("http://host")
        installer.log = Mock()
        installer.session.get = Mock(side_effect=[Mock(json=Mock(return_value={"code": 0}))] * 19
                                     + [Mock(json=Mock(return_value={"code": 200}))])
        self.assertEqual(installer.wait_ready([DB]), [DB])
        self.assertEqual(installer.session.get.call_count, 20)

    def test_local_package_presence_completes_without_app_launch_or_network_test(self):
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "app.apk"
            apk.write_bytes(b"apk")
            installer = self.make_installer(temp)
            installer.verifier.get_app_list.return_value = [PACKAGE]
            installer.session.request = Mock(side_effect=AssertionError("unexpected app or network request"))
            result = installer.install_from_local(apk, [DB])
            self.assertTrue(result["success"])
            self.assertEqual(result["devices"][DB]["status"], "already_installed")
            self.assertTrue(result["devices"][DB]["package_verified"])
            installer.uploader.upload_local.assert_not_called()
            installer.verifier.wait_for_install.assert_not_called()
            installer.session.request.assert_not_called()

    def test_uncertain_start_is_confirmed_by_rom_without_resending(self):
        with tempfile.TemporaryDirectory() as temp:
            installer = self.make_installer(temp)
            installer.list_instances.return_value = {DB: {"state": "stopped"}}
            installer.post_action = Mock(return_value={"code": 0})
            installer.verifier.get_app_list.return_value = [PACKAGE]
            result = installer.install_from_url("http://apk", [DB], start_stopped=True)
            self.assertTrue(result["success"])
            installer.post_action.assert_called_once_with("run", [DB])
            installer.wait_ready.assert_called_once_with([DB])
            installer.uploader.install_from_url.assert_not_called()

    def test_only_explicit_valid_targets_and_instance_envelopes(self):
        self.assertEqual(device_ids({"db_ids": [DB, DB]}), [DB])
        self.assertEqual(device_ids([{"db_id": DB}]), [DB])
        for invalid in (None, [], "", "all", "EDGE123;bad", {"devices": [{}]}):
            with self.assertRaises(ValueError):
                device_ids(invalid)
        for data in ([{"db_id": DB}], {"list": [{"db_id": DB}]}):
            self.assertIn(DB, parse_instances({"code": 200, "data": data}))
        with self.assertRaises(ValueError):
            parse_instances({"code": 500, "data": []})

    def make_installer(self, output):
        installer = TikTokInstaller("http://host", output)
        installer.log = Mock()
        installer.uploader = Mock()
        installer.verifier = Mock()
        installer.permission_mgr = Mock()
        installer.list_instances = Mock(return_value={DB: {"db_id": DB, "state": "running", "user_name": "test"}})
        installer.wait_ready = Mock(side_effect=lambda ids: ids)
        return installer

    def test_upload_timeout_still_verified_and_never_resubmitted(self):
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "app.apk"
            apk.write_bytes(b"apk")
            installer = self.make_installer(temp)
            installer.verifier.get_app_list.return_value = []
            installer.verifier.wait_for_install.return_value = {"success": [DB], "unconfirmed": []}
            installer.uploader.upload_local.return_value = {"success": False, "unconfirmed": True}
            result = installer.install_from_local(apk, [DB], wait_time=0)
            self.assertTrue(result["success"])
            installer.uploader.upload_local.assert_called_once()
            self.assertEqual(result["devices"][DB]["status"], "installed")
            self.assertTrue(json.loads((Path(temp) / "install_status.json").read_text(encoding="utf-8"))["success"])

    def test_upload_timeout_error_is_preserved_when_package_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "app.apk"
            apk.write_bytes(b"apk")
            installer = self.make_installer(temp)
            installer.verifier.get_app_list.return_value = []
            installer.uploader.upload_local.return_value = {
                "success": False, "unconfirmed": True, "error": "TimeoutExpired"}
            installer.verifier.wait_for_install.return_value = {
                "success": [], "unconfirmed": [DB], "last_query": {DB: "package_absent"}}
            result = installer.install_from_local(apk, [DB], wait_time=0)
            self.assertFalse(result["success"])
            self.assertIn("TimeoutExpired", result["devices"][DB]["error"])
            installer.uploader.upload_local.assert_called_once()

    def test_local_and_url_installations_share_slot_until_package_verification_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "app.apk"
            apk.write_bytes(b"apk")
            first = self.make_installer(Path(temp) / "first")
            second = self.make_installer(Path(temp) / "second")
            other = "EDGE654321"
            second.list_instances.return_value = {other: {"state": "running"}}
            first.verifier.get_app_list.return_value = []
            first.uploader.upload_local.return_value = {"success": True}
            second_ready = threading.Event()
            first_verifying = threading.Event()
            second_uploaded = threading.Event()
            release_first = threading.Event()

            def verify(*args):
                first_verifying.set()
                if not release_first.wait(timeout=5):
                    raise ValueError("test did not release first installation")
                return {"success": [DB], "unconfirmed": []}

            def query(*args):
                second_ready.set()
                return []

            def upload(*args):
                second_uploaded.set()
                return {"success": True}

            first.verifier.wait_for_install.side_effect = verify
            second.verifier.get_app_list.side_effect = query
            second.uploader.install_from_url.side_effect = upload
            second.verifier.wait_for_install.return_value = {"success": [other], "unconfirmed": []}
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_result = pool.submit(first.install_from_local, apk, [DB])
                try:
                    self.assertTrue(first_verifying.wait(timeout=5))
                    second_result = pool.submit(second.install_from_url, "http://apk", [other])
                    self.assertTrue(second_ready.wait(timeout=5))
                    self.assertFalse(second_uploaded.wait(timeout=0.1), "another upload entered before package verification finished")
                finally:
                    release_first.set()
                self.assertTrue(first_result.result(timeout=5)["success"])
                self.assertTrue(second_result.result(timeout=5)["success"])
            self.assertTrue(second_uploaded.is_set())

    def test_busy_rejection_checks_once_without_waiting_or_resubmitting(self):
        with tempfile.TemporaryDirectory() as temp:
            installer = self.make_installer(temp)
            installer.verifier.get_app_list.return_value = []
            installer.uploader.install_from_url.return_value = {
                "http_status": 200, "code": 201, "msg": "当前有安装任务进行中，请稍后再试", "success": False}
            installer.verifier.wait_for_install.return_value = {
                "success": [], "unconfirmed": [DB], "last_query": {DB: "package_absent"}}
            result = installer.install_from_url("http://apk", [DB], wait_time=300)
            self.assertFalse(result["success"])
            self.assertTrue(result["batches"][0]["rejected_busy"])
            self.assertIn("业务码201", result["devices"][DB]["error"])
            self.assertEqual(installer.verifier.wait_for_install.call_args.args[1], 0)
            installer.uploader.install_from_url.assert_called_once()

    def test_initialization_busy_rejection_checks_package_before_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            installer = self.make_installer(temp)
            installer.verifier.get_app_list.return_value = []
            installer.verifier.wait_for_install.return_value = {"success": [DB], "unconfirmed": []}
            installer.uploader.install_from_url.return_value = {
                "http_status": 200, "code": 201, "msg": "当前有安装任务进行中，请稍后再试"}
            result = installer.install_from_url("http://apk", [DB], wait_time=120, request_wait=120)
            self.assertTrue(result["success"])
            self.assertEqual(result["devices"][DB]["status"], "installed")
            self.assertTrue(result["devices"][DB]["package_verified"])
            installer.uploader.install_from_url.assert_called_once()
            installer.verifier.wait_for_install.assert_called_once_with([DB], 120, PACKAGE, installer.log)

    def test_installation_slot_is_released_after_upload_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            installer = self.make_installer(temp)
            installer.verifier.get_app_list.return_value = []
            installer.uploader.install_from_url.side_effect = OSError("upload failure")
            result = installer.install_from_url("http://apk", [DB])
            self.assertFalse(result["success"])
            acquired = INSTALL_LOCK.acquire(blocking=False)
            try:
                self.assertTrue(acquired, "upload exception left the installation queue locked")
            finally:
                if acquired:
                    INSTALL_LOCK.release()

    def test_existing_package_skip_and_unavailable_query_skip(self):
        with tempfile.TemporaryDirectory() as temp:
            for apps, expected in (([PACKAGE], "already_installed"), (None, "unconfirmed")):
                installer = self.make_installer(temp)
                installer.verifier.get_app_list.return_value = apps
                result = installer.install_from_url("http://apk", [DB])
                self.assertEqual(result["devices"][DB]["status"], expected)
                installer.uploader.install_from_url.assert_not_called()

    def test_start_only_stopped_and_restore_only_started_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            installer = self.make_installer(temp)
            second = "EDGE654321"
            installer.list_instances.return_value = {DB: {"state": "stopped"}, second: {"state": "running"}}
            installer.verifier.get_app_list.return_value = [PACKAGE]
            installer.post_action = Mock(return_value={"code": 200})
            installer.restore_stopped = Mock(return_value={"success": True})
            result = installer.install_from_url("http://apk", [DB, second], start_stopped=True, restore_state=True, batch_size=2)
            self.assertTrue(result["success"])
            installer.post_action.assert_called_once_with("run", [DB])
            installer.restore_stopped.assert_called_once_with([DB])
            installer = self.make_installer(temp)
            installer.list_instances.return_value = {DB: {"state": "stopped"}}
            installer.post_action = Mock()
            result = installer.install_from_url("http://apk", [DB])
            self.assertEqual(result["install_failed"], [DB])
            installer.post_action.assert_not_called()

    def test_postupload_pending_is_reported_and_cleanup_failure_not_hidden(self):
        with tempfile.TemporaryDirectory() as temp:
            installer = self.make_installer(temp)
            installer.verifier.get_app_list.return_value = []
            installer.uploader.install_from_url.return_value = {"code": 200, "success": True}
            installer.verifier.wait_for_install.return_value = {"success": [], "unconfirmed": [DB]}
            installer.restore_stopped = Mock(return_value={"success": False})
            result = installer.install_from_url("http://apk", [DB], restore_state=True)
            self.assertFalse(result["success"])
            self.assertTrue(result["cleanup_failed"])
            self.assertEqual(result["install_pending"], [DB])
            self.assertEqual(result["install_failed"], [])

    def test_restore_waits_for_startup_and_transient_reads_before_one_stop(self):
        installer = TikTokInstaller("http://host")
        installer.log = Mock()
        installer.list_instances = Mock(side_effect=[requests.Timeout(), {DB: {"state": "starting"}},
            {DB: {"state": "running"}}, {DB: {"state": "stopping"}}, {DB: {"state": "stopped"}}])
        installer.post_action = Mock(return_value={"code": 200})
        with patch("install_tiktok.time.sleep"):
            result = installer.restore_stopped([DB])
        self.assertTrue(result["success"])
        self.assertEqual(result["states"], {DB: "stopped"})
        installer.post_action.assert_called_once_with("stop", [DB])

    def test_restore_uncertain_stop_is_only_polled_and_timeout_keeps_evidence(self):
        for response in (requests.Timeout(), {"code": 0}):
            installer = TikTokInstaller("http://host")
            installer.log = Mock()
            installer.list_instances = Mock(side_effect=[{DB: {"state": "running"}}, {DB: {"state": "stopped"}}])
            installer.post_action = Mock(side_effect=response if isinstance(response, Exception) else None,
                                         return_value=response)
            with patch("install_tiktok.time.sleep"):
                self.assertTrue(installer.restore_stopped([DB])["success"])
            installer.post_action.assert_called_once_with("stop", [DB])
        installer = TikTokInstaller("http://host")
        installer.log = Mock()
        installer.list_instances = Mock(return_value={DB: {"state": "starting"}})
        installer.post_action = Mock()
        result = installer.restore_stopped([DB], timeout=0)
        self.assertFalse(result["success"])
        self.assertEqual(result["states"], {DB: "starting"})
        self.assertEqual(result["unconfirmed"], [DB])
        installer.post_action.assert_not_called()
        installer.list_instances.return_value = {DB: {"state": "running"}}
        installer.post_action.return_value = {"code": 0}
        result = installer.restore_stopped([DB], timeout=0)
        self.assertFalse(result["success"])
        self.assertEqual(result["response_code"], 0)
        installer.post_action.assert_called_once_with("stop", [DB])


if __name__ == "__main__":
    unittest.main()
