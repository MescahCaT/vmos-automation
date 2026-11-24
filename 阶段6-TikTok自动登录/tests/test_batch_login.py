"""离线覆盖批量账号分配、独立端口、密文续跑与一次性提交。"""
import contextlib
from datetime import datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_login as fixtures
from test_login import CONFIG, DB, EMAIL, OLD, NEW, PROFILE, CODE_PAGE, tree
from auto_login import TikTokLogin, PendingLogin, profile_evidence
from control_api import ControlAPI
from email_verifier import protected_session, EmailVerifier
from install_tiktok import parse_instances
from setup_proxy import ProxySetup

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("login_entry", ROOT / "登录TK.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class BatchLoginTests(unittest.TestCase):
    def account(self, suffix="1", name="157.1"):
        return {"email": "test" + suffix + "@example.com", "username": "test.user" + suffix,
                "login_mode": "email_code", "device_name": name}

    def test_explicit_binding_preserves_requested_order_and_refuses_ambiguity(self):
        a, b = self.account(), self.account("2", "157.2")
        self.assertEqual(entry.select_accounts([a, b], ["157.2", "157.1"]), [("157.2", b), ("157.1", a)])
        self.assertEqual(entry.select_accounts([a], ["157.1"], a["email"]), [("157.1", a)])
        for names, bindings in [([], None), (["157.1", "157.1"], None), (["unknown"], None),
                                (None, {"157.1": a["email"], "157.2": a["email"]})]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                entry.select_accounts([a, b], names, bindings=bindings)
        b["username"] = a["username"]
        with self.assertRaises(ValueError):
            entry.select_accounts([a, b])

    def test_dpapi_roundtrip_hides_baseline_and_rejects_tampering(self):
        state = {"previous": OLD, "email": EMAIL, "phase": "code_wait"}
        token = protected_session(state)
        self.assertNotIn(OLD, token)
        self.assertNotIn(EMAIL, token)
        self.assertEqual(protected_session(token=token), state)
        for invalid in ("not base64!", token[:-8] + "AAAAAAAA"):
            with self.assertRaises(ValueError) as error:
                protected_session(token=invalid)
            self.assertNotIn(OLD, str(error.exception))

    def test_ui_uses_unique_instance_ports_and_never_falls_back(self):
        proxy = ProxySetup("http://host:18182")
        proxy.device = Mock()
        proxy.device.list_instances.return_value = {DB: {"http_api_port": 35001}, "EDGEOTHER": {"http_api_port": 35002}}
        first, second = ControlAPI(proxy.host, DB, proxy), ControlAPI(proxy.host, "EDGEOTHER", proxy)
        self.assertEqual(first.base, "http://host:35001/api")
        self.assertEqual(second.base, "http://host:35002/api")
        for bad in (None, 0, "35001", 35002):
            proxy.control_bases.clear()
            proxy.device.list_instances.return_value[DB]["http_api_port"] = bad
            first.session = Mock()
            with self.assertRaises(ValueError):
                first.call("input/text", {"text": "private"})
            first.session.post.assert_not_called()

    def test_duplicate_instance_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_instances({"code": 200, "data": [{"db_id": DB}, {"db_id": DB}]})

    def test_write_timeout_only_reads_and_fifth_read_succeeds(self):
        ui = ControlAPI("http://host", DB)
        ui.call = Mock(side_effect=requests.Timeout())
        target = tree({"class": "android.widget.EditText", "resource-id": "email"})[0]
        pending = tree({"class": "android.widget.EditText", "resource-id": "email", "text": ""})
        success = tree({"class": "android.widget.EditText", "resource-id": "email", "text": EMAIL})
        ui.dump = Mock(side_effect=[pending] * 4 + [success])
        with patch("check_vmos_connection.time.sleep"):
            ui.fill(target, EMAIL)
        ui.call.assert_called_once()
        self.assertEqual(ui.dump.call_count, 5)

    def test_explicit_404_uses_native_input_once_but_timeout_never_falls_back(self):
        ui = ControlAPI("http://host", DB)
        node = tree({"class": "android.widget.EditText", "focused": "true", "text": "Email or username"})[0]
        empty = tree({"class": "android.widget.EditText", "focused": "true", "text": ""})
        filled = tree({"class": "android.widget.EditText", "focused": "true", "text": EMAIL, "bounds": "[10,20][90,80]"})
        response = requests.Response()
        response.status_code = 404
        ui.call = Mock(side_effect=[requests.HTTPError(response=response), {}, {}, {}, {}])
        ui.dump = Mock(side_effect=[tree(dict(node.attrib)), empty, filled])
        ui.fill(node, EMAIL)
        self.assertEqual([c.args[0] for c in ui.call.call_args_list], ["accessibility/node", "input/click", "input/keyevent", "input/keyevent", "input/text"])
        ui.call = Mock(side_effect=requests.Timeout())
        ui.native_fill = Mock()
        ui.dump = Mock(return_value=filled)
        ui.fill(node, EMAIL)
        ui.native_fill.assert_not_called()
        ui.call.assert_called_once()

    def test_encrypted_password_is_bound_to_email_and_env_takes_precedence(self):
        account = self.account()
        account["password_protected"] = protected_session({"email": account["email"], "password": "sample-password"})
        self.assertEqual(entry.account_password(account), "sample-password")
        with patch.dict("os.environ", {"TEST_TK_PASSWORD": "env-password"}):
            self.assertEqual(entry.account_password(account, "TEST_TK_PASSWORD"), "env-password")
        account["email"] = "another@example.com"
        with self.assertRaises(ValueError):
            entry.account_password(account)

    def test_native_input_timeout_reads_actual_field_without_repeating_text(self):
        ui = ControlAPI("http://host", DB)
        node = tree({"class": "android.widget.EditText", "focused": "true", "text": "Email or username"})[0]
        response = requests.Response()
        response.status_code = 404
        ui.call = Mock(side_effect=[requests.HTTPError(response=response), {}, {}, {}, requests.Timeout()])
        ui.dump = Mock(side_effect=[tree(dict(node.attrib)), tree({"class": "android.widget.EditText", "focused": "true", "text": ""}),
                                   tree({"class": "android.widget.EditText", "text": EMAIL})])
        ui.fill(node, EMAIL)
        self.assertEqual(sum(c.args[0] == "input/text" for c in ui.call.call_args_list), 1)

    def test_login_link_precedes_signup_and_birthday_returns_only_once(self):
        email_page = tree("Email / Username", {"class": "android.widget.EditText", "text": "Email or username"})
        choice = tree({"text": "Skip", "clickable": "true"}, {"text": "Use phone / email / username", "clickable": "true"},
                      {"text": "Already have an account? Log in", "clickable": "true"})
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        flow.ui.dump.side_effect = [tree("When’s your birthday?"), choice, email_page]
        with patch("auto_login.time.sleep"):
            flow.navigate_to_email()
        flow.ui.call.assert_called_once_with("input/keyevent", {"key_code": 4})
        self.assertEqual(flow.ui.click.call_args.args[0].get("text"), "Already have an account? Log in")
        flow.ui.reset_mock()
        flow.ui.dump.side_effect = None
        flow.ui.dump.return_value = tree("When’s your birthday?")
        with patch("auto_login.time.sleep"), self.assertRaises(PendingLogin):
            flow.navigate_to_email()
        flow.ui.call.assert_called_once_with("input/keyevent", {"key_code": 4})

    def test_login_method_precedes_skip_on_login_choice_page(self):
        email_page = tree("Email / Username", {"class": "android.widget.EditText", "text": "Email or username"})
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        flow.ui.dump.side_effect = [
            tree("Log in to TikTok", {"text": "Skip", "clickable": "true"},
                 {"text": "Use phone / email / username", "clickable": "true"}),
            email_page]
        with patch("auto_login.time.sleep"):
            actual, evidence = flow.navigate_to_email()
        self.assertIs(actual, email_page)
        self.assertIsNone(evidence)
        self.assertEqual(flow.ui.click.call_args.args[0].get("text"), "Use phone / email / username")
        flow.ui.click.assert_called_once()
        flow.ui.call.assert_not_called()

    def test_birthday_choice_waits_for_transition_without_back_cancelling_it(self):
        birthday = tree("When’s your birthdate?", "Get more great features? Sign up")
        signup = tree("Sign up for TikTok", "Already have an account? Log in")
        email_page = tree("Email / Username", {"class": "android.widget.EditText", "text": "Email or username"})
        for delayed in (True, False):
            with self.subTest(delayed=delayed):
                flow = TikTokLogin(CONFIG)
                flow.ui = Mock()
                flow.ui.dump.side_effect = [birthday] * 3 + [signup, email_page] if delayed else [birthday] * 6
                with patch("auto_login.time.sleep"):
                    if delayed:
                        self.assertIs(flow.navigate_to_email()[0], email_page)
                    else:
                        with self.assertRaises(PendingLogin):
                            flow.navigate_to_email()
                        self.assertEqual(flow.ui.click.call_count, 1)
                        self.assertEqual(flow.ui.dump.call_count, 6)
                flow.ui.call.assert_not_called()

    def test_pending_session_blocks_new_output_directory_and_foreground_is_preserved(self):
        helper = fixtures.LoginTests()
        with tempfile.TemporaryDirectory() as temp:
            flow = helper.make_login(temp)
            flow.report = {"db_id": DB, "account_email": EMAIL, "expected_username": "@test.user"}
            flow.session_path = Path(temp) / "login-locks" / ("login_session-" + DB + ".json")
            flow.checkpoint("password_pending", OLD)
            flow.output_dir = Path(temp) / "different-output"
            result, control, _ = helper.execute(flow)
            self.assertEqual(result["status"], "resume_required")
            control.return_value.call.assert_not_called()
        with tempfile.TemporaryDirectory() as temp:
            flow = helper.make_login(temp)
            with patch("auto_login.ROOT", Path(temp)), patch("auto_login.ControlAPI") as control, patch("auto_login.EmailVerifier") as mail, patch("setup_proxy.check_proxy", side_effect=AssertionError("登录不应探测代理")), patch("auto_login.time.sleep"):
                control.return_value.dump.return_value = tree("Edit profile", "@test.user", {"package": CONFIG["tiktok"]["package_name"]})
                mail.return_value.wait_new.return_value = NEW
                flow.run(DB, EMAIL, expected_username="test.user")
                self.assertFalse(any(c.args[0] == "activity/start" for c in control.return_value.call.call_args_list))

    def test_unbound_proxy_never_starts_or_submits_login(self):
        helper = fixtures.LoginTests()
        with tempfile.TemporaryDirectory() as temp:
            flow = helper.make_login(temp, "stopped")
            mapping = json.loads(flow.mapping_path.read_text())
            mapping["devices"][DB]["proxy_id"] = None
            flow.mapping_path.write_text(json.dumps(mapping))
            result, control, _ = helper.execute(flow, start_stopped=True)
            self.assertFalse(result["success"])
            flow.device.post_action.assert_not_called()
            control.return_value.call.assert_not_called()

    def test_birthdate_page_only_opens_account_choice_without_submitting_birthday(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        email_page = tree("Email / Username", {"class": "android.widget.EditText", "text": "Email or username"})
        flow.ui.dump.side_effect = [tree("When’s your birthdate?", "Get more great features? Sign up"),
                                   tree({"text": "Already have an account? Log in", "clickable": "true"}), email_page]
        with patch("auto_login.time.sleep"):
            flow.navigate_to_email()
        self.assertEqual([c.args[0].get("text") for c in flow.ui.click.call_args_list],
                         ["Get more great features? Sign up", "Already have an account? Log in"])
        flow.ui.text.assert_not_called()
        flow.ui.fill.assert_not_called()

    def test_resume_birthdate_recovers_login_entry_without_repeating_credentials(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        flow.session_state = {"phase": "password_pending"}
        birthday = tree("When’s your birthdate?", {"text": "Sign up", "clickable": "true"})
        signup = tree("Sign up for TikTok", {"text": "Sign in", "clickable": "true"}, "Use phone or email")
        login = tree("Log in to TikTok", {"text": "Use phone / email / username", "clickable": "true"})
        email = tree("Email / Username", {"class": "android.widget.EditText", "text": "Email or username"})
        flow.ui.dump.side_effect = [birthday, birthday, signup, login, email]
        mail = Mock()
        with patch("auto_login.time.sleep"), self.assertRaises(PendingLogin) as error:
            flow.resume_login(mail, 120, False)
        self.assertEqual(error.exception.status, "resume_restart_required")
        self.assertTrue(flow.report["login_entry_recovered"])
        self.assertEqual([c.args[0].get("text") for c in flow.ui.click.call_args_list],
                         ["Sign up", "Sign in", "Use phone / email / username"])
        mail.fetch.assert_not_called()
        mail.wait_new.assert_not_called()
        flow.ui.fill.assert_not_called()
        flow.ui.text.assert_not_called()

    def test_removed_device_account_binding_does_not_block_live_target(self):
        helper = fixtures.LoginTests()
        with tempfile.TemporaryDirectory() as temp:
            flow = helper.make_login(temp)
            mapping = json.loads(flow.mapping_path.read_text())
            mapping["devices"]["EDGEREMOVED"] = {"account_email": EMAIL, "present": True}
            flow.mapping_path.write_text(json.dumps(mapping))
            report, _, _ = helper.execute(flow)
            self.assertTrue(report["success"])
        with tempfile.TemporaryDirectory() as temp:
            flow = helper.make_login(temp)
            mapping = json.loads(flow.mapping_path.read_text())
            mapping["devices"]["EDGEOTHER"]["account_email"] = EMAIL
            flow.mapping_path.write_text(json.dumps(mapping))
            flow.device.list_instances.return_value["EDGEOTHER"] = {"state": "running"}
            report, control, _ = helper.execute(flow)
            self.assertFalse(report["success"])
            control.return_value.call.assert_not_called()

    def test_new_code_polling_never_exceeds_five_reads(self):
        mail = EmailVerifier(CONFIG["tiktok"]["email_code"], EMAIL)
        mail.fetch = Mock(return_value=OLD)
        with patch("check_vmos_connection.time.sleep"), self.assertRaises(ValueError):
            mail.wait_new(OLD, timeout=120)
        self.assertEqual(mail.fetch.call_count, 5)

    def test_fresh_email_flow_uses_real_navigation_send_and_input_once(self):
        with tempfile.TemporaryDirectory() as temp:
            flow = TikTokLogin(CONFIG, temp)
            flow.report = {"db_id": DB, "account_email": EMAIL, "expected_username": "@test.user"}
            flow.session_path = Path(temp) / "session.json"
            email_page = tree("Email / Username", {"class": "android.widget.EditText", "text": "Email or username"}, "Continue")
            flow.ui = Mock()
            flow.ui.dump.side_effect = [tree("Profile"), email_page, email_page, CODE_PAGE, CODE_PAGE, PROFILE, PROFILE]
            flow.ui.text.side_effect = requests.Timeout()
            mail = Mock()
            mail.fetch.return_value = OLD
            mail.wait_new.return_value = NEW
            with patch("auto_login.time.sleep"):
                page, evidence = flow.navigate_to_email()
                self.assertIsNone(evidence)
                flow.enter_email(EMAIL, page)
                previous, page = flow.send_code(mail)
                flow.input_code(flow.receive_code(mail, previous, 120), page)
                self.assertEqual(flow.verify_profile()["username"], "@test.user")
            flow.ui.fill.assert_called_once()
            flow.ui.text.assert_called_once_with(NEW)
            self.assertEqual(protected_session(token=json.loads(flow.session_path.read_text())["protected"])["phase"], "code_submitted")
            self.assertNotIn(NEW, (Path(temp) / "login_status.json").read_text())

    def test_email_submission_waits_for_fifth_page_without_resending(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        flow.report = {}
        waiting = tree("Log in")
        flow.ui.dump.side_effect = [tree("Continue")] + [waiting] * 4 + [CODE_PAGE]
        mail = Mock()
        mail.fetch.return_value = OLD
        with patch("auto_login.time.sleep") as sleep:
            previous, page = flow.send_code(mail)
        self.assertEqual(previous, OLD)
        self.assertIs(page, CODE_PAGE)
        flow.ui.click.assert_called_once()
        self.assertEqual(flow.ui.dump.call_count, 6)
        self.assertEqual(sum(c.args == (9.0,) for c in sleep.call_args_list), 4)

    def test_resume_waits_for_new_code_and_never_resends_or_retypes_submitted_code(self):
        for phase in ("code_wait", "code_submitted"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                flow = TikTokLogin(CONFIG, temp)
                flow.session_state = {"phase": phase, "previous": OLD}
                flow.read_page = Mock(return_value=CODE_PAGE)
                flow.input_code = Mock()
                flow.verify_profile = Mock(return_value=profile_evidence(PROFILE))
                mail = Mock()
                mail.wait_new.return_value = NEW
                self.assertEqual(flow.resume_login(mail, 120, False)["username"], "@test.user")
                self.assertEqual(flow.input_code.call_count, int(phase == "code_wait"))
                self.assertEqual(mail.wait_new.call_count, int(phase == "code_wait"))

    def test_resume_session_wrong_device_expired_or_missing_refuses_writes(self):
        helper = fixtures.LoginTests()
        for invalid in ("wrong_device", "expired", "missing"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                flow = helper.make_login(temp)
                flow.report = {"db_id": DB, "account_email": EMAIL, "expected_username": "@test.user"}
                flow.session_path = Path(temp) / ("login_session-" + DB + ".json")
                if invalid != "missing":
                    flow.checkpoint("code_wait", OLD)
                    state = flow.session_state
                    if invalid == "wrong_device":
                        state["db_id"] = "EDGEOTHER"
                    else:
                        state["updated_at"] = (datetime.now().astimezone() - timedelta(minutes=11)).isoformat()
                    flow.session_path.write_text(json.dumps({"protected": protected_session(state)}))
                result, control, mail = helper.execute(flow, resume=True)
                self.assertFalse(result["success"])
                self.assertEqual(result["status"], {"wrong_device": "resume_unconfirmed", "expired": "resume_expired", "missing": "resume_unavailable"}[invalid])
                flow.device.post_action.assert_not_called()
                flow.send_code.assert_not_called()
                control.return_value.call.assert_not_called()

    def test_same_device_process_lock_blocks_duplicate_login(self):
        import msvcrt
        with tempfile.TemporaryDirectory() as temp:
            flow = TikTokLogin(CONFIG, temp, Path(temp) / "mapping.json")
            lockdir = Path(temp) / "login-locks"
            lockdir.mkdir()
            with (lockdir / (DB + ".lock")).open("w+b") as handle:
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                flow._run = Mock()
                with self.assertRaises(ValueError):
                    flow.run(DB, EMAIL)
                flow._run.assert_not_called()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def test_password_result_waits_five_reads_without_resubmitting(self):
        flow = TikTokLogin(CONFIG)
        flow.report = {}
        flow.ui = Mock()
        mail = Mock(email=EMAIL)
        mail.fetch.return_value = OLD
        password_page = tree({"class": "android.widget.EditText", "password": "true"}, "Continue")
        flow.ui.dump.side_effect = [tree("Continue"), password_page, password_page] + [password_page] * 5
        with patch("auto_login.time.sleep") as sleep, self.assertRaises(PendingLogin) as error:
            flow.login_with_password(mail, "sample-password", 30)
        self.assertEqual(error.exception.status, "login_unconfirmed")
        self.assertEqual(flow.ui.click.call_count, 2)
        self.assertEqual(flow.ui.dump.call_count, 8)
        self.assertEqual(sum(c.args == (12.0,) for c in sleep.call_args_list), 4)
        self.assertTrue(flow.report["password_submitted"])

    def test_terms_prompt_resumes_without_password_or_code_submission(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        flow.report = {}
        flow.session_state = {"phase": "password_pending"}
        terms = tree("Dialog", "By tapping “Agree and continue”, you agree to our Terms of Service and acknowledge our Privacy Policy.",
                     {"text": "Agree and continue", "clickable": "true"})
        flow.ui.dump.side_effect = [terms, PROFILE]
        mail = Mock()
        with patch("auto_login.time.sleep"):
            evidence = flow.resume_login(mail, 120, False)
        self.assertEqual(evidence["username"], "@test.user")
        flow.ui.click.assert_called_once()
        mail.fetch.assert_not_called()
        mail.wait_new.assert_not_called()
        flow.ui.fill.assert_not_called()
        flow.ui.text.assert_not_called()
        self.assertFalse(flow.dismiss_info_prompt(terms))

    def test_email_code_can_resume_password_identity_verification_once(self):
        password_page = tree("Verify identity", {"class": "android.widget.EditText", "password": "true"}, "Next")
        identity = tree("Verify it’s really you", {"text": "Password", "clickable": "true"})
        for phase in ("code_submitted", "password_pending"):
            with self.subTest(phase=phase):
                flow = TikTokLogin(CONFIG)
                flow.ui = Mock()
                flow.session_state = {"phase": phase}
                flow.ui.dump.side_effect = [identity, identity, password_page, password_page] + [tree()] * 4 + [PROFILE]
                mail = Mock(email=EMAIL)
                mail.fetch.return_value = OLD
                with patch("auto_login.time.sleep"):
                    if phase == "code_submitted":
                        evidence = flow.resume_login(mail, 120, False, password="test-password")
                        self.assertEqual(evidence["username"], "@test.user")
                        flow.ui.fill.assert_called_once_with(password_page[1], "test-password")
                        self.assertEqual(flow.ui.click.call_count, 2)
                        self.assertTrue(flow.report["additional_password_verification"])
                        self.assertTrue(flow.report["password_submitted"])
                        mail.fetch.assert_called_once()
                    else:
                        with self.assertRaises(PendingLogin):
                            flow.resume_login(mail, 120, False, password="test-password")
                        flow.ui.fill.assert_not_called()
                        flow.ui.click.assert_not_called()
                        mail.fetch.assert_not_called()
                mail.wait_new.assert_not_called()
                flow.ui.text.assert_not_called()

    def test_password_challenge_without_supplied_password_does_not_click_or_type(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        flow.report = {"code_entered": True}
        flow.ui.dump.return_value = tree("Verify it’s really you", "Password")
        with self.assertRaises(PendingLogin) as error:
            flow.verify_profile(mail=Mock(email=EMAIL))
        self.assertEqual(error.exception.status, "password_required")
        flow.ui.fill.assert_not_called()
        flow.ui.click.assert_not_called()

    def test_password_submission_survives_code_checkpoint_and_prevents_resume_resubmission(self):
        with tempfile.TemporaryDirectory() as temp:
            flow = TikTokLogin(CONFIG)
            flow.ui = Mock()
            flow.ui.dump.return_value = tree("Verify it’s really you", "Password")
            flow.report = {"db_id": DB, "account_email": EMAIL, "expected_username": "@test.user", "password_submitted": True,
                           "additional_password_verification": True}
            flow.session_path = Path(temp) / "session.json"
            flow.checkpoint("code_submitted")
            saved = protected_session(token=json.loads(flow.session_path.read_text(encoding="utf-8"))["protected"])
            self.assertTrue(saved["password_submitted"])
            self.assertTrue(saved["additional_password_verification"])
            flow.report = {"db_id": DB, "account_email": EMAIL, "expected_username": "@test.user"}
            flow.session_state = saved
            flow.checkpoint("password_pending")
            saved = protected_session(token=json.loads(flow.session_path.read_text(encoding="utf-8"))["protected"])
            self.assertTrue(saved["additional_password_verification"])
            flow.report = {}
            with patch("auto_login.time.sleep"), self.assertRaises(PendingLogin):
                flow.resume_login(Mock(email=EMAIL), 120, False, password="test-password")
            flow.ui.click.assert_not_called()
            flow.ui.fill.assert_not_called()

    def test_notification_permission_after_password_can_resume_without_credentials(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        flow.session_state = {"phase": "password_pending", "password_submitted": True}
        package = "com.android.permissioncontroller"
        permission = tree({"text": "Allow TikTok to send you notifications?", "package": package},
                          {"text": "DON’T ALLOW", "package": package, "clickable": "true"})
        flow.ui.dump.side_effect = [permission, permission, PROFILE]
        mail = Mock()
        with patch("auto_login.time.sleep"):
            evidence = flow.resume_login(mail, 120, False, password="test-password")
        self.assertEqual(evidence["username"], "@test.user")
        self.assertEqual(flow.ui.click.call_args.args[0].get("text"), "DON’T ALLOW")
        flow.ui.click.assert_called_once()
        flow.ui.fill.assert_not_called()
        flow.ui.text.assert_not_called()
        mail.fetch.assert_not_called()
        mail.wait_new.assert_not_called()
        flow.ui.dump.side_effect = None
        permission[0].set("text", "Allow OtherApp to send you notifications?")
        flow.ui.dump.return_value = permission
        with self.assertRaises(PendingLogin):
            flow.read_page()

    def test_run_preserves_notification_prompt_for_resume_instead_of_relaunching_app(self):
        helper = fixtures.LoginTests()
        with tempfile.TemporaryDirectory() as temp:
            flow = helper.make_login(temp)
            flow.report = {"db_id": DB, "account_email": EMAIL, "expected_username": "@test.user", "password_submitted": True}
            flow.session_path = Path(temp) / "login-locks" / ("login_session-" + DB + ".json")
            flow.checkpoint("password_pending")
            flow.resume_login = Mock(return_value=profile_evidence(PROFILE))
            package = "com.android.permissioncontroller"
            permission = tree({"text": "Allow TikTok to send you notifications?", "package": package},
                              {"text": "DON’T ALLOW", "package": package})
            with patch("auto_login.ROOT", Path(temp)), patch("auto_login.ControlAPI") as control, patch("setup_proxy.check_proxy", side_effect=AssertionError("登录不应探测代理")), patch("auto_login.time.sleep"):
                control.return_value.dump.return_value = permission
                result = flow.run(DB, EMAIL, expected_username="test.user", resume=True)
            self.assertTrue(result["success"])
            flow.resume_login.assert_called_once()
            self.assertFalse(any(c.args[0] == "activity/start" for c in control.return_value.call.call_args_list))

    def test_start_watching_onboarding_resumes_to_profile_with_no_credential_writes(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        flow.session_state = {"phase": "password_pending", "password_submitted": True}
        intro = tree("Swipe up", "Videos are personalized for you based on what you watch, like, and share.", "Start watching")
        flow.ui.dump.side_effect = [intro, intro, PROFILE]
        mail = Mock()
        with patch("auto_login.time.sleep"):
            evidence = flow.resume_login(mail, 120, False)
        self.assertEqual(evidence["username"], "@test.user")
        self.assertEqual(flow.ui.click.call_args.args[0].get("text"), "Start watching")
        flow.ui.click.assert_called_once()
        self.assertFalse(flow.dismiss_info_prompt(intro))
        flow.ui.fill.assert_not_called()
        flow.ui.text.assert_not_called()
        mail.fetch.assert_not_called()
        mail.wait_new.assert_not_called()

    def test_password_login_also_handles_the_same_notification_prompt(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        password_page = tree({"class": "android.widget.EditText", "password": "true"}, "Continue")
        package = "com.android.permissioncontroller"
        permission = tree({"text": "Allow TikTok to send you notifications?", "package": package},
                          {"text": "DON’T ALLOW", "package": package, "clickable": "true"})
        flow.ui.dump.side_effect = [tree("Continue"), password_page, password_page, permission, PROFILE]
        mail = Mock(email=EMAIL)
        mail.fetch.return_value = OLD
        with patch("auto_login.time.sleep"):
            flow.login_with_password(mail, "test-password", 120)
        self.assertTrue(flow.report["password_submitted"])
        self.assertTrue(flow.report["notification_denied"])
        flow.ui.fill.assert_called_once()
        self.assertEqual(flow.ui.click.call_count, 3)
        mail.wait_new.assert_not_called()

    def test_selfie_avatar_introduction_is_backed_out_without_starting_capture(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        intro = tree("Create your TikTok avatar",
                     "This selfie you’ll take, and data about your face, will be used to create and improve your avatar. You can delete this data at any time.",
                     "Start")
        with patch("auto_login.time.sleep"):
            self.assertTrue(flow.dismiss_info_prompt(intro))
            self.assertFalse(flow.dismiss_info_prompt(intro))
        flow.ui.call.assert_called_once_with("input/keyevent", {"key_code": 4})
        flow.ui.click.assert_not_called()
        flow.ui.fill.assert_not_called()

    def test_other_app_and_ambiguous_profile_never_prove_login(self):
        flow = TikTokLogin(CONFIG)
        flow.ui = Mock()
        flow.ui.dump.return_value = tree({"package": "com.other.app", "text": "Continue"})
        with self.assertRaises(PendingLogin) as error:
            flow.read_page()
        self.assertEqual(error.exception.status, "app_not_foreground")
        self.assertIsNone(profile_evidence(tree("Edit profile", "@first", "@second")))

    def test_batch_concurrency_failure_continue_and_resume_success_skip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            mapping = path / "mapping.json"
            accounts = [self.account(), self.account("2", "157.2"), self.account("3", "158.1")]
            ids = [DB, "EDGEOTHER", "EDGETHIRD"]
            devices = {db: {"name": a["device_name"], "account_email": None} for db, a in zip(ids, accounts)}
            mapping.write_text(json.dumps({"host": "http://host:18182", "devices": devices}))
            proxy = Mock(mapping_path=mapping)
            proxy.device.list_instances.return_value = {db: {"user_name": a["device_name"]} for db, a in zip(ids, accounts)}
            seen = []
            barrier = threading.Barrier(2)
            def run(db_id, email, **kwargs):
                seen.append(db_id)
                if len(seen) <= 2:
                    barrier.wait(timeout=5)
                if db_id == DB:
                    raise requests.Timeout("private detail")
                return {"success": True, "status": "logged_in"}
            flow = Mock()
            flow.run.side_effect = run
            targets = entry.select_accounts(accounts)
            with patch.object(entry, "ProxySetup", return_value=proxy), patch.object(entry, "TikTokLogin", return_value=flow), contextlib.redirect_stdout(io.StringIO()):
                report = entry.execute(CONFIG, targets, path / "job", workers=2)
                self.assertEqual(report["success_count"], 2)
                self.assertEqual(report["failed_names"], ["157.1"])
                self.assertEqual(report["not_started"], [])
                self.assertEqual([r["name"] for r in report["devices"]], [a["device_name"] for a in accounts])
                self.assertNotIn("private detail", (path / "job/batch_status.json").read_text())
                for db, account in zip(ids[1:], accounts[1:]):
                    devices[db].update(account_email=account["email"], tiktok_username=account["username"])
                mapping.write_text(json.dumps({"host": "http://host:18182", "devices": devices}))
                flow.run.side_effect = lambda *a, **k: {"success": True, "status": "logged_in"}
                flow.run.reset_mock()
                resumed = entry.execute(CONFIG, targets, path / "job", resume=True)
                self.assertTrue(resumed["success"])
                self.assertEqual(flow.run.call_count, 1)
                self.assertTrue(all(r["resumed_skip"] for r in resumed["devices"][1:]))
                self.assertTrue(list((path / "job").glob("batch_status-before-resume-*.json")))


if __name__ == "__main__":
    unittest.main()
