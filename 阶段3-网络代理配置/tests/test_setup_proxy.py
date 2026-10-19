"""离线验证：明确单台、共享代理响应、超时回查、真实出口及状态恢复。"""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import sys
import tempfile
import threading
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
    def test_standard_proxies_use_distinct_ports_even_with_same_recorded_ip(self):
        instances = {DB: {"http_api_port": 35000, "ip": "172.17.0.2"},
                     OTHER: {"http_api_port": 35001, "ip": "172.17.0.2"}}
        states = {}
        state_lock = threading.Lock()

        def request(method, url, json=None, **options):
            port = 35000 if ":35000/" in url else 35001 if ":35001/" in url else None
            self.assertIsNotNone(port, url)
            with state_lock:
                if method == "POST":
                    states[port] = dict(json)
                data = states.get(port, {"enabled": False})
                return Mock(status_code=200, json=Mock(return_value={"code": 200, "data": data}))

        other_proxy = dict(CHAIN, id="p2", host="198.51.100.2", password="other-pass")
        setups = [ProxySetup(HOST), ProxySetup(HOST)]
        for setup in setups:
            setup.device = Mock()
            setup.device.list_instances.return_value = instances
            setup.session = Mock()
            setup.session.request.side_effect = request
            setup.api = Mock(side_effect=AssertionError("shared proxy route was used"))
            setup.cloud_json = Mock(return_value={"ip": IP})
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(setup.apply, db, proxy_body(proxy), proxy, 0)
                     for setup, db, proxy in zip(setups, (DB, OTHER), (CHAIN, other_proxy))]
            self.assertTrue(all(task.result(timeout=5)["accepted"] for task in tasks))
        for setup, db, proxy in zip(setups, (DB, OTHER), (CHAIN, other_proxy)):
            self.assertTrue(setup.current_proxy(db, proxy, 0)["matches"])
            self.assertTrue(setup.verify(db, IP, CONFIG, 0, proxy)["verified"])
            setup.api.assert_not_called()
        self.assertEqual(states[35000]["nodes"][1]["password"], CHAIN["password"])
        self.assertEqual(states[35001]["nodes"][1]["password"], other_proxy["password"])

    def test_missing_or_duplicate_control_port_blocks_requests(self):
        for instances in ({}, {DB: {"http_api_port": True}}, {DB: {"http_api_port": 0}},
                          {DB: {"http_api_port": 35000}, OTHER: {"http_api_port": 35000}}):
            with self.subTest(instances=instances):
                setup = ProxySetup(HOST)
                setup.device = Mock()
                setup.device.list_instances.return_value = instances
                setup.session = Mock()
                with self.assertRaises(ValueError):
                    setup.control_api("GET", "proxy/get", DB)
                setup.session.request.assert_not_called()

    def test_standard_stop_requires_explicit_disabled_state_and_never_retries_timeout(self):
        for code in (0, 200):
            setup = ProxySetup(HOST)
            setup.control_api = Mock(side_effect=[
                (200, {"code": code, "data": {"enabled": True}}),
                (200, {"code": 200, "data": {"enabled": True}})])
            with self.assertRaisesRegex(ValueError, "旧代理停止未确认"):
                setup.apply(DB, proxy_body(CHAIN), CHAIN, 0)
            self.assertEqual([call.args[0] for call in setup.control_api.call_args_list], ["POST", "GET"])
        setup.control_api = Mock(side_effect=[
            (200, {"code": 0}), (200, {"code": 200, "data": {"enabled": False}}),
            (200, {"code": 200})])
        self.assertTrue(setup.apply(DB, proxy_body(CHAIN), CHAIN, 0)["accepted"])
        setup.control_api = Mock(side_effect=requests.Timeout())
        with self.assertRaises(requests.Timeout):
            setup.apply(DB, proxy_body(CHAIN), CHAIN, 0)
        self.assertEqual([c.args[0] for c in setup.control_api.call_args_list], ["POST", "GET"])
        for disabled in (True, False):
            setup.control_api = Mock(side_effect=[requests.Timeout(),
                (200, {"code": 200, "data": {"enabled": not disabled}}), (200, {"code": 200})])
            if disabled:
                self.assertTrue(setup.apply(DB, proxy_body(CHAIN), CHAIN, 0)["accepted"])
                self.assertEqual([c.args[3]["enabled"] for c in setup.control_api.call_args_list if c.args[0] == "POST"],
                                 [False, True])
            else:
                with self.assertRaisesRegex(ValueError, "旧代理停止未确认"):
                    setup.apply(DB, proxy_body(CHAIN), CHAIN, 0)
                self.assertEqual(setup.control_api.call_count, 2)

    def test_android_boot_requires_command_result_and_blocks_proxy_writes(self):
        setup = ProxySetup(HOST)
        setup.log = Mock()
        setup.api = Mock(side_effect=[
            (200, {"code": 200, "data": {"message": "0"}}),
            (200, {"code": 200, "data": {"message": "1"}})])
        with patch("setup_proxy.time.sleep"):
            setup.wait_android_ready(DB)
        setup.api = Mock(return_value=(200, {"code": 200, "data": {}}))
        with self.assertRaisesRegex(ValueError, "Android启动尚未完成"):
            setup.wait_android_ready(DB, 0)
        with tempfile.TemporaryDirectory() as temp:
            setup = self.make_setup(temp, "stopped")
            setup.wait_android_ready.side_effect = ValueError("Android启动尚未完成")
            report = self.execute(setup, start_stopped=True)
            self.assertFalse(report["success"])
            setup.apply.assert_not_called()
            setup.device.restore_stopped.assert_called_once_with([DB])

    def test_concurrent_mapping_updates_preserve_both_devices(self):
        with tempfile.TemporaryDirectory() as temp:
            first = self.make_setup(temp)
            second = ProxySetup(HOST, temp, first.mapping_path)
            from setup_proxy import read_json, save_json
            first_saving = threading.Event()
            second_started = threading.Event()
            second_read = threading.Event()
            release_first = threading.Event()
            second_thread = None

            def read(path):
                if threading.get_ident() == second_thread:
                    second_read.set()
                return read_json(path)

            def save(path, data):
                if data["devices"][DB].get("proxy_id") == "p1" and data["devices"][OTHER].get("proxy_id") == "other":
                    first_saving.set()
                    if not release_first.wait(timeout=5):
                        raise ValueError("test did not release first writer")
                return save_json(path, data)

            def update_second():
                nonlocal second_thread
                second_thread = threading.get_ident()
                second_started.set()
                second.update_mapping(OTHER, "p2", VERIFIED, "running")

            with patch("setup_proxy.read_json", side_effect=read), patch("setup_proxy.save_json", side_effect=save), \
                    ThreadPoolExecutor(max_workers=2) as pool:
                first_result = pool.submit(first.update_mapping, DB, "p1", VERIFIED, "stopped")
                try:
                    self.assertTrue(first_saving.wait(timeout=5))
                    second_result = pool.submit(update_second)
                    self.assertTrue(second_started.wait(timeout=5))
                    self.assertFalse(second_read.wait(timeout=0.1), "second reader entered an unfinished mapping transaction")
                finally:
                    release_first.set()
                first_result.result(timeout=5)
                second_result.result(timeout=5)
            mapping = read_json(first.mapping_path)
            self.assertEqual(mapping["devices"][DB]["proxy_id"], "p1")
            self.assertEqual(mapping["devices"][OTHER]["proxy_id"], "p2")
            self.assertEqual(mapping["devices"][DB]["account_email"], "test@example.com")

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
        setup.control_api = Mock(return_value=(200, {"code": 200, "data": dict(legacy_proxy_body(PROXY), enabled=True)}))
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
        setup.wait_android_ready = Mock()
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
                self.assertTrue(setup.apply(DB, proxy_body(PROXY), PROXY, engine_type=1)["accepted"])
                self.assertEqual([call.args[1] for call in setup.api.call_args_list], ["proxy_stop", "proxy_get", "proxy_set"])
                self.assertTrue(proxy_matches(setup.api.call_args.args[3], PROXY))

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
                    setup.apply(DB, proxy_body(PROXY), PROXY, engine_type=1)
                self.assertEqual([call.args[1] for call in setup.api.call_args_list], ["proxy_stop", "proxy_get"])

    def test_proxy_service_waits_past_rom_metadata_before_any_write(self):
        setup = ProxySetup(HOST)
        setup.log = Mock()
        pending = (404, {"code": 404})
        ready = (200, {"code": 200, "data": {"enabled": False}})
        setup.control_api = Mock(side_effect=[pending, ready])
        setup.api = Mock()
        with patch("setup_proxy.time.sleep"):
            self.assertEqual(setup.current_proxy(DB, CHAIN), {"code": 200, "engineType": 0, "enabled": False, "matches": False})
        self.assertEqual([call.args[:2] for call in setup.control_api.call_args_list], [("GET", "proxy/get")] * 2)
        setup.api.assert_not_called()
        setup.log.assert_called_once_with("等待云机代理服务就绪")
        setup.control_api = Mock(return_value=pending)
        with self.assertRaisesRegex(ValueError, "代理服务尚未就绪"):
            setup.current_proxy(DB, CHAIN, wait_time=0)
        self.assertEqual(setup.control_api.call_count, 1)

    def test_ready_cbs_state_and_legacy_conflict_are_still_supported(self):
        setup = ProxySetup(HOST)
        setup.api = Mock(return_value=(200, {"code": 0, "data": {
            "engineType": 2, "enabled": False, "healthy": False}}))
        self.assertEqual(setup.current_proxy(DB, PROXY, wait_time=0)["engineType"], 2)
        setup.api = Mock(side_effect=[(409, {"code": 409}), (200, {"code": 200, "data": {
            "engineType": 0, "proxy_config": None}})])
        self.assertFalse(setup.current_proxy(DB, PROXY, wait_time=0)["matches"])
        self.assertEqual([call.args[1] for call in setup.api.call_args_list], ["cbs_proxy_get", "proxy_get"])

    def test_standard_engine_uses_instance_control_service(self):
        setup = ProxySetup(HOST)
        setup.control_api = Mock(side_effect=[(200, {"code": 200, "data": {"enabled": False}}), (200, {"code": 200})])
        setup.api = Mock()
        self.assertTrue(setup.apply(DB, proxy_body(PROXY), PROXY, engine_type=0)["accepted"])
        self.assertEqual([call.args[1] for call in setup.control_api.call_args_list], ["proxy/set_enabled"] * 2)
        self.assertEqual(setup.control_api.call_args_list[0].args[3], {"enabled": False})
        self.assertEqual(setup.control_api.call_args_list[1].args[3], dict(legacy_proxy_body(PROXY), enabled=True))
        setup.api.assert_not_called()

    def test_standard_chain_keeps_nodes_and_never_switches_to_cbs(self):
        setup = ProxySetup(HOST)
        setup.control_api = Mock(side_effect=[(200, {"code": 200, "data": {"enabled": False}}), (200, {"code": 200})])
        setup.api = Mock()
        self.assertTrue(setup.apply(DB, proxy_body(CHAIN), CHAIN, engine_type=0)["accepted"])
        self.assertEqual([c.args[1] for c in setup.control_api.call_args_list], ["proxy/set_enabled"] * 2)
        sent = setup.control_api.call_args.args[3]
        self.assertTrue(sent["enabled"])
        self.assertTrue(proxy_matches(sent, CHAIN))
        self.assertFalse(sent["dnsOverProxyDisabled"])
        self.assertEqual(sent["nodes"][0]["proxyName"], "vless")
        setup.api.assert_not_called()

    def test_region_sync_runs_after_binding_and_records_only_verified_target(self):
        region = {"country": "US", "language": "en", "timezone": "America/Los_Angeles"}
        target = dict(region, latitude=34.0522, longitude=-118.2437)
        with tempfile.TemporaryDirectory() as temp:
            setup = self.make_setup(temp)
            order = []
            setup.current_proxy.side_effect = [{"engineType": 0, "matches": False}, {"engineType": 0, "matches": True, "enabled": True}]
            setup.apply.side_effect = lambda *args: order.append("proxy_set") or {"accepted": True}
            with patch("setup_proxy.SystemSetup") as system, patch("setup_proxy.check_proxy", return_value={"exit_ip": IP}):
                def resolve(*args):
                    order.append("resolve")
                    self.assertEqual(setup.current_proxy.call_count, 2)
                    return target
                system.return_value.resolve.side_effect = resolve
                def configure(db, expected, report, *args):
                    order.append("system")
                    report.update(success=True, country_api_confirmed=True, gps_persisted=True)
                system.return_value.configure.side_effect = configure
                result = setup.run(DB, dict(PROXY, region=region), CONFIG, wait_time=0, sync_system=True)
                self.assertTrue(result["success"])
                self.assertEqual(order, ["proxy_set", "resolve", "system"])
                saved = json.loads(setup.mapping_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["devices"][DB]["system_config"]["expected"], target)
                self.assertTrue(saved["devices"][DB]["system_config"]["gps_persisted"])
                self.assertEqual(saved["devices"][OTHER], {"proxy_id": "other", "state": "stopped"})
            setup = self.make_setup(temp)
            setup.current_proxy.side_effect = [{"engineType": 0, "matches": False}, {"engineType": 0, "matches": True, "enabled": True}]
            with patch("setup_proxy.SystemSetup") as system, patch("setup_proxy.check_proxy", return_value={"exit_ip": IP}):
                system.return_value.resolve.return_value = target
                system.return_value.configure.side_effect = ValueError("GPS not retained")
                self.assertFalse(setup.run(DB, dict(PROXY, region=region), CONFIG, wait_time=0, sync_system=True)["success"])
                self.assertNotIn("system_config", json.loads(setup.mapping_path.read_text(encoding="utf-8"))["devices"][DB])

    def test_confirmed_reboot_restores_lost_proxy_once_and_still_requires_verification(self):
        region = {"country": "US", "language": "en", "timezone": "America/Los_Angeles"}
        for lost, uncertain, verified in ((False, False, True), (True, False, True),
                                          (True, True, True), (True, True, False)):
            with self.subTest(lost=lost, uncertain=uncertain, verified=verified), tempfile.TemporaryDirectory() as temp:
                setup = self.make_setup(temp)
                active = {"engineType": 0, "matches": True, "enabled": True}
                after = {"engineType": 0, "matches": False, "enabled": False} if lost else active
                setup.current_proxy.side_effect = [{"engineType": 0, "matches": False}, active, after]
                setup.apply.side_effect = [{"accepted": True}, requests.Timeout() if uncertain else {"accepted": True}]
                setup.verify.return_value = dict(VERIFIED, verified=verified, api={"engineType": 0})
                with patch("setup_proxy.SystemSetup") as system, patch("setup_proxy.check_proxy", return_value={"exit_ip": IP}):
                    system.return_value.resolve.return_value = dict(region, latitude=34.0522, longitude=-118.2437)
                    system.return_value.configure.side_effect = lambda db, expected, report, *args: report.update(
                        success=True, country_api_confirmed=True, gps_persisted=True, reboot_verified=True)
                    report = setup.run(DB, dict(CHAIN, region=region), CONFIG, wait_time=0, sync_system=True)
                self.assertEqual(report["success"], verified)
                self.assertEqual(setup.apply.call_count, 2 if lost else 1)
                self.assertEqual(report["proxy_after_reboot"], after)
                self.assertEqual("reapply_api" in report, lost)
                if uncertain:
                    self.assertIsNone(report["reapply_api"]["accepted"])

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
        setup.api = Mock(return_value=(200, {"code": 200, "data": {"message": json.dumps({"ip": IP}) + "\nCURL_EXIT=0\n"}}))
        self.assertEqual(setup.cloud_json(DB, CONFIG["probe_url"], 10), {"ip": IP})
        self.assertIn("curl_status=$?", setup.api.call_args.args[3]["cmd"])
        for data in ({"cmd": "curl"}, {"message": json.dumps({"ip": IP})},
                     {"message": "not json\nCURL_EXIT=0"}, {"message": "[]\nCURL_EXIT=0"}):
            setup.api.return_value = (200, {"code": 200, "data": data})
            with self.assertRaises(ValueError):
                setup.cloud_json(DB, CONFIG["probe_url"], 10)
        for code in (6, 7, 28, 35):
            setup.api.return_value = (200, {"code": 200, "data": {"message": "\nCURL_EXIT=" + str(code)}})
            with self.assertRaisesRegex(ValueError, "curl退出码 " + str(code)) as error:
                setup.cloud_json(DB, CONFIG["probe_url"], 10)
            self.assertIn(CONFIG["probe_url"], str(error.exception))
        setup.api.return_value = (200, {"code": 0, "data": {"message": json.dumps({"ip": IP}) + "\nCURL_EXIT=0"}})
        with self.assertRaisesRegex(ValueError, "业务码 0"):
            setup.cloud_json(DB, CONFIG["probe_url"], 10)

    def test_retry_does_not_reuse_previous_web_results_after_exit_probe_fails(self):
        setup = ProxySetup(HOST)
        setup.log = Mock()
        setup.api = Mock(return_value=(200, {"code": 200, "data": {"enabled": True}}))
        setup.cloud_json = Mock(side_effect=[{"ip": IP}, ValueError("exit probe failed")])
        url = "https://www.tiktok.com/"
        setup.cloud_http = Mock(side_effect=[{"url": url, "http_status": 200}, ValueError("TLS failed")])
        with patch("setup_proxy.time.monotonic", side_effect=[0, 0, 0, 2]), patch("setup_proxy.time.sleep"):
            result = setup.verify(DB, IP, dict(CONFIG, probe_urls=[url], verify_rounds=2), 1)
        self.assertFalse(result["verified"])
        self.assertNotIn("exit_ip", result)
        self.assertEqual(result["web_checks"], [])
        self.assertEqual(result["completed_rounds"], 0)
        self.assertEqual(result["error"], "exit probe failed")
        self.assertEqual(result["attempt_errors"], ["TLS failed", "exit probe failed"])

    def test_web_check_requires_real_exit_code_and_successful_http_status(self):
        setup = ProxySetup(HOST)
        setup.control_api = Mock(return_value=(200, {"code": 200, "data": {"exit_code": 0, "output": "200"}}))
        url = "https://www.tiktok.com/"
        self.assertEqual(setup.cloud_http(DB, url, 10), {"url": url, "http_status": 200})
        self.assertFalse(setup.control_api.call_args.args[3]["is_root"])
        self.assertNotIn("--noproxy", setup.control_api.call_args.args[3]["command"])
        for data in ({"exit_code": 28, "output": "200", "error": "private-pass"},
                     {"exit_code": 0, "output": "403"}, {"output": "200"},
                     {"exit_code": False, "output": "200"}):
            setup.control_api.return_value = (200, {"code": 200, "data": data})
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
