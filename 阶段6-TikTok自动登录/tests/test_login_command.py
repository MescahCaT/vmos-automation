"""账号路径和用户指定的验证码后密码跳转终点；全部离线。"""
import contextlib
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_batch_login import entry
import test_login as fixtures
from test_login import CONFIG, DB, EMAIL, tree
from auto_login import TikTokLogin, PendingLogin
from email_verifier import protected_session


class LoginCommandTests(unittest.TestCase):
    def rows(self):
        return [["", "tk账号", "密码", "邮箱", "邮箱密码", "辅助验证邮箱/验证地址"],
                ["157.1", "test.user", " 001-test-secret ", EMAIL, "never-import-mail-password",
                 "http://mail/code?username=" + EMAIL]]

    def test_account_paths_support_spaces_json_and_xlsx_without_plaintext_output(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "用户 账号 表.xlsx"
            book = Workbook()
            for row in self.rows():
                book.active.append(row)
            book.save(path)
            book.close()
            accounts = entry.load_accounts(path, CONFIG)
            self.assertEqual(entry.select_accounts(accounts)[0][0], "157.1")
            secret = protected_session(token=accounts[0]["password_protected"])
            self.assertEqual(secret["password"], " 001-test-secret ")
            self.assertNotIn("001-test-secret", json.dumps(accounts))
            self.assertNotIn("never-import-mail-password", json.dumps(accounts))
            self.assertEqual(accounts[0]["login_mode"], "email_code")
            json_path = Path(temp) / "账号 路径.json"
            json_path.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
            self.assertEqual(entry.load_accounts(json_path, CONFIG), accounts)

    def test_xls_uses_same_validated_parser_and_releases_book(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "中文 账号.xls"
            path.touch()
            book = Mock()
            sheet = book.sheet_by_index.return_value
            sheet.nrows = 2
            sheet.row_values.side_effect = self.rows()
            with patch("xlrd.open_workbook", return_value=book):
                self.assertEqual(entry.load_accounts(path, CONFIG)[0]["device_name"], "157.1")
            book.release_resources.assert_called_once()

    def test_bad_account_paths_rows_and_provider_are_rejected_without_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "账号文件不存在"):
                entry.load_accounts(Path(temp) / "missing.xls", CONFIG)
        for mutation in ("bad_url", "numeric_password", "blank_name", "bad_header"):
            rows = self.rows()
            if mutation == "bad_url":
                rows[1][5] = "http://another-provider/code?username=" + EMAIL
            elif mutation == "numeric_password":
                rows[1][2] = 12345
            elif mutation == "blank_name":
                rows[1][0] = ""
            else:
                rows[0][1] = "unknown"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError) as error:
                entry.spreadsheet_accounts(rows, CONFIG)
            self.assertNotIn(EMAIL, str(error.exception))
            self.assertNotIn("001-test-secret", str(error.exception))

    def test_password_submission_completes_and_returns_from_tiktok_login_entry(self):
        package = CONFIG["tiktok"]["package_name"]
        login_entry = tree({"text": "Log in", "package": package},
                           {"text": "Email / Username", "package": package},
                           {"text": "Phone number", "package": package})
        home = tree({"text": "Home", "package": package})
        flow = TikTokLogin(CONFIG)
        flow.completion = "password-transition"
        flow.report = {"expected_username": "@test.user"}
        flow.session_state = {"password_submitted": True, "additional_password_verification": True,
                              "password_submission_confirmed": True}
        flow.ui = Mock()
        flow.ui.dump.side_effect = [login_entry, login_entry, home]
        with patch("auto_login.time.sleep"):
            evidence = flow.verify_profile()
        self.assertEqual(evidence, {"method": "email_code_and_password_submission", "username": "@test.user"})
        self.assertTrue(flow.report["password_completion_confirmed"])
        self.assertTrue(flow.report["email_code_verified_before_password"])
        self.assertEqual(flow.ui.call.call_count, 2)
        self.assertTrue(all(call.args == ("input/keyevent", {"key_code": 4}) for call in flow.ui.call.call_args_list))
        flow.ui.click.assert_not_called()
        flow.ui.fill.assert_not_called()

    def test_legacy_submitted_password_checkpoint_completes_only_on_login_entry_page(self):
        package = CONFIG["tiktok"]["package_name"]
        login_entry = tree({"text": "Log in", "package": package},
                           {"text": "Email / Username", "package": package},
                           {"text": "Phone number", "package": package})
        home = tree({"text": "Home", "package": package})
        flow = TikTokLogin(CONFIG)
        flow.completion = "password-transition"
        flow.report = {"expected_username": "@test.user"}
        flow.session_state = {"phase": "password_pending", "password_submitted": True,
                              "additional_password_verification": True}
        flow.ui = Mock()
        flow.ui.dump.side_effect = [login_entry, login_entry, home]
        with patch("auto_login.time.sleep"):
            evidence = flow.verify_profile()
        self.assertEqual(evidence["method"], "email_code_and_password_submission")
        self.assertTrue(flow.report["password_submission_inferred_from_login_entry"])

    def test_expired_submitted_password_checkpoint_can_resume_without_credential_writes(self):
        helper = fixtures.LoginTests()
        with tempfile.TemporaryDirectory() as temp:
            flow = helper.make_login(temp)
            checkpoint = Path(temp) / ("login_session-" + DB + ".json")
            state = {"host": flow.host, "db_id": DB, "email": EMAIL, "username": "@test.user",
                     "phase": "password_pending", "updated_at": (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(),
                     "password_submitted": True, "additional_password_verification": True, "previous": "old-code"}
            checkpoint.write_text(json.dumps({"protected": protected_session(state)}), encoding="utf-8")
            def complete_from_checkpoint(*args, **kwargs):
                flow.report.update(password_submission_confirmed=True, password_completion_confirmed=True,
                                   email_code_verified_before_password=True)
                return {"method": "email_code_and_password_submission", "username": "@test.user"}
            flow.verify_profile.side_effect = complete_from_checkpoint
            package = CONFIG["tiktok"]["package_name"]
            login_entry = tree({"text": "Log in", "package": package},
                               {"text": "Email / Username", "package": package},
                               {"text": "Phone number", "package": package})
            result, control, mail = helper.execute(flow, resume=True, completion="password-transition", ui_tree=login_entry)
            self.assertTrue(result["success"])
            self.assertTrue(result["expired_password_checkpoint_read_only"])
            self.assertTrue(result["password_completion_confirmed"])
            control.return_value.fill.assert_not_called()
            control.return_value.text.assert_not_called()
            control.return_value.click.assert_not_called()
            mail.return_value.wait_new.assert_not_called()
            flow.proxy.verify.assert_not_called()
            self.assertFalse(checkpoint.exists())

    def test_future_password_checkpoint_is_rejected_without_credential_writes(self):
        helper = fixtures.LoginTests()
        with tempfile.TemporaryDirectory() as temp:
            flow = helper.make_login(temp)
            checkpoint = Path(temp) / ("login_session-" + DB + ".json")
            state = {"host": flow.host, "db_id": DB, "email": EMAIL, "username": "@test.user",
                     "phase": "password_pending", "updated_at": (datetime.now().astimezone() + timedelta(minutes=30)).isoformat(),
                     "password_submitted": True, "additional_password_verification": True}
            checkpoint.write_text(json.dumps({"protected": protected_session(state)}), encoding="utf-8")
            result, control, mail = helper.execute(flow, resume=True, completion="password-transition")
            self.assertEqual(result["status"], "resume_expired")
            self.assertFalse(result["success"])
            control.return_value.fill.assert_not_called()
            control.return_value.click.assert_not_called()
            mail.return_value.wait_new.assert_not_called()

    def test_password_submission_click_timeout_is_not_counted_as_completed(self):
        flow = TikTokLogin(CONFIG)
        flow.report = {"db_id": DB, "account_email": EMAIL, "expected_username": "@test.user"}
        flow.ui = Mock()
        flow.ui.fill.return_value = None
        flow.ui.click.side_effect = ValueError("timeout")
        flow.read_page = Mock(return_value=tree("Continue"))
        flow.baseline = Mock(return_value="old-code")
        flow.checkpoint = Mock()
        with patch("auto_login.time.sleep"), self.assertRaises(PendingLogin) as error:
            flow.submit_password(tree({"class": "android.widget.EditText", "password": "true"}), Mock(), "secret")
        self.assertEqual(error.exception.status, "password_submit_unconfirmed")
        self.assertFalse(flow.report.get("password_submission_confirmed", False))
        flow.ui.click.assert_called_once()

    def test_task_completion_is_separate_from_identity_and_resumes_without_login(self):
        account = {"device_name": "157.1", "email": EMAIL, "username": "test.user", "login_mode": "password"}
        result = {"success": True, "status": "logged_in", "password_completion_confirmed": True,
                  "email_code_verified_before_password": True}
        with tempfile.TemporaryDirectory() as temp:
            mapping = Path(temp) / "mapping.json"
            mapping.write_text(json.dumps({"host": "http://host:18182", "devices": {DB: {"name": "157.1"}}}))
            proxy = Mock(mapping_path=mapping)
            proxy.device.list_instances.return_value = {DB: {"user_name": "157.1"}}
            flow = Mock()
            flow.run.return_value = result
            with patch.object(entry, "ProxySetup", return_value=proxy), patch.object(entry, "TikTokLogin", return_value=flow), contextlib.redirect_stdout(io.StringIO()):
                job = Path(temp) / "job"
                report = entry.execute(CONFIG, [("157.1", account)], job, completion="password-transition")
                self.assertTrue(report["task_completed"])
                self.assertTrue(report["success"])
                self.assertEqual(report["completed_count"], 1)
                self.assertEqual(report["success_count"], 1)
                self.assertEqual((job / "failed_names.txt").read_text(), "")
                self.assertEqual(flow.run.call_args.kwargs["login_mode"], "email_code")
                self.assertTrue(flow.run.call_args.kwargs["keep_running"])
                current = json.loads(mapping.read_text())
                current["devices"][DB].update(account_email=EMAIL, tiktok_username="@test.user")
                mapping.write_text(json.dumps(current))
                flow.run.reset_mock()
                resumed = entry.execute(CONFIG, [("157.1", account)], job, resume=True, completion="password-transition")
                self.assertTrue(resumed["task_completed"])
                flow.run.assert_not_called()
                with self.assertRaises(ValueError):
                    entry.execute(CONFIG, [("157.1", account)], job, resume=True, completion="profile")
        self.assertFalse(entry.task_completed(result, "profile"))
        self.assertFalse(entry.task_completed(dict(result, cleanup_failed=True), "password-transition"))

    def test_password_submission_is_successful_and_binds_the_selected_account(self):
        helper = fixtures.LoginTests()
        with tempfile.TemporaryDirectory() as temp:
            flow = helper.make_login(temp, "stopped")
            def confirmed(*args, **kwargs):
                flow.report.update(password_submission_confirmed=True, password_completion_confirmed=True,
                                   email_code_verified_before_password=True)
                return {"method": "email_code_and_password_submission", "username": "@test.user"}
            flow.verify_profile = Mock(side_effect=confirmed)
            result, _, _ = helper.execute(flow, start_stopped=True, completion="password-transition")
            self.assertTrue(result["success"])
            self.assertEqual(result["status"], "logged_in")
            self.assertTrue(result["password_completion_confirmed"])
            self.assertNotIn("error", result)
            self.assertTrue(result["mapping_updated"])
            flow.device.restore_stopped.assert_not_called()
            mapping = json.loads(flow.mapping_path.read_text())
            self.assertEqual(mapping["devices"][DB]["account_email"], EMAIL)
            self.assertEqual(mapping["devices"][DB]["tiktok_username"], "@test.user")

    def test_mixed_batch_continues_after_failure_and_never_counts_submission_only(self):
        accounts = [{"device_name": name, "email": "test" + str(i) + "@example.com",
                     "username": "test.user" + str(i), "login_mode": "email_code"}
                    for i, name in enumerate(("157.1", "157.2", "158.1"))]
        ids = [DB, "EDGESECOND", "EDGETHIRD"]
        results = [{"success": False, "status": "failed"},
                   {"success": False, "status": "password_transition_confirmed",
                    "password_transition_confirmed": True, "email_code_verified_before_password": True},
                   {"success": False, "status": "login_unconfirmed", "password_submitted": True}]
        with tempfile.TemporaryDirectory() as temp:
            mapping = Path(temp) / "mapping.json"
            mapping.write_text(json.dumps({"host": "http://host:18182",
                "devices": {db: {"name": a["device_name"]} for db, a in zip(ids, accounts)}}))
            proxy = Mock(mapping_path=mapping)
            proxy.device.list_instances.return_value = {db: {"user_name": a["device_name"]} for db, a in zip(ids, accounts)}
            flow = Mock()
            flow.run.side_effect = results
            with patch.object(entry, "ProxySetup", return_value=proxy), patch.object(entry, "TikTokLogin", return_value=flow), contextlib.redirect_stdout(io.StringIO()):
                report = entry.execute(CONFIG, entry.select_accounts(accounts), Path(temp) / "job", completion="password-transition")
            self.assertEqual(flow.run.call_count, 3)
            self.assertEqual(report["completed_count"], 1)
            self.assertEqual(report["success_count"], 0)
            self.assertEqual(report["failed_names"], ["157.1", "158.1"])
            self.assertFalse(report["task_completed"])
            self.assertEqual(report["not_started"], [])

    def test_unconfirmed_password_page_is_polled_at_most_five_without_retyping(self):
        flow = TikTokLogin(CONFIG)
        flow.completion = "password-transition"
        flow.report = {"password_submitted": True, "additional_password_verification": True}
        flow.ui = Mock()
        flow.ui.dump.return_value = tree({"class": "android.widget.EditText", "password": "true"}, "Enter password")
        with patch("auto_login.time.sleep"), self.assertRaises(PendingLogin) as error:
            flow.verify_profile()
        self.assertEqual(error.exception.status, "login_unconfirmed")
        self.assertEqual(flow.ui.dump.call_count, 5)
        flow.ui.fill.assert_not_called()
        flow.ui.click.assert_not_called()
        self.assertNotIn("password_transition_confirmed", flow.report)

    def test_cli_quoted_file_path_workers_and_names_are_forwarded_without_live_calls(self):
        account = {"device_name": "157.1", "email": EMAIL, "username": "test.user", "login_mode": "email_code"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "用户 引用路径.xls"
            job = Path(temp) / "日志"
            result = {"task_completed": True, "completed_count": 1, "total": 1, "devices": []}
            with patch.object(entry, "read_json", return_value=CONFIG), patch.object(entry, "load_accounts", return_value=[account]) as loader, patch.object(entry, "execute", return_value=result) as execute, contextlib.redirect_stdout(io.StringIO()):
                code = entry.main(["--accounts-file", str(path), "--name", "157.1", "--workers", "3", "--output-dir", str(job)])
            self.assertEqual(code, 0)
            loader.assert_called_once_with(path, CONFIG)
            args = execute.call_args.args
            self.assertEqual(args[3], 3)
            self.assertEqual(args[7], 240)
            self.assertEqual(args[-1], "password-transition")


if __name__ == "__main__":
    unittest.main()
