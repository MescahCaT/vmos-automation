"""地区与GPS同步顺序、只读、重启回查、失败边界。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from setup_system import SystemSetup, region_settings


DB = "EDGE123456"
REGION = {"country": "US", "language": "en", "timezone": "America/Los_Angeles",
          "latitude": 34.0522, "longitude": -118.2437}
ACTUAL = {"country": "US", "locale": "en-US", "timezone": "America/Los_Angeles",
          "latitude": 34.0522, "longitude": -118.2437}


class SystemTests(unittest.TestCase):
    def make_system(self):
        proxy = Mock()
        proxy.api.return_value = (200, {"code": 200, "data": {}})
        system = SystemSetup(proxy)
        system.snapshot = Mock(return_value=dict(ACTUAL))
        system.reboot = Mock(side_effect=lambda db, report: report.update(reboot_verified=True))
        return system

    def test_country_timezone_language_gps_order_and_reboot_are_required_once(self):
        system = self.make_system()
        report = {}
        system.configure(DB, REGION, report)
        self.assertTrue(report["success"])
        self.assertTrue(report["reboot_verified"])
        self.assertFalse(report["sim_details_verified"])
        self.assertEqual([c.args[1] for c in system.proxy.api.call_args_list],
                         ["country_set", "timezone_set", "language_set", "gps_inject"])
        self.assertTrue(all(c.args[2] == DB for c in system.proxy.api.call_args_list))
        system.reboot.assert_called_once_with(DB, report)
        self.assertEqual(system.snapshot.call_count, 2)

    def test_recorded_matching_settings_skip_all_writes_and_verify_is_read_only(self):
        system = self.make_system()
        previous = {"expected": REGION, "country_api_confirmed": True}
        report = system.configure(DB, REGION, {}, previous=previous)
        self.assertEqual(report["action"], "already_configured")
        system.proxy.api.assert_not_called()
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

    def test_reboot_timeout_response_is_not_reissued_and_uptime_must_reset(self):
        proxy = Mock()
        proxy.device.post_action.side_effect = requests.Timeout()
        system = SystemSetup(proxy)
        system.shell = Mock(side_effect=["3000.0 0.0", "12.0 0.0", "1"])
        system.snapshot = Mock(return_value=ACTUAL)
        report = {}
        with patch("setup_system.time.sleep"):
            system.reboot(DB, report)
        self.assertTrue(report["reboot_verified"])
        self.assertTrue(report["reboot_response_uncertain"])
        proxy.device.post_action.assert_called_once_with("reboot", [DB])


if __name__ == "__main__":
    unittest.main()
