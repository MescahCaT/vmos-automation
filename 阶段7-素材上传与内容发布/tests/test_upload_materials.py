import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import upload_materials
from upload_materials import (archive_file, load_live_names, parse_device_list, parse_task_list,
                               plan_pool_uploads, resolve_config_paths, resolve_instances, upload_file)


class UploadMaterialsTests(unittest.TestCase):
    def test_device_name_resolves_to_live_instance_id(self):
        with patch.object(upload_materials, "ProxySetup") as setup:
            setup.return_value.device.list_instances.return_value = {
                "EDGE10402": {"db_id": "EDGE10402", "user_name": "s104-02",
                               "state": "stopped", "http_api_port": 18042}}
            self.assertEqual(resolve_instances("http://192.0.2.10:18182", ["s104-02"]),
                             {"s104-02": {"db_id": "EDGE10402", "state": "stopped"}})

    def test_missing_live_device_reports_live_list(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "devices.txt"
            path.write_text("s104-02\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "实时云机列表.*s104-01"):
                parse_device_list(path, {"s104-01"})

    def test_missing_explicit_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.json"
            with self.assertRaises(FileNotFoundError):
                load_live_names(missing, missing, "192.0.2.10")

    def test_host_selects_its_manifest_config_and_log_paths(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(upload_materials, "ROOT", Path(temp)):
            root = Path(temp)
            host = "192.168.110.35"
            profile = root / "主机配置" / host
            profile.mkdir(parents=True)
            (profile / "config.json").write_text(json.dumps({
                "vmos": {"host_ip": host, "port": 18182}
            }), encoding="utf-8")
            (profile / "batch_create.json").write_text(json.dumps({
                "host_ip": host, "devices": [{"user_name": "other-01"}]
            }), encoding="utf-8")

            config_path, main_path, log_path, resolved_host = resolve_config_paths(host, None)

            self.assertIsNone(config_path)
            self.assertEqual(main_path, profile / "config.json")
            self.assertEqual(log_path, root / "日志" / host)
            self.assertEqual(resolved_host, host)
            with patch.object(upload_materials, "ProxySetup") as setup:
                setup.return_value.device.list_instances.return_value = {
                    "EDGE1": {"db_id": "EDGE1", "user_name": "proxy53.1", "state": "stopped"}}
                self.assertEqual(load_live_names(config_path, main_path, host), (host, {"proxy53.1"}))
            inventory = json.loads((root / "数据" / host / "device_inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(inventory["devices"][0]["db_id"], "EDGE1")

    def test_pool_assigns_distinct_first_level_supported_files(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "a.mp4").write_bytes(b"a")
            (folder / "b.PNG").write_bytes(b"b")
            (folder / "skip.txt").write_bytes(b"x")
            nested = folder / "nested"
            nested.mkdir()
            (nested / "c.jpg").write_bytes(b"c")

            assets, _ = plan_pool_uploads(folder, ["device-001", "device-002"], 1)

            self.assertEqual([asset["path"].name for asset in assets], ["a.mp4", "b.PNG"])
            self.assertEqual(len({asset["path"] for asset in assets}), 2)

    def test_pool_shortage_fails_during_planning(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "only.mp4").write_bytes(b"a")
            with self.assertRaisesRegex(ValueError, "素材不足"):
                plan_pool_uploads(Path(temp), ["device-001", "device-002"], 1)

    def test_task_folder_is_first_level_and_filters_extensions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "assets"
            folder.mkdir()
            (folder / "a.mp4").write_bytes(b"a")
            (folder / "b.jpeg").write_bytes(b"b")
            (folder / "ignore.gif").write_bytes(b"x")
            nested = folder / "nested"
            nested.mkdir()
            (nested / "ignore.png").write_bytes(b"x")
            task_file = root / "tasks.tsv"
            task_file.write_text(f"device-001\t{folder}\n", encoding="utf-8")

            assets, skipped = parse_task_list(task_file, {"device-001"})

            self.assertEqual([asset["path"].name for asset in assets], ["a.mp4", "b.jpeg"])
            self.assertEqual([item["file"] for item in skipped], ["ignore.gif"])

    def test_archive_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "clip.mp4"
            source.write_bytes(b"new")
            archive = Path(temp) / "已上传"
            archive.mkdir()
            (archive / "clip.mp4").write_bytes(b"old")

            result = archive_file(source)

            self.assertEqual(Path(result["path"]).name, "clip (1).mp4")
            self.assertEqual((archive / "clip.mp4").read_bytes(), b"old")
            self.assertEqual((archive / "clip (1).mp4").read_bytes(), b"new")

    def test_upload_disables_scan_and_checks_remote_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clip.mp4"
            path.write_bytes(b"data")
            asset = {"path": path, "size_bytes": 4, "mime_type": "video/mp4"}
            body = json.dumps({"code": 200, "data": {"files": [{
                "file_path": "/sdcard/DCIM/Codex/clip.mp4", "file_size": 4, "scanned": False
            }]}}).encode() + b"\n200"

            def fake_run(command, **kwargs):
                self.assertEqual(kwargs["stdin"].read(), b"data")
                return subprocess.CompletedProcess(command, 0, stdout=body, stderr=b"")

            with patch("upload_materials.subprocess.run", side_effect=fake_run) as run:
                result = upload_file("curl.exe", "http://127.0.0.1/api", asset,
                                     "/sdcard/DCIM/Codex/", 10)

            self.assertTrue(result["success"])
            command = run.call_args.args[0]
            self.assertIn("scan=false", command)
            self.assertIn("file=@-;filename=\"clip.mp4\";type=video/mp4", command)

            body = body.replace(b"/sdcard/DCIM/Codex/clip.mp4", b"/sdcard/Download/clip.mp4")
            with patch("upload_materials.subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=body, stderr=b"")):
                result = upload_file("curl.exe", "http://127.0.0.1/api", asset,
                                     "/sdcard/DCIM/Codex/", 10)
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
