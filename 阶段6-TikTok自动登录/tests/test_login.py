"""离线验证新旧验证码、登录证据、指定设备边界、失败状态和数据保留。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_login import PendingLogin, TikTokLogin, find_label, page_kind, profile_evidence
from control_api import ControlAPI
from email_verifier import EmailVerifier

DB = "EDGE123456"
EMAIL = "test@example.com"
IP = "198.51.100.1"
OLD, NEW = "102345", "654321"
CONFIG = {"vmos": {"host_ip": "host", "port": 18182}, "proxy": {"config_file": "proxy.json", "probe_url": "https://example.com", "timeout": 10},
          "tiktok": {"package_name": "com.zhiliaoapp.musically", "email_code": {"endpoint": "http://mail/code", "timeout": 10}}}


def tree(*items):
    root = ET.Element("hierarchy")
    for item in items:
        attributes = {"class": "android.widget.TextView", "enabled": "true", "bounds": "[10,20][100,80]"}
        attributes.update({"text": item} if isinstance(item, str) else item)
        ET.SubElement(root, "node", attributes)
    return root


PROFILE = tree("Edit profile", "@test.user")
CODE_PAGE = tree("Enter 6-digit code", {"class": "android.widget.EditText", "text": ""})


class LoginTests(unittest.TestCase):
    def test_known_info_overlay_is_dismissed_once_but_never_from_code_page(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        overlay = tree({"content-desc": "@2131893000", "resource-id": "com.zhiliaoapp.musically:id/q7b"},
                       {"resource-id": "com.zhiliaoapp.musically:id/q7c"}, "Learn more")
        flow.read_page = Mock(side_effect=[overlay, PROFILE])
        with patch("auto_login.time.sleep"):
            self.assertEqual(flow.navigate_to_email()[1], profile_evidence(PROFILE))
        flow.ui.call.assert_called_once_with("input/keyevent", {"key_code": 4})
        flow.ui.reset_mock()
        flow.read_page = Mock(return_value=CODE_PAGE)
        with self.assertRaises(PendingLogin):
            flow.navigate_to_email()
        flow.ui.call.assert_not_called()

    def test_dump_retries_only_read_only_transient_ui_results(self):
        ui = ControlAPI("http://host", DB)
        ui.call = Mock(side_effect=[None, "", ET.tostring(PROFILE, encoding="unicode")])
        with patch("control_api.time.sleep"):
            self.assertEqual(profile_evidence(ui.dump()), profile_evidence(PROFILE))
        self.assertTrue(all(c.args == ("accessibility/dump",) for c in ui.call.call_args_list))
        ui.call.side_effect = [None, None, None]
        with patch("control_api.time.sleep"):
            with self.assertRaises(ValueError):
                ui.dump()

    def test_avatar_intro_back_is_limited_to_one_and_never_edits(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        overlay = tree("Your avatar, your style", "Create your TikTok avatar and bring it to chats, Stories, and more.", "Get started")
        with patch("auto_login.time.sleep"):
            self.assertTrue(flow.dismiss_info_prompt(overlay))
            self.assertFalse(flow.dismiss_info_prompt(overlay))
        flow.ui.call.assert_called_once_with("input/keyevent", {"key_code": 4})
        flow.report = {}
        flow.ui.reset_mock()
        ET.SubElement(overlay, "node", {"class": "android.widget.EditText"})
        self.assertFalse(flow.dismiss_info_prompt(overlay))
        flow.ui.call.assert_not_called()

    def test_fill_waits_for_asynchronous_text_and_requires_the_target_field(self):
        ui = ControlAPI("http://host", DB)
        ui.call = Mock()
        target = tree({"class": "android.widget.EditText", "resource-id": "email", "text": ""})[0]
        pending = tree({"class": "android.widget.EditText", "resource-id": "email", "text": ""},
                       {"class": "android.widget.EditText", "resource-id": "other", "text": EMAIL})
        updated = tree({"class": "android.widget.EditText", "resource-id": "email", "text": EMAIL})
        ui.dump = Mock(side_effect=[pending, updated])
        with patch("control_api.time.sleep"):
            ui.fill(target, EMAIL)
        self.assertEqual(ui.dump.call_count, 2)
        ui.call.assert_called_once()
        ui.dump = Mock(return_value=pending)
        with patch("control_api.time.monotonic", side_effect=[0, 4]):
            with self.assertRaises(ValueError) as raised:
                ui.fill(target, EMAIL)
        self.assertNotIn(EMAIL, str(raised.exception))

    def test_password_fill_accepts_masked_readback_but_not_arbitrary_same_length_text(self):
        ui = ControlAPI("http://host", DB)
        ui.call = Mock()
        secret = "test-secret"
        target = tree({"class": "android.widget.EditText", "password": "true", "text": ""})[0]
        ui.dump = Mock(return_value=tree({"class": "android.widget.EditText", "password": "true", "text": "•" * len(secret)}))
        ui.fill(target, secret)
        ui.call.assert_called_once()
        ui.dump.return_value = tree({"class": "android.widget.EditText", "password": "true", "text": "x" * len(secret)})
        with patch("control_api.time.monotonic", side_effect=[0, 4]):
            with self.assertRaises(ValueError) as raised:
                ui.fill(target, secret)
        self.assertNotIn(secret, str(raised.exception))

    def test_plaintext_leading_zero_is_preserved_and_non_code_is_rejected(self):
        mail = EmailVerifier(CONFIG["tiktok"]["email_code"], EMAIL)
        mail.session = Mock()
        mail.session.get.return_value.status_code = 200
        mail.session.get.return_value.headers = {"Content-Type": "text/plain"}
        mail.session.get.return_value.text = "012345\n"
        self.assertEqual(mail.fetch(), "012345")
        mail.session.get.return_value.text = "no mail found"
        self.assertIsNone(mail.fetch())

    def test_mail_fetch_uses_username_and_does_not_expose_code_or_response(self):
        mail = EmailVerifier(CONFIG["tiktok"]["email_code"], EMAIL)
        mail.session = Mock()
        mail.session.get.return_value.status_code = 200
        mail.session.get.return_value.headers = {"Content-Type": "text/plain;charset=UTF-8"}
        mail.session.get.return_value.text = NEW
        mail.session.get.return_value.json.return_value = int(NEW)
        self.assertEqual(mail.fetch(), NEW)
        self.assertEqual(mail.session.get.call_args.kwargs["params"], {"username": EMAIL})
        mail.session.get.return_value.headers = {"Content-Type": "application/json"}
        mail.session.get.return_value.json.return_value = int(NEW)
        self.assertEqual(mail.fetch(), NEW)
        mail.session.get.return_value.json.side_effect = ValueError(NEW)
        with self.assertRaises(ValueError) as raised:
            mail.fetch()
        self.assertNotIn(NEW, str(raised.exception))
        mail.session.get.side_effect = requests.Timeout(NEW)
        with self.assertRaises(ValueError) as raised:
            mail.fetch()
        self.assertNotIn(NEW, str(raised.exception))

    def test_old_code_is_never_used_and_only_changed_code_is_accepted(self):
        mail = EmailVerifier(CONFIG["tiktok"]["email_code"], EMAIL)
        mail.fetch = Mock(return_value=OLD)
        with self.assertRaises(ValueError):
            mail.wait_new(OLD, timeout=0)
        mail.fetch.side_effect = [OLD, None, ValueError("unavailable"), NEW]
        with patch("email_verifier.time.sleep"):
            self.assertEqual(mail.wait_new(OLD, timeout=10, interval=0), NEW)

    def test_profile_evidence_rejects_homepage_and_other_profiles(self):
        self.assertEqual(profile_evidence(PROFILE), {"own_profile": True, "username": "@test.user"})
        modern_profile = tree("Edit", "Profile menu", "Private videos", "@test.user")
        self.assertEqual(profile_evidence(modern_profile), {"own_profile": True, "username": "@test.user"})
        for ui in (tree("Home", "For You", "Profile"), tree("Follow", "@someone"), tree("Edit profile"), tree("Log in to TikTok"),
                   tree("Edit", "@someone"), tree("Edit", "Private videos", "@someone")):
            self.assertIsNone(profile_evidence(ui))

    def test_captcha_errors_signup_and_code_page_are_separate(self):
        for message, status in (("Drag the puzzle piece into place", "captcha_required"),
                                ("Too many attempts. Try again later", "rate_limited"),
                                ("Incorrect code", "code_rejected"),
                                ("Please try again or log in with a different method.", "login_request_failed"),
                                ("When's your birthday?", "signup_required"), ("Enter 6-digit code", "code_entry")):
            self.assertEqual(page_kind(tree(message)), status)
        ui = tree({"text": "Continue", "enabled": "false"}, {"content-desc": "Continue", "clickable": "true"})
        self.assertEqual(find_label(ui, ["Continue"]).get("clickable"), "true")

    def test_app_load_failure_is_not_rate_limit_and_blocks_login_navigation(self):
        for failed_page in (tree("Something went wrong", "Try again later", "Retry", "Profile"),
                            tree("Your network is unstable. Tap to retry.", "Unstable network. Please try again.", "Retry", "Profile")):
            self.assertEqual(page_kind(failed_page), "app_unavailable")
            with tempfile.TemporaryDirectory() as temp:
                login = self.make_login(temp)
                login.ui = Mock()
                login.ui.dump.return_value = failed_page
                with self.assertRaises(PendingLogin) as raised:
                    TikTokLogin.navigate_to_email(login)
                self.assertEqual(raised.exception.status, "app_unavailable")
                login.ui.click.assert_not_called()
                login.enter_email.assert_not_called()
                login.send_code.assert_not_called()

    def make_login(self, temp, state="running"):
        path = Path(temp)
        mapping = path / "mapping.json"
        mapping.write_text(json.dumps({"host": "http://host:18182", "devices": {DB: {"account_email": None, "proxy_id": "p1"},
                           "EDGEOTHER": {"proxy_id": "keep", "account_email": "other@example.com"}}}), encoding="utf-8")
        (path / "proxy.json").write_text(json.dumps({"proxies": [{"id": "p1"}]}), encoding="utf-8")
        login = TikTokLogin(CONFIG, path, mapping)
        login.log = Mock()
        login.device = Mock()
        login.device.list_instances.return_value = {DB: {"state": state}}
        login.device.wait_ready.return_value = [DB]
        login.device.verifier.verify_installed.return_value = True
        login.device.post_action.return_value = {"code": 200}
        login.device.restore_stopped.return_value = {"success": True}
        login.proxy.verify = Mock(return_value={"verified": True, "exit_ip": IP})
        login.navigate_to_email = Mock(return_value=(tree({"class": "android.widget.EditText", "text": "Email or username"}), None))
        login.enter_email = Mock()
        login.send_code = Mock(return_value=(OLD, CODE_PAGE))
        login.input_code = Mock()
        login.verify_profile = Mock(return_value={"own_profile": True, "username": "@test.user"})
        return login

    def execute(self, login, **options):
        with (patch("auto_login.ROOT", login.output_dir), patch("auto_login.ControlAPI") as control,
              patch("auto_login.EmailVerifier") as mail, patch("auto_login.check_proxy", return_value={"exit_ip": IP}),
              patch("auto_login.time.sleep"), contextlib.redirect_stdout(io.StringIO())):
            control.return_value.dump.return_value = PROFILE
            mail.return_value.wait_new.return_value = NEW
            result = login.run(DB, EMAIL, **options)
            return result, control, mail

    def test_success_requires_new_code_and_profile_and_only_updates_target_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            report, control, mail = self.execute(login)
            self.assertTrue(report["success"])
            mail.return_value.wait_new.assert_called_once_with(OLD, 120, log=login.log)
            login.input_code.assert_called_once_with(NEW, CODE_PAGE)
            mapping = json.loads(login.mapping_path.read_text(encoding="utf-8"))
            self.assertEqual(mapping["devices"][DB]["account_email"], EMAIL)
            self.assertEqual(mapping["devices"][DB]["tiktok_username"], "@test.user")
            self.assertEqual(mapping["devices"]["EDGEOTHER"], {"proxy_id": "keep", "account_email": "other@example.com"})
            report_text = (Path(temp) / "login_status.json").read_text(encoding="utf-8")
            self.assertNotIn(NEW, report_text)
            self.assertNotIn(OLD, report_text)

    def test_proxy_failure_never_launches_or_sends_and_restores_original_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp, "stopped")
            login.proxy.verify.return_value = {"verified": False}
            report, control, mail = self.execute(login, start_stopped=True)
            self.assertFalse(report["success"])
            self.assertEqual(report["status"], "proxy_unavailable")
            control.return_value.call.assert_not_called()
            login.send_code.assert_not_called()
            login.device.post_action.assert_called_once_with("run", [DB])
            login.device.restore_stopped.assert_called_once_with([DB])

    def test_captcha_does_not_bind_account_and_input_code_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            login.verify_profile.side_effect = PendingLogin("captcha_required", "人工验证")
            report, _, _ = self.execute(login)
            self.assertFalse(report["success"])
            self.assertEqual(report["status"], "captcha_required")
            login.input_code.assert_called_once()
            self.assertIsNone(json.loads(login.mapping_path.read_text(encoding="utf-8"))["devices"][DB]["account_email"])

    def test_wrong_username_never_binds_the_requested_account(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            result, _, _ = self.execute(login, expected_username="different.user")
            self.assertEqual(result["status"], "account_mismatch")
            self.assertFalse(result["success"])
            self.assertIsNone(json.loads(login.mapping_path.read_text(encoding="utf-8"))["devices"][DB]["account_email"])

    def test_current_provider_code_requires_explicit_option_and_is_recorded_honestly(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            login.report = {}
            mail = Mock()
            mail.wait_new.side_effect = ValueError("timeout")
            mail.fetch.return_value = OLD
            with self.assertRaises(ValueError):
                login.receive_code(mail, OLD, 1)
            mail.fetch.assert_not_called()
            self.assertEqual(login.receive_code(mail, OLD, 1, allow_current=True), OLD)
            self.assertFalse(login.report["fresh_code_received"])
            self.assertTrue(login.report["provider_current_code_used"])
            self.assertFalse(login.report["provider_code_freshness_confirmed"])

    def test_code_is_never_typed_after_page_changes_while_waiting_for_mail(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            login.ui = Mock()
            login.ui.dump.return_value = tree("Email / Username", {"class": "android.widget.EditText", "text": EMAIL})
            login.report = {}
            with self.assertRaises(PendingLogin):
                TikTokLogin.input_code(login, NEW, CODE_PAGE)
            login.ui.text.assert_not_called()

    def test_password_run_routes_to_password_flow_and_region_failure_blocks_account_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            login.login_with_password = Mock()
            report, _, _ = self.execute(login, login_mode="password", password="test-password")
            self.assertTrue(report["success"])
            login.login_with_password.assert_called_once()
            login.send_code.assert_not_called()
            self.assertNotIn("test-password", (Path(temp) / "login_status.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            (Path(temp) / "proxy.json").write_text(json.dumps({"proxies": [{"id": "p1", "region": {"country": "US"}}]}), encoding="utf-8")
            with patch("auto_login.SystemSetup") as system:
                system.return_value.resolve.side_effect = ValueError("region mismatch")
                report, control, _ = self.execute(login)
            self.assertFalse(report["success"])
            self.assertEqual(report["status"], "system_unavailable")
            control.return_value.call.assert_not_called()
            login.enter_email.assert_not_called()

    def test_password_flow_uses_matching_email_once_and_verifies_own_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            login.report = {}
            login.ui = Mock()
            login.sensitive_input = False
            mail = Mock(email=EMAIL)
            mail.fetch.return_value = OLD
            mail.wait_new.return_value = NEW
            email_page = tree({"text": "Continue", "clickable": "true"})
            code_page = tree("Verify email", "Use the link or enter the code sent to " + EMAIL,
                             {"text": "Need help logging in?", "clickable": "true"})
            password_page = tree("Enter password", {"class": "android.widget.EditText", "password": "true", "text": ""},
                                 {"text": "Continue", "clickable": "true"})
            identity = tree("Verify it’s really you", {"text": "Email t***t@example.com", "clickable": "true"})
            login.ui.dump.side_effect = [email_page, code_page, tree("Use password instead"),
                                         password_page, password_page, identity, CODE_PAGE, CODE_PAGE]
            with patch("auto_login.time.sleep"):
                TikTokLogin.login_with_password(login, mail, "test-password", 30)
            self.assertTrue(login.report["password_submitted"])
            self.assertTrue(login.report["identity_email_requested"])
            self.assertTrue(login.report["fresh_code_received"])
            login.input_code.assert_called_once_with(NEW, CODE_PAGE)
            self.assertEqual(login.ui.click.call_count, 5)
            self.assertTrue(login.sensitive_input)

    def test_password_flow_refuses_wrong_identity_destination_and_wrong_password(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            login.report = {}
            login.ui = Mock()
            mail = Mock(email=EMAIL)
            email_page = tree("Continue")
            password_page = tree("Enter password", {"class": "android.widget.EditText", "password": "true"}, "Continue")
            for blocked_page, expected in ((tree("Verify it’s really you", "Email s***e@other.example"), "identity_unconfirmed"),
                                            (tree("Incorrect password"), "password_rejected")):
                login.ui.dump.side_effect = [email_page, password_page, password_page, blocked_page]
                with patch("auto_login.time.sleep"):
                    with self.assertRaises(PendingLogin) as raised:
                        TikTokLogin.login_with_password(login, mail, "test-password", 30)
                self.assertEqual(raised.exception.status, expected)
                login.input_code.assert_not_called()

    def test_switching_email_after_error_preserves_rate_limit_and_phone_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            login.navigate_to_email = TikTokLogin.navigate_to_email.__get__(login)
            login.ui = Mock()
            email_page = tree("Email / Username", "Please try again or log in with a different method.",
                              {"class": "android.widget.EditText", "text": "old@example.com"},
                              {"content-desc": "Back to previous screen", "clickable": "true"})
            clean_page = tree("Email / Username", {"class": "android.widget.EditText", "text": "Email or username"})
            login.ui.dump.side_effect = [email_page, clean_page]
            with patch("auto_login.time.sleep"):
                found, evidence = login.navigate_to_email()
            self.assertIs(found, clean_page)
            self.assertIsNone(evidence)
            self.assertEqual(login.ui.click.call_args.args[0].get("content-desc"), "Back to previous screen")
            login.ui.click.reset_mock()
            login.ui.dump.side_effect = None
            login.ui.dump.return_value = tree("Too many attempts", "Email / Username",
                                              {"class": "android.widget.EditText", "text": EMAIL})
            with self.assertRaises(PendingLogin) as raised:
                login.navigate_to_email()
            self.assertEqual(raised.exception.status, "rate_limited")
            phone_page = tree("Email / Username", {"class": "android.widget.EditText", "text": "Phone number"})
            login.ui.dump.return_value = phone_page
            with patch("auto_login.time.monotonic", side_effect=[0, 1, 61]), patch("auto_login.time.sleep"):
                with self.assertRaises(PendingLogin):
                    login.navigate_to_email()
            login.ui.click.assert_called_once()

    def test_existing_unbound_login_cannot_be_mistaken_for_specified_account(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            login.navigate_to_email.return_value = (PROFILE, profile_evidence(PROFILE))
            result, _, _ = self.execute(login)
            self.assertEqual(result["status"], "existing_account_unconfirmed")
            login.send_code.assert_not_called()
            with self.assertRaises(ValueError):
                login.run(DB + ",EDGEOTHER", EMAIL)

    def test_read_only_verification_preserves_login_report_mapping_and_device(self):
        with tempfile.TemporaryDirectory() as temp:
            login = self.make_login(temp)
            mapping = json.loads(login.mapping_path.read_text(encoding="utf-8"))
            mapping["devices"][DB].update(account_email=EMAIL, tiktok_username="@test.user")
            login.mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
            before = login.mapping_path.read_bytes()
            (Path(temp) / "login_status.json").write_text("old", encoding="utf-8")
            report, control, mail = self.execute(login, verify_only=True)
            self.assertTrue(report["success"])
            control.return_value.call.assert_not_called()
            mail.assert_not_called()
            login.device.post_action.assert_not_called()
            self.assertEqual(login.mapping_path.read_bytes(), before)
            self.assertEqual((Path(temp) / "login_status.json").read_text(encoding="utf-8"), "old")
            self.assertTrue((Path(temp) / "login_verify.json").exists())

    def test_sending_code_samples_baseline_before_send_and_submits_once(self):
        verify_email = tree("Verify email", "Use the link or enter the code sent to " + EMAIL,
                            "Resend code", {"class": "android.widget.EditText", "text": ""})
        verify_identity = tree("Verify identity", "Verify your identity by entering the code sent to t***t@example.com.",
                               "Resend code", {"class": "android.widget.EditText", "resource-id": "code-input", "text": ""})
        for code_page in (CODE_PAGE, verify_email, verify_identity):
            with self.subTest(page=code_page[0].get("text")), tempfile.TemporaryDirectory() as temp:
                login = self.make_login(temp)
                login.ui = Mock()
                login.report = {}
                mail = Mock()
                mail.fetch.return_value = OLD
                login.ui.dump.side_effect = [tree({"text": "Continue", "clickable": "true"}), code_page]
                with patch("auto_login.time.sleep"):
                    previous, ui = TikTokLogin.send_code(login, mail)
                self.assertEqual(previous, OLD)
                self.assertIs(ui, code_page)
                self.assertTrue(login.report["code_sent"])
                login.ui.click.assert_called_once()
                mail.fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
