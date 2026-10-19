"""地区与GPS同步顺序、只读、重启回查、失败边界。"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from setup_system import SystemSetup, region_settings


DB = "EDGE123456"
REGION = {"country": "US", "language": "en", "timezone": "America/Los_Angeles",
          "latitude": 34.0522, "longitude": -118.2437}
ACTUAL = {"country": "US", "locale": "en-US", "timezone": "America/Los_Angeles",
          "latitude": 34.0522, "longitude": -118.2437}


class SystemTests(unittest.TestCase):
    def test_gps_snapshot_uses_target_control_port_and_checks_http_status(self):
        proxy = Mock()
        proxy.control_api.return_value = (200, {"code": 200, "data": {
            "latitude": ACTUAL["latitude"], "longitude": ACTUAL["longitude"]}})
        system = SystemSetup(proxy)
        system.request = Mock(return_value=dict(ACTUAL))
        values = {"persist.sys.country": "US", "persist.sys.locale": "en-US",
                  "persist.sys.timezone": REGION["timezone"]}
        system.shell = Mock(side_effect=lambda db, cmd: values[cmd.removeprefix("getprop ")])
        self.assertEqual(system.snapshot(DB), ACTUAL)
        proxy.control_api.assert_called_once_with("GET", "location/get_data", DB, timeout=20)
        proxy.session.get.assert_not_called()
        proxy.control_api.return_value = (500, {"code": 200, "data": dict(ACTUAL)})
        with self.assertRaisesRegex(ValueError, "GPS回查失败"):
            system.snapshot(DB)

    def make_system(self):
        proxy = Mock(host="http://host")
        proxy.api.return_value = (200, {"code": 200, "data": {}})
        proxy.control_api.return_value = (200, {"code": 200, "data": {}})
        proxy.session.post.return_value.json.return_value = {"code": 200}
        system = SystemSetup(proxy)
        system.snapshot = Mock(return_value=dict(ACTUAL))
        system.shell = Mock(side_effect=lambda db, command: str(REGION["latitude" if command.endswith(".lat") else "longitude"]))
        system.reboot = Mock(side_effect=lambda db, report: report.update(reboot_verified=True))
        return system

    def test_country_timezone_language_gps_order_and_reboot_are_required_once(self):
        system = self.make_system()
        report = {}
        system.configure(DB, REGION, report)
        self.assertTrue(report["success"])
        self.assertTrue(report["reboot_verified"])
        self.assertFalse(report["sim_details_verified"])
        self.assertTrue(report["gps_persisted"])
        body = system.proxy.session.post.call_args.kwargs["json"]
        self.assertEqual(body["db_ids"], [DB])
        self.assertFalse(body["isRestart"])
        self.assertEqual(json.loads(body["user_prop"]), {
            "persist.sys.cloud.gps.lat": str(REGION["latitude"]),
            "persist.sys.cloud.gps.lon": str(REGION["longitude"])})
        self.assertEqual(system.proxy.method_calls[:4], [
            call.api("POST", "country_set", DB, {"country": REGION["country"]}),
            call.control_api("POST", "system/update_settings", DB, {"timezone": REGION["timezone"]}),
            call.api("POST", "language_set", DB, {"country": REGION["country"], "language": REGION["language"]}),
            call.api("POST", "gps_inject", DB, {k: REGION[k] for k in ("latitude", "longitude")})])
        self.assertNotIn("timezone_set", [c.args[1] for c in system.proxy.api.call_args_list])
        self.assertEqual(report["steps"][1]["transport"], "instance_port")
        system.reboot.assert_called_once_with(DB, report)
        self.assertEqual(system.snapshot.call_count, 2)

    def test_recorded_matching_settings_skip_all_writes_and_verify_is_read_only(self):
        system = self.make_system()
        previous = {"expected": REGION, "country_api_confirmed": True, "gps_persisted": True}
        report = system.configure(DB, REGION, {}, previous=previous)
        self.assertEqual(report["action"], "already_configured")
        system.proxy.api.assert_not_called()
        system.proxy.control_api.assert_not_called()
        system.proxy.session.post.assert_not_called()
        system.reboot.assert_not_called()

        system.configure(DB, REGION, {}, verify_only=True)
        system.proxy.api.assert_not_called()
        drift = dict(REGION, latitude=34.05223, longitude=-118.24368)
        self.assertEqual(system.configure(DB, drift, {}, previous=previous)["action"], "already_configured")
        system.proxy.api.assert_not_called()
        system.snapshot.return_value["country"] = "CN"
        with self.assertRaises(ValueError):
            system.configure(DB, REGION, {}, verify_only=True)
        system.reboot.assert_not_called()
        system.proxy.control_api.assert_not_called()

    def test_timeout_or_bad_post_reboot_readback_never_marks_success(self):
        system = self.make_system()
        system.proxy.api.side_effect = requests.Timeout("sensitive raw response")
        report = {}
        with self.assertRaises(requests.Timeout):
            system.configure(DB, REGION, report)
        self.assertFalse(report["success"])
        self.assertEqual(system.proxy.api.call_count, 1)
        self.assertFalse(report["steps"][0]["accepted"])
        system.reboot.assert_not_called()
        system = self.make_system()
        system.snapshot.side_effect = [ACTUAL, dict(ACTUAL, latitude=26.0614)]
        report = {}
        with self.assertRaises(ValueError):
            system.configure(DB, REGION, report)
        self.assertFalse(report["success"])

    def test_timezone_uncertain_response_uses_readback_without_resending_write(self):
        for failure in (requests.Timeout("private response"), (200, {"code": 0}), (500, {"code": 200})):
            with self.subTest(failure=type(failure).__name__):
                system = self.make_system()
                if isinstance(failure, Exception):
                    system.proxy.control_api.side_effect = failure
                else:
                    system.proxy.control_api.return_value = failure
                system.proxy.api.return_value = (200, {"code": 200, "data": dict(ACTUAL)})
                shell = system.shell.side_effect
                system.shell.side_effect = lambda db, cmd: REGION["timezone"] if cmd == "getprop persist.sys.timezone" else shell(db, cmd)
                report = system.configure(DB, REGION, {})
                self.assertTrue(report["success"])
                step = report["steps"][1]
                self.assertFalse(step["accepted"])
                self.assertTrue(step["readback_verified"])
                self.assertEqual(step["api_timezone"], step["android_timezone"])
                self.assertNotIn("private response", json.dumps(report))
                system.proxy.control_api.assert_called_once_with("POST", "system/update_settings", DB,
                                                                 {"timezone": REGION["timezone"]})
                self.assertNotIn("timezone_set", [c.args[1] for c in system.proxy.api.call_args_list])
                system.reboot.assert_called_once()

    def test_timezone_readback_must_match_both_sources_before_later_writes(self):
        for api_timezone, actual in (("GMT", REGION["timezone"]), (REGION["timezone"], "GMT"), ("GMT", "GMT")):
            with self.subTest(api_timezone=api_timezone, actual=actual):
                system = self.make_system()
                system.proxy.control_api.return_value = (200, {"code": 0})
                system.proxy.api.return_value = (200, {"code": 200, "data": {"timezone": api_timezone}})
                system.shell = Mock(return_value=actual)
                report = {}
                with patch("setup_system.time.sleep"), self.assertRaisesRegex(ValueError, "HTTP 200，业务码 0.*时区回查未确认"):
                    system.configure(DB, REGION, report)
                self.assertFalse(report["success"])
                writes = [c.args[1] for c in system.proxy.api.call_args_list if c.args[0] == "POST"]
                self.assertEqual(writes, ["country_set"])
                system.proxy.control_api.assert_called_once_with("POST", "system/update_settings", DB,
                                                                 {"timezone": REGION["timezone"]})
                self.assertEqual(system.shell.call_count, 3)
                system.proxy.session.post.assert_not_called()
                system.reboot.assert_not_called()

    def test_timezone_read_queries_retry_but_final_reboot_snapshot_stays_strict(self):
        system = self.make_system()
        system.proxy.control_api.side_effect = requests.Timeout()
        system.proxy.api.side_effect = [(200, {"code": 200}), requests.Timeout(), requests.Timeout(),
            (200, {"code": 200, "data": dict(ACTUAL)}), (200, {"code": 200}), (200, {"code": 200})]
        shell = system.shell.side_effect
        system.shell.side_effect = lambda db, cmd: REGION["timezone"] if cmd == "getprop persist.sys.timezone" else shell(db, cmd)
        system.snapshot.side_effect = [ACTUAL, dict(ACTUAL, timezone="GMT")]
        report = {}
        with patch("setup_system.time.sleep"), self.assertRaisesRegex(ValueError, "重启后地区参数或GPS未保留"):
            system.configure(DB, REGION, report)
        self.assertTrue(report["steps"][1]["readback_verified"])
        self.assertFalse(report["success"])
        self.assertNotIn("gps_persisted", report)
        system.proxy.control_api.assert_called_once()

    def test_language_uncertain_response_reads_android_before_api_without_resending(self):
        for failure in ((200, {"code": 0}), requests.Timeout("private response"), (500, {"code": 200})):
            with self.subTest(failure=type(failure).__name__):
                system = self.make_system()
                def api(method, action, db, body):
                    if action == "language_set":
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    return 200, {"code": 200, "data": dict(ACTUAL)}
                system.proxy.api.side_effect = api
                shell = system.shell.side_effect
                system.shell.side_effect = lambda db, cmd: ACTUAL["locale"] if cmd == "getprop persist.sys.locale" else shell(db, cmd)
                calls = Mock()
                calls.attach_mock(system.proxy.api, "api")
                calls.attach_mock(system.shell, "shell")
                report = system.configure(DB, REGION, {})
                step = report["steps"][2]
                self.assertTrue(report["success"])
                self.assertFalse(step["accepted"])
                self.assertTrue(step["readback_verified"])
                self.assertEqual(step["android_locale"], "en-US")
                self.assertEqual(step["api_locale"], "en-US")
                self.assertNotIn("private response", json.dumps(report))
                language_call = call.api("POST", "language_set", DB, {"country": "US", "language": "en"})
                index = calls.mock_calls.index(language_call)
                self.assertEqual(calls.mock_calls[index + 1:index + 3], [
                    call.shell(DB, "getprop persist.sys.locale"),
                    call.api("GET", "get_timezone_locale", DB, None)])
                self.assertEqual(calls.mock_calls.count(language_call), 1)
                system.reboot.assert_called_once()

    def test_language_readback_must_match_both_sources_before_gps_or_reboot(self):
        for api_locale, actual in (("en-CN", "en-US"), ("en-US", "en-CN"), ("en-CN", "")):
            with self.subTest(api_locale=api_locale, actual=actual):
                system = self.make_system()
                system.proxy.api.side_effect = lambda method, action, db, body: (
                    (200, {"code": 0}) if action == "language_set"
                    else (200, {"code": 200, "data": {"locale": api_locale}}))
                system.shell = Mock(return_value=actual)
                report = {}
                with patch("setup_system.time.sleep"), self.assertRaisesRegex(ValueError, "HTTP 200，业务码 0.*语言回查未确认"):
                    system.configure(DB, REGION, report)
                self.assertFalse(report["success"])
                self.assertEqual([c.args[1] for c in system.proxy.api.call_args_list if c.args[0] == "POST"],
                                 ["country_set", "language_set"])
                system.shell.assert_has_calls([call(DB, "getprop persist.sys.locale")] * 3)
                reads = [c for c in system.proxy.api.call_args_list if c.args[0] == "GET"]
                self.assertEqual(len(reads), 3 if actual == "en-US" else 0)
                system.proxy.session.post.assert_not_called()
                system.reboot.assert_not_called()

    def test_language_read_errors_retry_and_post_reboot_locale_stays_strict(self):
        system = self.make_system()
        system.proxy.api.side_effect = [(200, {"code": 200}), (200, {"code": 0}), requests.Timeout(),
            (200, {"code": 200, "data": {"locale": "en-CN"}}),
            (200, {"code": 200, "data": dict(ACTUAL)}), (200, {"code": 200})]
        shell = system.shell.side_effect
        locale_reads = iter([requests.Timeout(), "en-US", "en-US", "en-US"])
        def read(db, cmd):
            if cmd != "getprop persist.sys.locale":
                return shell(db, cmd)
            value = next(locale_reads)
            if isinstance(value, Exception):
                raise value
            return value
        system.shell.side_effect = read
        report = {}
        # 三轮以内未能双源确认就停止，不把查询超时当成成功。
        with patch("setup_system.time.sleep"), self.assertRaisesRegex(ValueError, "语言回查未确认"):
            system.configure(DB, REGION, report)
        self.assertFalse(report["success"])
        system.reboot.assert_not_called()

        system = self.make_system()
        system.proxy.api.side_effect = [(200, {"code": 200}), (200, {"code": 0}), requests.Timeout(),
            (200, {"code": 200, "data": dict(ACTUAL)}), (200, {"code": 200})]
        shell = system.shell.side_effect
        system.shell.side_effect = lambda db, cmd: "en-US" if cmd == "getprop persist.sys.locale" else shell(db, cmd)
        system.snapshot.side_effect = [ACTUAL, dict(ACTUAL, locale="en-CN")]
        report = {}
        with patch("setup_system.time.sleep"), self.assertRaisesRegex(ValueError, "重启后地区参数或GPS未保留"):
            system.configure(DB, REGION, report)
        self.assertTrue(report["steps"][2]["readback_verified"])
        self.assertFalse(report["success"])
        self.assertNotIn("gps_persisted", report)

    def test_initial_api_property_mismatch_is_recorded_then_all_settings_are_applied(self):
        for initial_country in ("", "CN", "US"):
            with self.subTest(initial_country=initial_country):
                proxy = Mock(host="http://host")
                proxy.session.post.return_value.json.return_value = {"code": 200}
                system = SystemSetup(proxy)
                system.request = Mock(return_value={"country": "CN", "locale": "en-CN", "timezone": "GMT"})
                proxy.control_api.return_value = (200, {"code": 200, "data": {
                    "latitude": ACTUAL["latitude"], "longitude": ACTUAL["longitude"]}})
                system.shell = Mock(side_effect=[initial_country, "en-US", REGION["timezone"],
                                                "US", "en-US", REGION["timezone"],
                                                str(REGION["latitude"]), str(REGION["longitude"])])
                system.reboot = Mock()
                # 真实属性看似匹配且有历史记录时，接口不一致也不能跳过设置。
                system.reboot.side_effect = lambda *args: setattr(system.request, "return_value", dict(ACTUAL))
                report = system.configure(DB, REGION, {}, previous={"expected": REGION, "country_api_confirmed": True})
                self.assertTrue(report["success"])
                self.assertEqual(report["before"]["country"], initial_country)
                self.assertEqual(report["before"]["api_region"]["country"], "CN")
                self.assertEqual([call.args[0] for call in system.request.call_args_list],
                                 ["get_timezone_locale", "country_set", "timezone_set", "language_set", "gps_inject", "get_timezone_locale"])
                self.assertNotIn("api_region", report["after"])

    def test_verify_only_and_post_configuration_still_reject_api_property_mismatch(self):
        for verify_only in (True, False):
            with self.subTest(verify_only=verify_only):
                proxy = Mock(host="http://host")
                proxy.session.post.return_value.json.return_value = {"code": 200}
                system = SystemSetup(proxy)
                system.request = Mock(return_value={"country": "CN", "locale": "en-CN", "timezone": "GMT"})
                proxy.control_api.return_value = (200, {"code": 200, "data": {
                    "latitude": ACTUAL["latitude"], "longitude": ACTUAL["longitude"]}})
                system.shell = Mock(side_effect=["US", "US", "US", "US"])
                system.reboot = Mock()
                report = {}
                with self.assertRaisesRegex(ValueError, "地区接口与Android实际参数不一致: country"):
                    system.configure(DB, REGION, report, verify_only=verify_only)
                self.assertFalse(report["success"])
                writes = [call.args[0] for call in system.request.call_args_list if len(call.args) == 3]
                self.assertEqual(writes, [] if verify_only else ["country_set", "timezone_set", "language_set", "gps_inject"])

    def test_old_completion_without_persistent_gps_is_reconfigured_once(self):
        system = self.make_system()
        report = system.configure(DB, REGION, {}, previous={"expected": REGION, "country_api_confirmed": True})
        self.assertEqual(report["action"], "apply")
        self.assertTrue(report["gps_persisted"])
        system.proxy.session.post.assert_called_once()
        system.reboot.assert_called_once()

    def test_persistence_rejection_or_timeout_never_restarts_or_resubmits(self):
        for failure in (ValueError("rejected"), requests.Timeout()):
            with self.subTest(failure=type(failure).__name__):
                system = self.make_system()
                if isinstance(failure, requests.Timeout):
                    system.proxy.session.post.side_effect = failure
                else:
                    system.proxy.session.post.return_value.json.return_value = {"code": 0}
                report = {}
                with self.assertRaises(type(failure)):
                    system.configure(DB, REGION, report)
                self.assertFalse(report["success"])
                self.assertFalse(report["steps"][-1]["accepted"])
                system.proxy.session.post.assert_called_once()
                system.reboot.assert_not_called()

    def test_runtime_gps_match_cannot_hide_wrong_or_invalid_persisted_properties(self):
        for actual in ("33.3028", "", "nan"):
            with self.subTest(actual=actual):
                system = self.make_system()
                system.shell.return_value = actual
                system.shell.side_effect = None
                report = {}
                with self.assertRaisesRegex(ValueError, "GPS持久属性"):
                    system.configure(DB, REGION, report)
                self.assertFalse(report["success"])
                self.assertNotIn("gps_persisted", report)

    def test_failed_initial_region_query_still_blocks_configuration(self):
        proxy = Mock()
        system = SystemSetup(proxy)
        system.request = Mock(side_effect=ValueError("get_timezone_locale未确认成功"))
        report = {}
        with self.assertRaisesRegex(ValueError, "get_timezone_locale"):
            system.configure(DB, REGION, report)
        self.assertFalse(report["success"])
        system.request.assert_called_once_with("get_timezone_locale", DB)

    def test_region_validation_and_exit_geo_country_mismatch_block_writes(self):
        for fields in ({"country": "us"}, {"timezone": "US"}, {"language": "en;bad"},
                       {"latitude": float("nan")}, {"longitude": 181}):
            with self.assertRaises(ValueError):
                region_settings({"region": dict(REGION, **fields)})
        with self.assertRaises(ValueError):
            region_settings({"region": {k: v for k, v in REGION.items() if k != "longitude"}})
        system = self.make_system()
        region = {k: v for k, v in REGION.items() if k not in ("latitude", "longitude")}
        system.proxy.api.return_value = (200, {"code": 200, "data": {"ip": "198.51.100.1", "country": "US", "lat": 34.0522, "lon": -118.2437}})
        self.assertEqual(system.resolve(DB, {"region": region}, "198.51.100.1"), REGION)
        with self.assertRaises(ValueError):
            system.resolve(DB, {"region": region}, "198.51.100.2")
        system.proxy.api.return_value[1]["data"]["country"] = "CN"
        with self.assertRaises(ValueError):
            system.resolve(DB, {"region": region}, "198.51.100.1")
        self.assertTrue(all(c.args[0] == "GET" for c in system.proxy.api.call_args_list))

    def test_geo_country_name_uses_verified_country_code_and_same_ip(self):
        system = self.make_system()
        geo = {"ip": "198.51.100.1", "country": "United States", "lat": 34.0522, "lon": -118.2437}
        system.proxy.api.return_value = (200, {"code": 200, "data": geo})
        system.proxy.cloud_json.return_value = {"success": True, "ip": geo["ip"], "country_code": "US"}
        self.assertEqual(system.resolve(DB, {"region": REGION}, geo["ip"]), REGION)
        system.proxy.cloud_json.return_value["ip"] = "198.51.100.2"
        with self.assertRaises(ValueError):
            system.resolve(DB, {"region": REGION}, geo["ip"])

    def test_geo_transient_read_failure_is_retried_but_mismatch_is_not(self):
        system = self.make_system()
        geo = {"ip": "198.51.100.1", "country": "US", "lat": 34.0522, "lon": -118.2437}
        system.proxy.api.side_effect = [requests.Timeout(), (200, {"code": 500}),
                                        (200, {"code": 200, "data": geo})]
        with patch("setup_system.time.sleep"):
            self.assertEqual(system.resolve(DB, {"region": REGION}, geo["ip"]), REGION)
        self.assertEqual(system.proxy.api.call_count, 3)
        self.assertTrue(all(c.args[0] == "GET" for c in system.proxy.api.call_args_list))
        system.proxy.api.reset_mock(side_effect=True)
        system.proxy.api.return_value = (200, {"code": 200, "data": dict(geo, country="CN")})
        with self.assertRaises(ValueError):
            system.resolve(DB, {"region": REGION}, geo["ip"])
        self.assertEqual(system.proxy.api.call_count, 1)

    def test_reboot_timeout_response_is_not_reissued_and_boot_id_must_change(self):
        proxy = Mock()
        proxy.device.post_action.side_effect = requests.Timeout()
        system = SystemSetup(proxy)
        before = "11111111-1111-4111-8111-111111111111"
        after = "22222222-2222-4222-8222-222222222222"
        system.shell = Mock(side_effect=[before, after, "1"])
        system.snapshot = Mock(return_value=ACTUAL)
        report = {}
        with patch("setup_system.time.sleep"):
            system.reboot(DB, report)
        self.assertTrue(report["reboot_verified"])
        self.assertTrue(report["reboot_response_uncertain"])
        self.assertEqual(report["boot_id_before"], before)
        self.assertEqual(report["boot_id_after"], after)
        self.assertNotIn("cat /proc/uptime", [c.args[1] for c in system.shell.call_args_list])
        proxy.device.post_action.assert_called_once_with("reboot", [DB])

    def test_same_or_invalid_boot_id_and_unready_android_never_confirm_reboot(self):
        before = "11111111-1111-4111-8111-111111111111"
        after = "22222222-2222-4222-8222-222222222222"
        for observed, completed in ((before, "1"), ("", "1"), (after, "0")):
            with self.subTest(observed=observed, completed=completed):
                system = SystemSetup(Mock())
                system.proxy.device.post_action.return_value = {"code": 200}
                system.shell = Mock(side_effect=[before, observed, completed])
                system.snapshot = Mock()
                report = {}
                with patch("setup_system.time.sleep"), patch("setup_system.time.monotonic", side_effect=[0, 0, 1, 3]):
                    with self.assertRaisesRegex(ValueError, "未重发重启"):
                        system.reboot(DB, report, timeout=3)
                self.assertNotIn("reboot_verified", report)
                system.snapshot.assert_not_called()
                system.proxy.device.post_action.assert_called_once_with("reboot", [DB])
        system = SystemSetup(Mock())
        system.shell = Mock(return_value="")
        with self.assertRaisesRegex(ValueError, "重启前启动ID无效"):
            system.reboot(DB, {})
        system.proxy.device.post_action.assert_not_called()
        system = SystemSetup(Mock())
        system.proxy.device.post_action.return_value = {"code": 200}
        system.shell = Mock(side_effect=[before, after, "1"])
        system.snapshot = Mock(side_effect=ValueError("地区或GPS回查失败"))
        report = {}
        with patch("setup_system.time.sleep"), patch("setup_system.time.monotonic", side_effect=[0, 0, 1, 3]):
            with self.assertRaisesRegex(ValueError, "未重发重启"):
                system.reboot(DB, report, timeout=3)
        self.assertNotIn("reboot_verified", report)
        self.assertEqual(report["reboot_last_error"], "地区或GPS回查失败")


if __name__ == "__main__":
    unittest.main()
