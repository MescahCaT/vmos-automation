"""离线检查安装响应、真实包列表、超时回查和指定设备边界。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apk_uploader import ApkUploader
from install_verifier import InstallVerifier, parse_app_list
from install_tiktok import TikTokInstaller, device_ids, parse_instances
from permission_manager import PermissionManager


DB = "EDGE123456"
PACKAGE = "com.zhiliaoapp.musically"


class InstallTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
