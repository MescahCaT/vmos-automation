import requests
import json
import subprocess
from typing import List, Dict
from pathlib import Path


class ApkUploader:
    """APK上传器"""

    def __init__(self, base_url: str, session=None):
        self.base_url = base_url
        self.session = session or requests.Session()
        self.session.trust_env = False

    def upload_local(self, apk_path: str, db_ids: List[str], timeout: int = 600) -> Dict:
        """本地文件批量上传安装"""
        apk = Path(apk_path).resolve()
        if not apk.is_file() or not apk.stat().st_size:
            raise FileNotFoundError(f"APK文件不存在: {apk_path}")
        if not db_ids:
            raise ValueError("未指定安装设备")
        print(f"上传文件: {apk.name} ({apk.stat().st_size / 1024 / 1024:.2f} MB)", flush=True)
        # 本机curl流式上传479MB文件，避免requests multipart一次读取整个APK。
        try:
            result = subprocess.run([
                "curl.exe", "--disable", "--noproxy", "*", "--silent", "--show-error",
                "--connect-timeout", "10", "--max-time", str(timeout),
                "--write-out", "\n%{http_code}", "--form-string", "db_ids=" + ",".join(db_ids),
                "--form", 'file=@"' + apk.name + '"',
                self.base_url + "/android_api/v1/upload_file_android_batch",
            ], cwd=apk.parent, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout + 10)
            if result.returncode:
                return {"success": False, "unconfirmed": True,
                        "error": f"上传请求异常（curl退出码{result.returncode}），需回查安装状态"}
            body, status = result.stdout.rsplit("\n", 1)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("上传响应不是对象")
            payload["http_status"] = int(status)
            payload["success"] = 200 <= int(status) < 300 and str(payload.get("code")) == "200"
            return payload
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return {"success": False, "unconfirmed": True, "error": type(error).__name__}

    def install_from_url(self, url: str, db_ids: List[str], timeout: int = 300) -> Dict:
        """URL批量安装"""
        payload = {
            'url': url,
            'db_ids': ','.join(db_ids)
        }

        try:
            resp = self.session.post(
                f"{self.base_url}/android_api/v1/install_apk_from_url_batch",
                json=payload,
                timeout=timeout
            )
            resp.raise_for_status()
            result = resp.json()
            if not isinstance(result, dict):
                raise ValueError("安装响应不是对象")
            result["http_status"] = resp.status_code
            result["success"] = str(result.get("code")) == "200"
            return result
        except (requests.RequestException, ValueError) as e:
            return {"success": False, "unconfirmed": True, "error": str(e)}
