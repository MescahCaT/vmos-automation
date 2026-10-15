"""离线验证：明确单台、共享代理响应、超时回查、真实出口及状态恢复。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from setup_proxy import ProxySetup, legacy_proxy_body, probe_command, proxy_body, proxy_matches, status_fields
from relay_proxy import relay_node

DB = "EDGE123456"
OTHER = "EDGE654321"
HOST = "http://host"
IP = "198.51.100.42"
PROXY = {"id": "p1", "protocol": "socks5", "host": "198.51.100.1", "port": 1080,
         "username": "private-user", "password": "private-pass", "enabled": True}
CONFIG = {"probe_url": "https://api.ipify.org?format=json", "timeout": 10}
VERIFIED = {"verified": True, "exit_ip": IP, "verified_at": "2026-10-12T20:00:00+08:00"}
CHAIN = dict(PROXY, transit={"protocol": "vless", "host": "192.0.2.1", "port": 443,
                            "uuid": "00000000-0000-4000-8000-000000000001", "network": "tcp", "tls": False})


class ProxyTests(unittest.TestCase):
    def test_chain_readback_checks_order_relay_identity_and_exit_authentication(self):
        body = {"nodes": [relay_node(CHAIN), legacy_proxy_body(PROXY)]}
        normalized = json.loads(json.dumps(body))
        for node in normalized["nodes"]:
            node["proxyType"] = node.pop("proxyName")
        self.assertTrue(proxy_matches(normalized, CHAIN))
        without_default_tls = json.loads(json.dumps(normalized))
        without_default_tls["nodes"][0].pop("tls")
        self.assertTrue(proxy_matches(without_default_tls, CHAIN))
        self.assertFalse(proxy_matches(normalized, PROXY))
        self.assertFalse(proxy_matches({"nodes": normalized["nodes"][::-1]}, CHAIN))
        self.assertFalse(proxy_matches(legacy_proxy_body(PROXY), CHAIN))
        for index, field, value in ((0, "uuid", "different"), (0, "tls", True),
                                    (1, "password", "different")):
            changed = json.loads(json.dumps(normalized))
            changed["nodes"][index][field] = value
            self.assertFalse(proxy_matches(changed, CHAIN))

    def test_chain_uses_cbs_transit_and_never_accepts_only_matching_exit(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": 200}))
        self.assertTrue(setup.apply(DB, proxy_body(CHAIN), CHAIN, engine_type=1)["accepted"])
        self.assertEqual(setup.api.call_count, 1)
        self.assertEqual(setup.api.call_args.args[1], "cbs_proxy_set")
        sent = setup.api.call_args.args[3]
        self.assertEqual(sent["server"], CHAIN["host"])
        self.assertEqual(sent["transit"]["server"], CHAIN["transit"]["host"])
        native = legacy_proxy_body(CHAIN)
        self.assertTrue(proxy_matches(native, CHAIN))
        self.assertEqual(native["dnsServers"], ["223.5.5.5"])
        setup.api = Mock(return_value=(200, {"code": 200, "data": {
            "proxy_config": dict(legacy_proxy_body(PROXY), enabled=True)}}))
        setup.cloud_json = Mock(return_value={"ip": IP})
        result = setup.verify(DB, IP, CONFIG, 0, CHAIN)
        self.assertFalse(result["verified"])
        self.assertFalse(result["configuration_matches"])
        setup.cloud_json.assert_not_called()

    def test_root_web_check_requires_real_exit_marker_and_success_status(self):
        setup = ProxySetup(HOST)
        url = "https://www.tiktok.com/"
        setup.api = Mock(return_value=(200, {"code": 200, "data": {"message": "200\nCURL_EXIT=0\n"}}))
        self.assertEqual(setup.cloud_http(DB, url, 10, root=True), {"url": url, "http_status": 200})
        self.assertIn("curl_status=$?", setup.api.call_args.args[3]["cmd"])
        for output in ("200", "", "200\nCURL_EXIT=35", "403\nCURL_EXIT=0"):
            setup.api.return_value = (200, {"code": 200, "data": {"message": output}})
            with self.assertRaises(ValueError):
                setup.cloud_http(DB, url, 10, root=True)

    def test_standard_web_verification_labels_root_scope_and_context(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": 200, "data": {
            "engineType": 0, "proxy_config": {"enabled": True}}}))
        setup.cloud_json = Mock(return_value={"ip": IP})
        setup.cloud_http = Mock(return_value={"url": "https://www.tiktok.com/", "http_status": 200})
        result = setup.verify(DB, IP, dict(CONFIG, probe_urls=["https://www.tiktok.com/"], verify_rounds=2), 0)
        self.assertTrue(result["verified"])
        self.assertEqual(result["scope"], "proxy_exit_and_root_web")
        self.assertEqual(result["web_probe_context"], "root")
        self.assertFalse(result["tiktok_app_verified"])
        self.assertTrue(all(call.kwargs["root"] for call in setup.cloud_http.call_args_list))

    def make_setup(self, temp, state="running"):
        mapping = Path(temp) / "mapping.json"
        mapping.write_text(json.dumps({"host": HOST, "devices": {
            DB: {"proxy_id": None, "account_email": "test@example.com"},
            OTHER: {"proxy_id": "other", "state": "stopped"}}}), encoding="utf-8")
        setup = ProxySetup(HOST, temp, mapping)
        setup.log = Mock()
        setup.device = Mock()
        setup.device.list_instances.return_value = {DB: {"state": state, "user_name": "test"}}
        setup.device.wait_ready.return_value = [DB]
        setup.device.post_action.return_value = {"code": 200}
        setup.device.restore_stopped.return_value = {"success": True}
        setup.apply = Mock(return_value={"accepted": True})
        setup.current_proxy = Mock(return_value={"engineType": 0, "matches": False})
        setup.verify = Mock(return_value=dict(VERIFIED))
        setup.cloud_json = Mock(return_value={"success": True, "ip": IP, "country_code": "US"})
        return setup

    def execute(self, setup, **options):
        with patch("setup_proxy.check_proxy", return_value={"exit_ip": IP}), contextlib.redirect_stdout(io.StringIO()):
            return setup.run(DB, PROXY, CONFIG, wait_time=0, **options)

    def test_body_validation_and_status_do_not_expose_credentials(self):
        body = proxy_body(PROXY)
        self.assertEqual(body["extra"]["username"], PROXY["username"])
        self.assertTrue(body["udp"])
        self.assertFalse(body["udpDisabled"])
        safe = status_fields({"code": 0, "data": {"enabled": True, "healthy": True,
                               "proxy_config": body, "password": PROXY["password"]}})
        self.assertEqual(safe, {"code": 0, "enabled": True, "healthy": True})
        self.assertTrue(status_fields({"code": 200, "data": {"engineType": 1, "healthy": True,
            "proxy_config": {"enable": "true", "password": PROXY["password"]}}})["enabled"])
        self.assertFalse(status_fields({"code": 0, "data": {"enabled": False,
            "proxy_config": {"enable": "true"}}})["enabled"])
        for fields in ({"port": True}, {"port": 0}, {"host": "host; bad"}, {"password": "bad\n"}, {"protocol": "ftp"}, {"enabled": False}):
            with self.assertRaises(ValueError):
                proxy_body(dict(PROXY, **fields))
        cmd = probe_command(CONFIG["probe_url"], 10)
        self.assertIn("--noproxy '*'", cmd)
        self.assertNotIn("--proxy ", cmd)
        for url in ("file:///etc/passwd", "https://user:pass@host", "https://host\nwhoami"):
            with self.assertRaises(ValueError):
                probe_command(url, 10)

    def test_apply_accepts_zero_with_accepted_and_only_retries_explicit_conflict(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": 0, "data": {"accepted": True}}))
        self.assertTrue(setup.apply(DB, proxy_body(PROXY))["accepted"])
        for http, code, data in ((200, 0, {}), (500, 200, {"accepted": True}), (200, 200, {"accepted": False})):
            setup.api = Mock(return_value=(http, {"code": code, "data": data}))
            with self.assertRaises(ValueError):
                setup.apply(DB, proxy_body(PROXY))
            self.assertEqual(setup.api.call_count, 1)
        setup.log = Mock()
        setup.api = Mock(side_effect=[(409, {"code": 409, "msg": "legacy proxy in use"}),
                                      (200, {"code": 200}), (200, {"code": 0, "data": {"accepted": True}})])
        self.assertTrue(setup.apply(DB, proxy_body(PROXY))["accepted"])
        self.assertEqual([call.args[1] for call in setup.api.call_args_list], ["cbs_proxy_set", "proxy_stop", "cbs_proxy_set"])

    def test_legacy_engine_stops_then_sets_legacy_shape(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(side_effect=[(200, {"code": 200}), (200, {"code": 200})])
        result = setup.apply(DB, proxy_body(PROXY), PROXY, engine_type=1)
        self.assertTrue(result["accepted"])
        self.assertEqual([call.args[1] for call in setup.api.call_args_list], ["proxy_stop", "proxy_set"])
        self.assertEqual(setup.api.call_args_list[1].args[3], legacy_proxy_body(PROXY))

    def test_new_instance_without_old_proxy_can_bind_after_stop_reports_failure(self):
        for config in (None, {}, {"enabled": False}, {"enable": "false"}):
            with self.subTest(config=config):
                setup = ProxySetup(HOST)
                setup.log = Mock()
                setup.api = Mock(side_effect=[
                    (200, {"code": 500, "msg": "no active proxy"}),
                    (200, {"code": 200, "data": {"engineType": 0, "proxy_config": config}}),
                    (200, {"code": 200})])
                self.assertTrue(setup.apply(DB, proxy_body(CHAIN), CHAIN, engine_type=0)["accepted"])
                self.assertEqual([call.args[1] for call in setup.api.call_args_list], ["proxy_stop", "proxy_get", "proxy_set"])
                self.assertTrue(proxy_matches(setup.api.call_args.args[3], CHAIN))

    def test_stop_failure_requires_definite_inactive_readback_before_any_set(self):
        cases = [(200, {"code": 200, "data": {"proxy_config": {"enabled": True}}}),
                 (200, {"code": 200, "data": {}}),
                 (200, {"code": 0, "data": {"engineType": 0, "state": "stopped"}}),
                 (500, {"code": 200, "data": {"proxy_config": None}})]
        for response in cases:
            with self.subTest(response=response):
                setup = ProxySetup(HOST)
                setup.api = Mock(side_effect=[(500, {"code": 500}), response])
                with self.assertRaises(ValueError):
                    setup.apply(DB, proxy_body(PROXY), PROXY, engine_type=0)
                self.assertEqual([call.args[1] for call in setup.api.call_args_list], ["proxy_stop", "proxy_get"])

    def test_proxy_service_waits_past_rom_metadata_before_any_write(self):
        setup = ProxySetup(HOST)
        setup.log = Mock()
        pending = (200, {"code": 0, "data": {"engineType": 0, "state": "running"}})
        ready = (200, {"code": 200, "data": {"engineType": 0, "proxy_config": None}})
        setup.api = Mock(side_effect=[pending, ready])
        with patch("setup_proxy.time.sleep"):
            self.assertEqual(setup.current_proxy(DB, CHAIN), {"code": 200, "engineType": 0, "matches": False})
        self.assertEqual([call.args[:2] for call in setup.api.call_args_list], [("GET", "cbs_proxy_get")] * 2)
        setup.log.assert_called_once_with("等待云机代理服务就绪")
        setup.api = Mock(return_value=pending)
        with self.assertRaisesRegex(ValueError, "代理服务尚未就绪"):
            setup.current_proxy(DB, CHAIN, wait_time=0)
        self.assertEqual(setup.api.call_count, 1)

    def test_ready_cbs_state_and_legacy_conflict_are_still_supported(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": 0, "data": {
            "engineType": 2, "enabled": False, "healthy": False}}))
        self.assertEqual(setup.current_proxy(DB, PROXY, wait_time=0)["engineType"], 2)
        setup.api = Mock(side_effect=[(409, {"code": 409}), (200, {"code": 200, "data": {
            "engineType": 0, "proxy_config": None}})])
        self.assertFalse(setup.current_proxy(DB, CHAIN, wait_time=0)["matches"])
        self.assertEqual([call.args[1] for call in setup.api.call_args_list], ["cbs_proxy_get", "proxy_get"])

    def test_standard_engine_uses_legacy_endpoint_and_preserves_mode(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(side_effect=[(200, {"code": 200}), (200, {"code": 200})])
        self.assertTrue(setup.apply(DB, proxy_body(PROXY), PROXY, engine_type=0)["accepted"])
        self.assertEqual([call.args[1] for call in setup.api.call_args_list], ["proxy_stop", "proxy_set"])
        self.assertEqual(setup.api.call_args_list[1].args[3], dict(legacy_proxy_body(PROXY), engineType=0))

    def test_standard_chain_keeps_nodes_and_never_switches_to_cbs(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(side_effect=[(200, {"code": 200}), (200, {"code": 200})])
        self.assertTrue(setup.apply(DB, proxy_body(CHAIN), CHAIN, engine_type=0)["accepted"])
        self.assertEqual([c.args[1] for c in setup.api.call_args_list], ["proxy_stop", "proxy_set"])
        sent = setup.api.call_args.args[3]
        self.assertEqual(sent["engineType"], 0)
        self.assertTrue(proxy_matches(sent, CHAIN))
        self.assertFalse(sent["dnsOverProxyDisabled"])

    def test_region_sync_runs_after_binding_and_records_only_verified_target(self):
        region = {"country": "US", "language": "en", "timezone": "America/Los_Angeles"}
        target = dict(region, latitude=34.0522, longitude=-118.2437)
        with tempfile.TemporaryDirectory() as temp:
            setup = self.make_setup(temp)
            order = []
            setup.current_proxy.side_effect = [{"engineType": 0, "matches": False}, {"engineType": 0, "matches": True, "enabled": True}]
            setup.apply.side_effect = lambda *args: order.append("proxy_set") or {"accepted": True}
            with patch("setup_proxy.SystemSetup") as system, patch("setup_proxy.check_proxy", return_value={"exit_ip": IP}):
                system.return_value.resolve.return_value = target
                def configure(db, expected, report, *args):
                    order.append("system")
                    report.update(success=True, country_api_confirmed=True)
                system.return_value.configure.side_effect = configure
                result = setup.run(DB, dict(PROXY, region=region), CONFIG, wait_time=0, sync_system=True)
                self.assertTrue(result["success"])
                self.assertEqual(order, ["proxy_set", "system"])
                saved = json.loads(setup.mapping_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["devices"][DB]["system_config"]["expected"], target)
                self.assertEqual(saved["devices"][OTHER], {"proxy_id": "other", "state": "stopped"})
            setup = self.make_setup(temp)
            setup.current_proxy.side_effect = [{"engineType": 0, "matches": False}, {"engineType": 0, "matches": True, "enabled": True}]
            with patch("setup_proxy.SystemSetup") as system, patch("setup_proxy.check_proxy", return_value={"exit_ip": IP}):
                system.return_value.resolve.return_value = target
                system.return_value.configure.side_effect = ValueError("GPS not retained")
                self.assertFalse(setup.run(DB, dict(PROXY, region=region), CONFIG, wait_time=0, sync_system=True)["success"])
                self.assertNotIn("system_config", json.loads(setup.mapping_path.read_text(encoding="utf-8"))["devices"][DB])

    def test_region_success_is_saved_even_when_subsequent_network_check_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            setup = self.make_setup(temp)
            region = {"country": "US", "language": "en", "timezone": "America/Los_Angeles"}
            target = dict(region, latitude=34.05, longitude=-118.24)
            setup.current_proxy.side_effect = [{"engineType": 0, "matches": False},
                                               {"engineType": 0, "matches": True, "enabled": True}]
            setup.verify.return_value = {"verified": False, "error": "web timeout"}
            order = []
            setup.apply.side_effect = lambda *args: order.append("proxy") or {"accepted": True}
            with patch("setup_proxy.SystemSetup") as system, patch("setup_proxy.check_proxy", return_value={"exit_ip": IP}):
                system.return_value.resolve.return_value = target
                def configure(db, expected, report, *args):
                    order.append("system")
                    report.update(success=True, country_api_confirmed=True)
                system.return_value.configure.side_effect = configure
                result = setup.run(DB, dict(PROXY, region=region), CONFIG, wait_time=0, sync_system=True)
            self.assertEqual(order, ["proxy", "system"])
            self.assertFalse(result["success"])
            self.assertTrue(result["system"]["success"])
            self.assertTrue(result["system_mapping_updated"])
            mapping = json.loads(setup.mapping_path.read_text(encoding="utf-8"))
            self.assertEqual(mapping["devices"][DB]["system_config"]["expected"], target)
            self.assertIsNone(mapping["devices"][DB]["proxy_id"])
            self.assertEqual(mapping["devices"][OTHER], {"proxy_id": "other", "state": "stopped"})

    def test_missing_health_field_still_requires_actual_web_success(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": 200, "data": {
            "engineType": 0, "proxy_config": {"enabled": True}}}))
        setup.cloud_json = Mock(return_value={"ip": IP})
        url = "https://www.tiktok.com/"
        setup.cloud_http = Mock(return_value={"url": url, "http_status": 200})
        config = dict(CONFIG, probe_urls=[url], verify_rounds=2)
        self.assertTrue(setup.verify(DB, IP, config, 0)["verified"])
        setup.cloud_http.side_effect = ValueError("network failed")
        result = setup.verify(DB, IP, config, 0)
        self.assertFalse(result["verified"])
        self.assertEqual(result["web_checks"][-1], {"url": url, "round": 1, "success": False, "error": "network failed"})

    def test_matching_working_proxy_is_not_rewritten_but_broken_proxy_is_repaired(self):
        self.assertTrue(proxy_matches({"nodes": [legacy_proxy_body(PROXY)]}, PROXY))
        self.assertFalse(proxy_matches(dict(legacy_proxy_body(PROXY), password="changed"), PROXY))
        self.assertFalse(proxy_matches({"nodes": [legacy_proxy_body(PROXY), legacy_proxy_body(PROXY)]}, PROXY))
        with tempfile.TemporaryDirectory() as temp:
            setup = self.make_setup(temp)
            setup.current_proxy.return_value = {"engineType": 1, "matches": True}
            report = self.execute(setup)
            self.assertTrue(report["success"])
            self.assertEqual(report["set_api"]["action"], "already_configured")
            setup.apply.assert_not_called()
            setup = self.make_setup(temp)
            setup.current_proxy.return_value = {"engineType": 1, "matches": True}
            setup.verify.side_effect = [{"verified": False}, dict(VERIFIED)]
            self.assertTrue(self.execute(setup)["success"])
            setup.apply.assert_called_once_with(DB, proxy_body(PROXY), PROXY, 1)

    def test_verify_requires_healthy_proxy_and_matching_cloud_exit(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": "0", "data": {"enabled": True, "healthy": True}}))
        setup.cloud_json = Mock(return_value={"ip": IP})
        self.assertTrue(setup.verify(DB, IP, CONFIG, 0)["verified"])
        setup.cloud_json.return_value = {"ip": "198.51.100.99"}
        self.assertFalse(setup.verify(DB, IP, CONFIG, 0)["verified"])
        setup.api.return_value = (200, {"code": 0, "data": {"enabled": True, "healthy": False}})
        setup.cloud_json.reset_mock()
        self.assertFalse(setup.verify(DB, IP, CONFIG, 0)["verified"])
        setup.cloud_json.assert_not_called()
        setup.api.side_effect = [(409, {"code": 409}), (200, {"code": 200,
            "data": {"engineType": 1, "healthy": True, "proxy_config": {"enable": "true"}}})]
        setup.cloud_json.return_value = {"ip": IP}
        self.assertTrue(setup.verify(DB, IP, CONFIG, 0)["verified"])
        self.assertEqual(setup.api.call_args.args[1], "proxy_get")

    def test_shell_output_is_required_not_merely_command_accepted(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": 200, "data": {"message": json.dumps({"ip": IP})}}))
        self.assertEqual(setup.cloud_json(DB, CONFIG["probe_url"], 10), {"ip": IP})
        for data in ({"cmd": "curl"}, {"message": "curl: failed to connect"}):
            setup.api.return_value = (200, {"code": 200, "data": data})
            with self.assertRaises(ValueError):
                setup.cloud_json(DB, CONFIG["probe_url"], 10)

    def test_web_check_requires_real_exit_code_and_successful_http_status(self):
        setup = ProxySetup(HOST)
        setup.session = Mock()
        response = setup.session.post.return_value
        response.json.return_value = {"code": 200, "data": {"exit_code": 0, "output": "200"}}
        url = "https://www.tiktok.com/"
        self.assertEqual(setup.cloud_http(DB, url, 10), {"url": url, "http_status": 200})
        self.assertFalse(setup.session.post.call_args.kwargs["json"]["is_root"])
        self.assertNotIn("--noproxy", setup.session.post.call_args.kwargs["json"]["command"])
        for data in ({"exit_code": 28, "output": "200", "error": "private-pass"},
                     {"exit_code": 0, "output": "403"}, {"output": "200"},
                     {"exit_code": False, "output": "200"}):
            response.json.return_value = {"code": 200, "data": data}
            with self.assertRaises(ValueError) as raised:
                setup.cloud_http(DB, url, 10)
            self.assertNotIn("private-pass", str(raised.exception))

    def test_root_web_output_isolated_and_exit_status_required(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": 200, "data": {"message": "200\nCURL_EXIT=0"}}))
        url = "https://www.tiktok.com/"
        self.assertEqual(setup.cloud_http(DB, url, 10, root=True)["http_status"], 200)
        self.assertIn("2>/dev/null; curl_status=$?", setup.api.call_args.args[3]["cmd"])
        for output in ("000\nCURL_EXIT=28", "200", "200\nCURL_EXIT=1"):
            setup.api.return_value[1]["data"]["message"] = output
            with self.assertRaises(ValueError):
                setup.cloud_http(DB, url, 10, root=True)

    def test_exit_ip_success_cannot_hide_unstable_web_requests(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": 200, "data": {"enabled": True, "healthy": True}}))
        setup.cloud_json = Mock(return_value={"ip": IP})
        url = "https://www.tiktok.com/"
        setup.cloud_http = Mock(side_effect=[{"url": url, "http_status": 200}, ValueError("network failed")])
        config = dict(CONFIG, probe_urls=[url], verify_rounds=2)
        result = setup.verify(DB, IP, config, 0)
        self.assertFalse(result["verified"])
        self.assertEqual(result["completed_rounds"], 1)
        self.assertFalse(result["tiktok_app_verified"])
        self.assertEqual(result["web_checks"][-1], {"url": url, "round": 2, "success": False, "error": "network failed"})
        setup.cloud_http.side_effect = None
        setup.cloud_http.return_value = {"url": url, "http_status": 200}
        self.assertTrue(setup.verify(DB, IP, config, 0)["verified"])

    def test_timeout_readback_success_preserves_other_mapping_and_hides_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            setup = self.make_setup(temp)
            setup.apply.side_effect = requests.Timeout("private-pass must not appear")
            report = self.execute(setup)
            self.assertTrue(report["success"])
            setup.apply.assert_called_once()
            saved = (Path(temp) / "proxy_status.json").read_text(encoding="utf-8")
            for secret in (PROXY["username"], PROXY["password"]):
                self.assertNotIn(secret, saved)
            mapping = json.loads(setup.mapping_path.read_text(encoding="utf-8"))
            self.assertEqual(mapping["devices"][OTHER], {"proxy_id": "other", "state": "stopped"})
            self.assertEqual(mapping["devices"][DB]["proxy_id"], "p1")
            self.assertEqual(mapping["devices"][DB]["account_email"], "test@example.com")
            setup.device.post_action.assert_not_called()

    def test_stopped_requires_explicit_start_and_only_started_target_is_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            setup = self.make_setup(temp, "stopped")
            self.assertFalse(self.execute(setup)["success"])
            setup.device.post_action.assert_not_called()
            setup.apply.assert_not_called()
            setup = self.make_setup(temp, "stopped")
            self.assertTrue(self.execute(setup, start_stopped=True)["success"])
            setup.device.post_action.assert_called_once_with("run", [DB])
            setup.device.restore_stopped.assert_called_once_with([DB])
            setup = self.make_setup(temp, "stopped")
            self.assertTrue(self.execute(setup, start_stopped=True, keep_running=True)["success"])
            setup.device.restore_stopped.assert_not_called()

    def test_failed_verification_does_not_bind_and_restores_started_target(self):
        with tempfile.TemporaryDirectory() as temp:
            setup = self.make_setup(temp, "stopped")
            setup.verify.return_value = {"verified": False, "error": "exit mismatch"}
            self.assertFalse(self.execute(setup, start_stopped=True, keep_running=True)["success"])
            setup.device.restore_stopped.assert_called_once_with([DB])
            mapping = json.loads(setup.mapping_path.read_text(encoding="utf-8"))
            self.assertIsNone(mapping["devices"][DB]["proxy_id"])
            setup = self.make_setup(temp, "stopped")
            setup.device.restore_stopped.return_value = {"success": False}
            self.assertFalse(self.execute(setup, start_stopped=True)["success"])

    def test_initializer_can_own_cleanup_after_success_or_network_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            for verified in (True, False):
                setup = self.make_setup(temp, "stopped")
                setup.verify.return_value = dict(VERIFIED, verified=verified)
                result = self.execute(setup, start_stopped=True, restore_state=False)
                self.assertEqual(result["success"], verified)
                self.assertTrue(result["started_by_script"])
                setup.device.post_action.assert_called_once_with("run", [DB])
                setup.device.restore_stopped.assert_not_called()

    def test_verify_only_preserves_install_report_mapping_and_device_state(self):
        with tempfile.TemporaryDirectory() as temp:
            setup = self.make_setup(temp)
            before = setup.mapping_path.read_bytes()
            (Path(temp) / "proxy_status.json").write_text("existing", encoding="utf-8")
            self.assertTrue(self.execute(setup, verify_only=True)["success"])
            setup.apply.assert_not_called()
            setup.device.post_action.assert_not_called()
            setup.device.restore_stopped.assert_not_called()
            self.assertEqual(setup.mapping_path.read_bytes(), before)
            self.assertEqual((Path(temp) / "proxy_status.json").read_text(encoding="utf-8"), "existing")
            self.assertTrue((Path(temp) / "proxy_verify.json").exists())
            with self.assertRaises(ValueError):
                setup.run(DB + "," + OTHER, PROXY, CONFIG)


if __name__ == "__main__":
    unittest.main()
