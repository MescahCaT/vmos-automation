"""在临时目录替换业务入口，核验模板串联顺序、参数及失败停止；不连接VMOS。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "use_modules.ps1"
MODULES = {
    "阶段3-网络代理配置": "setup_proxy.py",
    "阶段5-TikTok应用安装": "install_tiktok.py",
    "阶段6-TikTok自动登录": "auto_login.py",
}
STUB = '''import json,os,sys
from pathlib import Path
with open(os.environ['TEST_MODULE_TRACE'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps([Path(__file__).name] + sys.argv[1:]) + '\\n')
if Path(__file__).name == os.environ.get('TEST_FAIL_MODULE'):
    raise SystemExit(2)
'''


class TemplateTests(unittest.TestCase):
    def run_template(self, *arguments, fail=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = root / "阶段9-脚本开发与集成" / SCRIPT.name
            runner.parent.mkdir()
            shutil.copy2(SCRIPT, runner)
            for phase, name in MODULES.items():
                target = root / phase / name
                target.parent.mkdir()
                target.write_text(STUB, encoding="utf-8")
            trace = root / "trace.jsonl"
            env = dict(os.environ, TEST_MODULE_TRACE=str(trace), TEST_FAIL_MODULE=fail or "",
                       VMOS_TIKTOK_PASSWORD="template-test-secret")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner), *arguments],
                cwd=root, env=env, capture_output=True, text=True, errors="replace", timeout=30,
            )
            calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []
            self.assertNotIn("template-test-secret", result.stdout + result.stderr + json.dumps(calls))
            return result, calls

    def test_default_check_is_read_only_and_single_target(self):
        result, calls = self.run_template()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c[0] for c in calls], list(MODULES.values()))
        self.assertTrue(all("--verify-only" in c for c in calls))
        self.assertTrue(all("EDGE5YDRZ1Q7D6TW" in c for c in calls))
        self.assertFalse(any("--start-stopped" in c for c in calls))

    def test_plan_does_not_execute_modules(self):
        result, calls = self.run_template("-Mode", "Run", "-Plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_prepare_reuses_region_sync_and_skips_login(self):
        result, calls = self.run_template("-Mode", "Prepare", "-DbId", "EDGE123456", "-ProxyId", "proxy-example")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c[0] for c in calls], ["setup_proxy.py", "install_tiktok.py"])
        self.assertTrue(all("EDGE123456" in c for c in calls))
        self.assertIn("--keep-running", calls[0])
        self.assertNotIn("--restore-state", calls[1])

    def test_failure_preserves_exit_code_and_stops_next_steps(self):
        result, calls = self.run_template("-Mode", "Run", fail="setup_proxy.py")
        self.assertEqual(result.returncode, 2)
        self.assertEqual([c[0] for c in calls], ["setup_proxy.py"])

    def test_run_forwards_login_options_without_password_value(self):
        result, calls = self.run_template("-Mode", "Run", "-AllowCurrentCode", "-CodeWait", "75")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c[0] for c in calls], list(MODULES.values()))
        self.assertIn("--password-env", calls[-1])
        self.assertIn("VMOS_TIKTOK_PASSWORD", calls[-1])
        self.assertIn("--allow-current-code", calls[-1])
        self.assertEqual(calls[-1][calls[-1].index("--code-wait") + 1], "75")


if __name__ == "__main__":
    unittest.main()
