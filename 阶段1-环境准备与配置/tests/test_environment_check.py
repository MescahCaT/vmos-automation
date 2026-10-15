"""不访问真实主机、不读取实际凭据的阶段1回归检查。"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_vmos_connection as env
import relay_proxy


class EnvironmentTests(unittest.TestCase):
    def test_relay_probe_uses_local_chain_and_does_not_return_credentials(self):
        proxy = {"id": "chain", "protocol": "socks5", "host": "198.51.100.1", "port": 1080,
                 "username": "private-user", "password": "private-pass",
                 "transit": {"protocol": "vless", "host": "192.0.2.1", "port": 443,
                             "uuid": "00000000-0000-4000-8000-000000000001", "tls": False}}
        local = {"id": "chain", "protocol": "socks5", "host": "127.0.0.1", "port": 12345}
        with patch.object(relay_proxy, "local_proxy") as context, patch.object(env.subprocess, "run") as run:
            context.return_value.__enter__.return_value = local
            run.return_value = Mock(returncode=0, stdout='{"ip":"198.51.100.2"}')
            result = env.check_proxy(proxy, {"probe_url": "https://example.com/ip", "timeout": 2})
            self.assertIn('socks5h://127.0.0.1:12345', run.call_args.kwargs["input"])
            self.assertNotIn("private-pass", run.call_args.kwargs["input"])
            self.assertEqual(result["host"], proxy["host"])
            self.assertEqual(result["transit"]["host"], proxy["transit"]["host"])
            self.assertNotIn("uuid", str(result))
            self.assertNotIn("private-pass", str(result))

    def test_relay_core_and_temporary_credentials_are_cleaned_on_probe_failure(self):
        proxy = {"id": "chain", "protocol": "socks5", "host": "198.51.100.1", "port": 1080,
                 "transit": {"protocol": "vless", "host": "192.0.2.1", "port": 443,
                             "uuid": "00000000-0000-4000-8000-000000000001", "tls": False}}
        with patch.object(relay_proxy, "CORE") as core, patch.object(relay_proxy.subprocess, "Popen") as popen, \
                patch.object(relay_proxy.socket, "create_connection"):
            core.is_file.return_value = True
            process = popen.return_value
            process.poll.return_value = None
            with self.assertRaisesRegex(ValueError, "probe failed"):
                with relay_proxy.local_proxy(proxy):
                    config_path = Path(popen.call_args.args[0][-1])
                    self.assertTrue(config_path.exists())
                    raise ValueError("probe failed")
            process.terminate.assert_called_once()
            process.wait.assert_called_once()
            self.assertFalse(config_path.exists())

    def test_vmos_response_and_health(self):
        session = Mock()
        response = session.get.return_value
        for data in ([], [{"repository": "image"}], {"list": [{"repository": "image"}]}):
            response.json.return_value = {"code": "200", "data": data}
            self.assertIsInstance(env.get_vmos_data(session, "http://host", env.API_PATHS["images"], 1), list)
        for payload in ({"code": 500, "data": []}, {"code": 200, "data": "invalid"}):
            response.json.return_value = payload
            with self.assertRaises(ValueError):
                env.get_vmos_data(session, "http://host", env.API_PATHS["images"], 1)
        response.json.return_value = {"code": 200, "data": {"docker_status": False, "http_status": True, "ping_status": True}}
        with self.assertRaises(ValueError):
            env.get_vmos_data(session, "http://host", env.API_PATHS["heartbeat"], 1)
        response.raise_for_status.side_effect = env.requests.HTTPError()
        with self.assertRaises(env.requests.HTTPError):
            env.get_vmos_data(session, "http://host", env.API_PATHS["images"], 1)

    def test_mail_code_and_no_raw_code_in_result(self):
        for payload in (123456, "012345", {"code": 200, "data": "012345"}):
            self.assertIsNotNone(env.extract_mail_code(payload))
        for payload in (True, 0, -1, "time 123456", "20261012", {"code": 200, "data": "no mail"}):
            self.assertIsNone(env.extract_mail_code(payload))
        with self.assertRaises(ValueError):
            env.extract_mail_code({"code": 500, "data": 123456})
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = 123456
        result = env.check_mail(session, {"endpoint": "http://host/mail", "timeout": 1}, "test@example.com")
        self.assertTrue(result["has_six_digit_code"])
        self.assertNotIn("123456", str(result))
        self.assertFalse(result["fresh_login_verified"])
        self.assertEqual(session.get.call_args.kwargs["params"], {"username": "test@example.com"})

    def test_authenticated_proxy_redaction_and_failure(self):
        proxy = {"id": "test", "protocol": "socks5", "host": "198.51.100.1", "port": 1080, "username": "user", "password": "secret"}
        config = {"probe_url": "https://example.com/ip", "timeout": 2}
        with patch.object(env.subprocess, "run", return_value=Mock(returncode=0, stdout='{"ip":"198.51.100.2"}')) as run:
            result = env.check_proxy(proxy, config)
            self.assertEqual(result["exit_ip"], "198.51.100.2")
            self.assertNotIn("secret", str(result))
            self.assertNotIn("secret", str(run.call_args.args))
            self.assertIn('proxy-user = "user:secret"', run.call_args.kwargs["input"])
            self.assertIn("socks5h://", run.call_args.kwargs["input"])
        with patch.object(env.subprocess, "run", return_value=Mock(returncode=97, stderr="secret")):
            with self.assertRaisesRegex(ValueError, "97") as exc:
                env.check_proxy(proxy, config)
            self.assertNotIn("secret", str(exc.exception))
        with patch.object(env.subprocess, "run", side_effect=[Mock(returncode=28), Mock(returncode=28), Mock(returncode=0, stdout='{"ip":"198.51.100.2"}')]) as run:
            self.assertEqual(env.check_proxy(proxy, config)["attempts"], 3)
            self.assertEqual(run.call_count, 3)
        with self.assertRaises(ValueError):
            env.curl_option("proxy-user", "user:secret\nurl = bad")

    def test_mapping_preserves_bindings_and_survives_bad_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mapping.json"
            env.save_json(path, {"host": "host", "devices": {"old": {"proxy_id": "p", "account_email": "test@example.com"}, "gone": {}}})
            env.update_mapping(path, [{"db_id": "old", "user_name": "existing", "state": "running"}], "host", "now")
            data = env.read_json(path)["devices"]
            self.assertEqual(data["old"]["proxy_id"], "p")
            self.assertEqual(data["old"]["account_email"], "test@example.com")
            self.assertTrue(data["old"]["present"])
            self.assertFalse(data["gone"]["present"])
            before = path.read_bytes()
            for instances, host in (([{}], "host"), ([{"db_id": "old"}, {"db_id": "old"}], "host"), ([], "other")):
                with self.assertRaises(ValueError):
                    env.update_mapping(path, instances, host, "later")
                self.assertEqual(path.read_bytes(), before)

    def test_apk_uses_basename_and_validates_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "test.apk").write_bytes(b"apk")
            config = {"apk_path": "test.apk", "aapt_path": "aapt.exe", "package_name": "com.zhiliaoapp.musically"}
            stdout = "package: name='com.zhiliaoapp.musically' versionCode='1' versionName='43.7.2'"
            with patch.object(env.subprocess, "run", return_value=Mock(returncode=0, stdout=stdout)) as run:
                self.assertEqual(env.check_apk(config, root)["version_name"], "43.7.2")
                self.assertEqual(run.call_args.args[0][-1], "test.apk")
                config["package_name"] = "wrong"
                with self.assertRaises(ValueError):
                    env.check_apk(config, root)
            config["apk_path"] = "missing.apk"
            with self.assertRaises(ValueError):
                env.check_apk(config, root)

    def test_failed_host_does_not_overwrite_mapping_or_report_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mapping = root / "数据" / "device_mapping.json"
            env.save_json(mapping, {"keep": "original"})
            config = {"vmos": {"host_ip": "host", "port": 1, "timeout": 1}, "device": {"count": 1, "image_repository": "image", "adiID": 1}}
            with patch.object(env, "get_vmos_data", side_effect=ValueError("failure")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(env.inspect_environment(config, root, True), 1)
            self.assertEqual(env.read_json(mapping), {"keep": "original"})
            self.assertEqual(env.read_json(root / "数据" / "environment_check.json")["status"], "failed")
            self.assertFalse(env.read_json(root / "数据" / "vmos_info.json")["complete"])

    def test_pending_and_missing_resources(self):
        data = {"heartbeat": {"docker_status": True}, "host_config": {}, "images": [{"repository": "image"}], "templates": [{"adiID": 1, "adiPass": "hidden"}], "instances": []}
        def get_data(_session, _host, path, _timeout):
            return next(value for key, value in data.items() if env.API_PATHS[key] == path)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {"vmos": {"host_ip": "host", "port": 1, "timeout": 1}, "device": {"count": 1, "image_repository": "image", "adiID": 1}, "proxy": {"config_file": "proxies.json"}, "tiktok": {"account_file": "accounts.json", "email_code": {}}}
            env.save_json(root / "proxies.json", {"proxies": []})
            env.save_json(root / "accounts.json", {"accounts": [{"email": "test@example.com", "login_mode": "email_code"}]})
            with patch.object(env, "get_vmos_data", side_effect=get_data), patch.object(env, "check_apk", return_value={}), patch.object(env, "check_mail", return_value={"has_six_digit_code": False}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(env.inspect_environment(config, root), 2)
                report = env.read_json(root / "数据" / "environment_check.json")
                self.assertEqual(report["status"], "pending")
                self.assertNotIn("hidden", str(report))
                self.assertNotIn("adiPass", str(env.read_json(root / "数据" / "vmos_info.json")))
                (root / "proxies.json").unlink()
                self.assertEqual(env.inspect_environment(config, root), 1)
                self.assertEqual(env.read_json(root / "数据" / "environment_check.json")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
