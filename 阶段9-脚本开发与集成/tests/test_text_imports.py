"""在临时目录验证文本导入、绑定对应及失败保护；不访问 VMOS 或真实代理配置。"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from text_import_common import write_json_files


class TextImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vmos text imports ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.proxy_path = self.root / "proxy_list.json"
        self.config_path = self.root / "create.json"
        self.bindings_path = self.root / "bindings.json"
        self.proxies = {"metadata": "preserve", "proxies": [
            {"id": "shared", "protocol": "socks5", "host": "192.0.2.10", "port": 1080,
             "username": "old", "password": "old-secret", "enabled": True,
             "engine_type": 0, "transit": {"protocol": "vless", "host": "192.0.2.20", "port": 443,
                                          "uuid": "00000000-0000-4000-8000-000000000000",
                                          "network": "tcp", "tls": False, "enabled": True},
             "region": {"country": "US", "language": "en", "timezone": "America/Los_Angeles",
                        "latitude": 34.0, "longitude": -118.0}},
            {"id": "151", "host": "192.0.2.1", "port": 1080, "note": "preserve",
             "enabled": True, "region": {"country": "US", "language": "en", "timezone": "UTC"}},
            {"id": "152", "enabled": True}, {"id": "153", "enabled": True},
        ]}
        self.config = {"host_ip": "192.0.2.30", "count": 10,
                       "default_config": {"image_repository": "test-image", "adiID": 1039,
                                          "resolution": {"dpi": 420, "fps": 60, "height": 1920, "width": 1080},
                                          "dns": ["1.1.1.1"], "bool_gms_disabled": False},
                       "devices": [{"user_name": "old", "remark": "old"}],
                       "port_config": {"auto_allocate": False}, "custom": "preserve"}
        for path, document in ((self.proxy_path, self.proxies), (self.config_path, self.config),
                               (self.bindings_path, {"old": "shared"})):
            path.write_text(json.dumps(document), encoding="utf-8")

    def run_script(self, script, *arguments, stdin=None, success=True):
        result = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / script), *map(str, arguments)],
                                input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=15,
                                cwd=self.root)
        self.assertEqual(result.returncode, 0 if success else 1, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_proxy_merge_reimport_and_explicit_transit_template(self):
        source = self.root / "代理 清单.txt"
        source.write_text("# 中文说明\n151 192.0.2.151\t9820\taccount\tnew-secret\n154\t192.0.2.154 9820 user secret154\n", encoding="gb18030")
        before = self.proxy_path.read_bytes()
        output = self.run_script("import_proxies.py", source, "--output", self.proxy_path,
                                 "--template-proxy", "shared")
        result = json.loads(self.proxy_path.read_text(encoding="utf-8"))
        rows = {row["id"]: row for row in result["proxies"]}
        self.assertEqual(len(rows), 5)
        self.assertEqual(result["metadata"], "preserve")
        self.assertEqual(rows["shared"], self.proxies["proxies"][0])
        self.assertEqual(rows["151"]["note"], "preserve")
        self.assertEqual(rows["151"]["password"], "new-secret")
        self.assertEqual(rows["154"]["port"], 9820)
        self.assertEqual(rows["154"]["transit"], rows["shared"]["transit"])
        self.assertNotIn("latitude", rows["154"]["region"])
        self.assertEqual(next(self.root.glob("proxy_list.json.*.bak")).read_bytes(), before)
        self.assertNotIn("new-secret", output)
        self.assertNotIn("secret154", output)
        self.run_script("import_proxies.py", source, "--output", self.proxy_path)
        self.assertEqual(json.loads(self.proxy_path.read_text(encoding="utf-8")), result)

    def test_invalid_proxy_rows_do_not_write_or_expose_credentials(self):
        source = self.root / "invalid.txt"
        valid = "151 192.0.2.151 9820 user hidden-secret\n"
        for invalid in ("156130.94.203.28 9820 user hidden-secret", "154 192.0.2.999 9820 user hidden-secret",
                        "154 192.0.2.154 65536 user hidden-secret", "151 192.0.2.154 9820 user hidden-secret"):
            with self.subTest(invalid=invalid):
                source.write_text(valid + invalid, encoding="utf-8")
                before = self.proxy_path.read_bytes()
                output = self.run_script("import_proxies.py", source, "--output", self.proxy_path, success=False)
                self.assertIn("第 2 行", output)
                self.assertNotIn("hidden-secret", output)
                self.assertEqual(self.proxy_path.read_bytes(), before)
                self.assertEqual(list(self.root.glob("*.bak")), [])

    def test_new_proxy_defaults_and_interactive_quoted_utf16_path(self):
        source = self.root / "交互 输入.txt"
        source.write_text("155 192.0.2.155 1080 user secret\n", encoding="utf-16")
        self.run_script("import_proxies.py", "--output", self.proxy_path, stdin='"' + str(source) + '"\n')
        rows = json.loads(self.proxy_path.read_text(encoding="utf-8"))["proxies"]
        self.assertEqual(rows[-1], {"id": "155", "protocol": "socks5", "host": "192.0.2.155", "port": 1080,
                                    "username": "user", "password": "secret", "enabled": True,
                                    "engine_type": 0, "transit": self.proxies["proxies"][0]["transit"]})

    def test_last_transit_is_remembered_and_missing_transit_rejects_import(self):
        source = self.root / "proxies.txt"
        source.write_text("154 192.0.2.154 9820 user secret", encoding="utf-8")
        self.run_script("import_proxies.py", source, "--output", self.proxy_path)
        document = json.loads(self.proxy_path.read_text(encoding="utf-8"))
        self.assertEqual(document["default_transit"], self.proxies["proxies"][0]["transit"])
        # 即使代理组记录被清空，仍可复用上次记住的中转。
        document["proxies"] = []
        self.proxy_path.write_text(json.dumps(document), encoding="utf-8")
        self.run_script("import_proxies.py", source, "--output", self.proxy_path)
        self.assertEqual(json.loads(self.proxy_path.read_text(encoding="utf-8"))["proxies"][0]["transit"], document["default_transit"])
        self.proxy_path.write_text('{"proxies": []}', encoding="utf-8")
        before = self.proxy_path.read_bytes()
        output = self.run_script("import_proxies.py", source, "--output", self.proxy_path, success=False)
        self.assertIn("必须携带中转", output)
        self.assertEqual(self.proxy_path.read_bytes(), before)

    def test_devices_preserve_creation_parameters_and_generate_bindings(self):
        source = self.root / "names.txt"
        source.write_text("151.1\n151.2\n\n# 注释\n152.1\n152.2\n153.1\n", encoding="utf-8-sig")
        before = {path: path.read_bytes() for path in (self.config_path, self.bindings_path)}
        self.run_script("generate_devices.py", source, "--config", self.config_path,
                        "--bindings", self.bindings_path, "--proxies", self.proxy_path)
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        bindings = json.loads(self.bindings_path.read_text(encoding="utf-8"))
        names = ["151.1", "151.2", "152.1", "152.2", "153.1"]
        expected = copy.deepcopy(self.config)
        expected.update(count=5, devices=[{"user_name": name, "remark": name} for name in names])
        self.assertEqual(config, expected)
        self.assertEqual(bindings, {name: name.split(".")[0] for name in names})
        for path, original in before.items():
            self.assertEqual(next(self.root.glob(path.name + ".*.bak")).read_bytes(), original)
            self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        spec = importlib.util.spec_from_file_location("device_config", SCRIPTS.parent / "阶段2-云机批量创建与初始化" / "device_config.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loader = module.ConfigLoader(str(self.config_path))
        self.assertEqual(loader.get_count(), 5)
        for device in loader.get_devices():
            payload = module.get_create_payload(device["user_name"], device["remark"], loader.get_default_config())
            self.assertEqual(payload["adiID"], 1039)
            self.assertEqual(payload["user_name"], payload["remark"])

    def test_invalid_names_and_missing_or_disabled_proxy_preserve_both_outputs(self):
        source = self.root / "bad_names.txt"
        self.proxies["proxies"].append({"id": "disabled", "enabled": False})
        self.proxy_path.write_text(json.dumps(self.proxies), encoding="utf-8")
        for text in ("", "151.1\n151.1", "151.1\n999.1", "151.1\ndisabled.1", "151.1\n151", "151.1\n151.2 extra"):
            with self.subTest(text=text):
                source.write_text(text, encoding="utf-8")
                before = [p.read_bytes() for p in (self.config_path, self.bindings_path)]
                self.run_script("generate_devices.py", source, "--config", self.config_path,
                                "--bindings", self.bindings_path, "--proxies", self.proxy_path, success=False)
                self.assertEqual([p.read_bytes() for p in (self.config_path, self.bindings_path)], before)
                self.assertEqual(list(self.root.glob("*.bak")), [])

    def test_path_collision_and_missing_template_are_rejected(self):
        before = self.proxy_path.read_bytes()
        self.run_script("import_proxies.py", self.proxy_path, "--output", self.proxy_path, success=False)
        self.assertEqual(self.proxy_path.read_bytes(), before)
        source = self.root / "proxy.txt"
        source.write_text("154 192.0.2.154 9820 user secret", encoding="utf-8")
        self.run_script("import_proxies.py", source, "--output", self.proxy_path,
                        "--template-proxy", "missing", success=False)
        self.assertEqual(self.proxy_path.read_bytes(), before)
        source.write_text("151.1", encoding="utf-8")
        self.run_script("generate_devices.py", source, "--config", self.config_path,
                        "--bindings", self.config_path, "--proxies", self.proxy_path, success=False)
        self.assertEqual(list(self.root.glob("*.bak")), [])

    def test_second_file_failure_restores_first_file(self):
        before = [p.read_bytes() for p in (self.config_path, self.bindings_path)]
        replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated write failure")
            replace(source, target)

        with patch("text_import_common.os.replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                write_json_files({self.config_path: {"new": 1}, self.bindings_path: {"new": 2}})
        self.assertEqual([p.read_bytes() for p in (self.config_path, self.bindings_path)], before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_default_paths_and_both_interactive_prompts_in_isolated_project(self):
        root = self.root / "隔离项目"
        scripts = root / SCRIPTS.name
        scripts.mkdir(parents=True)
        for name in ("import_proxies.py", "generate_devices.py", "text_import_common.py"):
            shutil.copy2(SCRIPTS / name, scripts / name)
        relay = root / "阶段1-环境准备与配置" / "relay_proxy.py"
        relay.parent.mkdir()
        shutil.copy2(SCRIPTS.parent / "阶段1-环境准备与配置" / "relay_proxy.py", relay)
        proxy_path = root / "配置文件" / "proxy_list.json"
        proxy_path.parent.mkdir()
        proxy_path.write_text(json.dumps(self.proxies), encoding="utf-8")
        config_path = scripts / "batch10_create.example.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8-sig")
        source = self.root / "names and proxies.txt"
        for name, text in (("import_proxies.py", "151 192.0.2.151 9820 user secret"),
                           ("generate_devices.py", "151.1\n151.2")):
            source.write_text(text, encoding="utf-8")
            result = subprocess.run([sys.executable, "-X", "utf8", str(scripts / name)],
                                    input='"' + str(source) + '"\n', cwd=self.root, capture_output=True,
                                    text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proxy_rows = json.loads(proxy_path.read_text(encoding="utf-8"))["proxies"]
        self.assertTrue(next(row for row in proxy_rows if row["id"] == "151")["transit"])
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["count"], 2)
        self.assertEqual(json.loads((scripts / "batch10_bindings.example.json").read_text(encoding="utf-8")),
                         {"151.1": "151", "151.2": "151"})


if __name__ == "__main__":
    unittest.main()
