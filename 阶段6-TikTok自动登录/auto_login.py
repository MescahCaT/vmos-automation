"""单台 TikTok 邮箱验证码或密码登录；人机验证/未知页面留待人工处理。"""
import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from control_api import ControlAPI
from email_verifier import EmailVerifier

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "阶段3-网络代理配置"))
from setup_proxy import ProxySetup, SystemSetup, check_proxy, read_json, save_json, device_ids


class PendingLogin(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def labels(tree):
    return [value.strip() for node in tree.iter("node")
            for value in (node.get("text", ""), node.get("content-desc", "")) if value.strip()]


def find_label(tree, names):
    """优先可点击的精确节点，坐标取自当次页面，不硬编码屏幕尺寸。"""
    names = {name.casefold() for name in names}
    nodes = [node for node in tree.iter("node") if node.get("enabled") != "false" and any(
        value.strip().casefold() in names for value in (node.get("text", ""), node.get("content-desc", "")))]
    return next((node for node in nodes if node.get("clickable") == "true"), nodes[0] if nodes else None)


def page_kind(tree):
    text = "\n".join(labels(tree)).casefold()
    if ("retry" in text and any(word in text for word in ("something went wrong", "network is unstable", "unstable network"))) or any(word in text for word in ("no internet connection", "no network connection", "无网络连接", "无法连接网络")):
        return "app_unavailable"
    if any(word in text for word in ("drag the puzzle", "drag the slider", "verify to continue", "select all images", "captcha", "complete the puzzle", "滑块", "拼图", "完成验证", "拖动滑块")):
        return "captcha_required"
    if any(word in text for word in ("too many attempts", "too many requests", "try again later", "尝试次数过多", "操作太频繁")):
        return "rate_limited"
    if any(word in text for word in ("incorrect code", "invalid code", "code expired", "验证码错误", "验证码已过期")):
        return "code_rejected"
    if any(word in text for word in ("incorrect password", "wrong password", "password is incorrect", "密码错误")):
        return "password_rejected"
    if any(word in text for word in ("couldn't find your account", "account doesn't exist", "account not found", "找不到账号")):
        return "account_not_found"
    if any(word in text for word in ("when's your birthday", "when’s your birthday", "what's your date of birth", "birthday", "出生日期", "生日")):
        return "signup_required"
    if any(word in text for word in ("please try again or log in with a different method", "network is unstable", "unstable network", "network error", "no internet connection", "网络不稳定", "网络错误")):
        return "login_request_failed"
    if any(word in text for word in ("enter 6-digit code", "enter the 6-digit code", "enter verification code", "enter code", "enter the code sent to", "entering the code sent to", "6-digit code sent", "输入验证码", "输入6位验证码")):
        return "code_entry"
    if "verify it’s really you" in text or "verify it's really you" in text:
        return "identity_method"
    return "unknown"


def profile_evidence(tree):
    """自己的资料编辑控件与账号名同时出现，首页/浏览他人主页不算登录。"""
    names = labels(tree)
    edit = any(text.casefold() in ("edit profile", "set up profile", "编辑资料", "编辑个人资料", "完善个人资料") for text in names)
    modern_own_profile = {"edit", "profile menu", "private videos"}.issubset({text.casefold() for text in names})
    handle = next((text for text in names if re.fullmatch(r"@[A-Za-z0-9_.]{2,24}", text)), None)
    return {"own_profile": True, "username": handle} if (edit or modern_own_profile) and handle else None


class TikTokLogin:
    def __init__(self, config, output_dir=None, mapping_path=None):
        self.config = config
        self.host = f"http://{config['vmos']['host_ip']}:{int(config['vmos']['port'])}"
        self.output_dir = Path(output_dir) if output_dir else Path(__file__).resolve().parent
        self.proxy = ProxySetup(self.host, mapping_path=mapping_path)
        self.device = self.proxy.device
        self.mapping_path = self.proxy.mapping_path
        self.logs = []
        self.device.log = self.log
        self.ui = None
        self.report = {}
        self.sensitive_input = False

    def log(self, message):
        line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
        self.logs.append(line)
        print(line, flush=True)

    def save(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        filename = "login_verify" if self.report.get("mode") == "verify" else "login_status"
        save_json(self.output_dir / (filename + ".json"), self.report)
        (self.output_dir / ("login_verify_log.txt" if self.report.get("mode") == "verify" else "login_log.txt")).write_text("\n".join(self.logs) + "\n", encoding="utf-8")

    def snapshot(self, name, tree):
        # 密码或验证码输入阶段不保存截图/UI树，避免记录完整或逐位验证码。
        if self.sensitive_input or page_kind(tree) == "code_entry" or any(n.get("password") == "true" for n in tree.iter("node")):
            return
        path = self.output_dir / "screenshots" / (name + ".png")
        try:
            self.ui.screenshot(path)
            self.report.setdefault("screenshots", []).append(str(path))
        except (requests.RequestException, OSError, ValueError):
            self.report["screenshot_unavailable"] = True

    def read_page(self, navigating=False):
        tree = self.ui.dump()
        kind = page_kind(tree)
        self.report["page_kind"] = kind
        if kind in ("app_unavailable", "captcha_required", "rate_limited", "code_rejected", "password_rejected", "account_not_found", "signup_required", "login_request_failed") and not (navigating and kind == "login_request_failed"):
            self.snapshot(kind, tree)
            raise PendingLogin(kind, {
                "app_unavailable": "TikTok页面无法加载，联网或服务响应待确认；未继续提交账号",
                "captcha_required": "出现人机验证，请在客户端人工完成；未自动破解或继续提交",
                "rate_limited": "TikTok提示限频，请稍后再试；未重发验证码",
                "code_rejected": "TikTok拒绝验证码；未重发或反复提交",
                "password_rejected": "TikTok拒绝账号密码；未反复提交",
                "account_not_found": "TikTok未找到指定账号，未进入注册",
                "signup_required": "界面进入生日/注册流程，未创建新账号",
                "login_request_failed": "TikTok登录请求失败或网络不稳定，未反复提交",
            }[kind])
        return tree

    def click_and_wait(self, node):
        self.ui.click(node)
        time.sleep(1)

    def dismiss_info_prompt(self, tree):
        names = set(labels(tree))
        resources = {n.get("resource-id") for n in tree.iter("node")}
        prompt = None
        if (names == {"@2131893000", "Learn more"}
                and {"com.zhiliaoapp.musically:id/q7b", "com.zhiliaoapp.musically:id/q7c"} <= resources):
            prompt = "family_pairing"
        elif names == {"Your avatar, your style", "Create your TikTok avatar and bring it to chats, Stories, and more.", "Get started"}:
            prompt = "avatar_intro"
        if (prompt is None or prompt in self.report.get("dismissed_info_prompts", [])
                or any(n.get("class") == "android.widget.EditText" for n in tree.iter("node"))):
            return False
        self.ui.call("input/keyevent", {"key_code": 4})
        self.report.setdefault("dismissed_info_prompts", []).append(prompt)
        time.sleep(1)
        return True

    def navigate_to_email(self, timeout=60):
        deadline = time.monotonic() + timeout
        error_reset = False
        while True:
            tree = self.read_page(navigating=True)
            if time.monotonic() >= deadline:
                self.snapshot("unknown_page", tree)
                raise PendingLogin("unknown_page", "未识别到邮箱登录入口，请查看截图；未猜测坐标")
            evidence = profile_evidence(tree)
            if evidence:
                return tree, evidence
            if self.dismiss_info_prompt(tree):
                continue
            if page_kind(tree) == "login_request_failed":
                back = find_label(tree, ("Back to previous screen", "返回上一页"))
                if back is None or error_reset:
                    raise PendingLogin("stale_error_page", "上次登录错误未清除，未提交新邮箱")
                self.click_and_wait(back)
                error_reset = True
                continue
            email_tab = find_label(tree, ("Email / Username", "邮箱/用户名"))
            field = next((n for n in tree.iter("node") if n.get("class") == "android.widget.EditText" and n.get("password") != "true"
                          and not any(word in n.get("text", "").casefold() for word in ("phone number", "手机号"))
                          and (email_tab is not None or any(word in n.get("text", "").casefold() for word in ("email", "username", "邮箱", "用户名")))), None)
            if field is not None:
                return tree, None
            if page_kind(tree) == "code_entry":
                raise PendingLogin("existing_code_page", "已在验证码页，无法确认旧验证码发送时间；请返回邮箱输入页后重新运行")
            node = find_label(tree, ("Agree and continue", "同意并继续", "Skip", "跳过", "Not now", "暂不", "Don't allow", "不允许"))
            if node is None:
                node = find_label(tree, ("Use phone / email / username", "使用手机/邮箱/用户名", "Email / Username", "邮箱/用户名"))
            if node is None:
                node = find_label(tree, ("Profile", "个人主页", "我"))
            if node is None:
                node = find_label(tree, ("Log in", "登录"))
                if node is not None and node.get("clickable") != "true":
                    node = None
            if node is not None:
                self.click_and_wait(node)
            else:
                time.sleep(2)
            if time.monotonic() >= deadline:
                self.snapshot("unknown_page", tree)
                raise PendingLogin("unknown_page", "未识别到邮箱登录入口，请查看截图；未猜测坐标")

    def enter_email(self, email, tree):
        fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText" and n.get("password") != "true"]
        if len(fields) != 1:
            raise PendingLogin("unknown_page", "邮箱输入框不唯一，未输入")
        self.ui.fill(fields[0], email)
        self.report["email_entered"] = True
        self.log("邮箱已输入并回查确认")

    def send_code(self, mail, timeout=45):
        # Continue 在部分版本会直接发码，在其他版本只跳转密码页；基线须在它之前读取。
        previous = mail.fetch()
        self.report["baseline_read"] = True
        tree = self.ui.dump()
        node = find_label(tree, ("Continue", "Next", "继续", "下一步"))
        if node is None:
            raise PendingLogin("unknown_page", "未找到邮箱提交按钮")
        self.report["email_submitted"] = True
        self.click_and_wait(node)
        deadline = time.monotonic() + timeout
        clicked_code_login = False
        while True:
            tree = self.read_page()
            if page_kind(tree) == "code_entry":
                self.report.update(code_sent=True, code_sent_at=datetime.now().astimezone().isoformat(timespec="seconds"))
                self.log("已进入验证码页，等待网页返回新代码")
                return previous, tree
            node = find_label(tree, ("Log in with code", "Use code", "Use verification code", "Log in with a verification code", "Send code", "使用验证码登录", "验证码登录", "获取验证码", "发送验证码"))
            if node is not None and not clicked_code_login:
                # 明确发码按钮前再次读取基线；只点击一次，避免验证码发送限频。
                previous = mail.fetch()
                clicked_code_login = True
                self.report["code_request_clicked"] = True
                self.click_and_wait(node)
            elif time.monotonic() >= deadline:
                self.snapshot("send_code_unconfirmed", tree)
                raise PendingLogin("send_code_unconfirmed", "邮箱请求未进入验证码页；未重发请求")
            else:
                time.sleep(2)

    def input_code(self, code, tree):
        tree = self.read_page()
        if page_kind(tree) != "code_entry":
            raise PendingLogin("unknown_page", "当前页面已离开验证码输入页，未输入验证码")
        fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText" and n.get("enabled") != "false"]
        if not fields:
            raise PendingLogin("unknown_page", "未识别到验证码输入框")
        self.sensitive_input = True
        self.ui.click(fields[0])
        self.ui.text(code)
        self.report["code_entered"] = True
        self.log("验证码已输入；验证码未打印或保存")
        time.sleep(2)
        tree = self.read_page()
        if page_kind(tree) == "code_entry":
            node = find_label(tree, ("Log in", "Continue", "Next", "Verify", "登录", "继续", "下一步", "验证"))
            if node is not None:
                self.click_and_wait(node)

    def receive_code(self, mail, previous, timeout, allow_current=False):
        try:
            code = mail.wait_new(previous, timeout, log=self.log)
            self.report["fresh_code_received"] = True
            return code
        except ValueError:
            if not allow_current:
                raise
            code = mail.fetch()
            if code is None:
                raise ValueError("取码接口没有可用验证码") from None
            self.report.update(fresh_code_received=code != previous, provider_current_code_used=True,
                               provider_code_freshness_confirmed=code != previous)
            self.log("新码等待结束，按显式选项单次使用取码接口当前值；是否有效由TikTok验证")
            return code

    def login_with_password(self, mail, password, code_wait, allow_current=False, timeout=60):
        if not isinstance(password, str) or not password or re.search(r"[\r\n\0]", password):
            raise PendingLogin("password_required", "密码登录需要通过 --password-env 指定已设置的环境变量")
        self.sensitive_input = True
        previous = mail.fetch()
        tree = self.read_page()
        submit = find_label(tree, ("Continue", "Next", "继续", "下一步"))
        if submit is None:
            raise PendingLogin("unknown_page", "未找到邮箱提交按钮")
        self.report.update(email_submitted=True, login_method="password", baseline_read=True)
        self.click_and_wait(submit)
        deadline = time.monotonic() + timeout
        help_clicked = False
        while time.monotonic() < deadline:
            tree = self.read_page()
            fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText" and n.get("password") == "true"]
            if len(fields) == 1:
                self.ui.fill(fields[0], password)
                password = None
                tree = self.read_page()
                submit = find_label(tree, ("Continue", "Log in", "登录", "继续"))
                if submit is None:
                    raise PendingLogin("unknown_page", "未找到密码提交按钮")
                previous = mail.fetch()
                self.report["password_submitted"] = True
                self.click_and_wait(submit)
                self.log("密码已提交，核对身份验证与本人资料；密码未记录")
                break
            option = find_label(tree, ("Use password instead", "Log in with password", "使用密码登录"))
            if option is not None:
                self.click_and_wait(option)
                continue
            help_node = find_label(tree, ("Need help logging in?",))
            if help_node is not None and page_kind(tree) == "code_entry" and not help_clicked:
                self.click_and_wait(help_node)
                help_clicked = True
            else:
                time.sleep(2)
        else:
            raise PendingLogin("password_entry_unconfirmed", "未进入密码页，未猜测坐标或反复提交")
        deadline = time.monotonic() + timeout
        identity_selected = False
        while time.monotonic() < deadline:
            tree = self.read_page()
            if profile_evidence(tree):
                return
            kind = page_kind(tree)
            if kind == "identity_method":
                if identity_selected:
                    raise PendingLogin("identity_unconfirmed", "邮箱验证方式尚未跳转；未重复发送")
                choices = []
                for node in tree.iter("node"):
                    for value in (node.get("text", ""), node.get("content-desc", "")):
                        match = re.fullmatch(r"Email\s+([^\s@]+@[^\s@]+)", value.strip(), re.I)
                        if match:
                            pattern = re.escape(match[1]).replace(r"\*", "[^@]*")
                            if re.fullmatch(pattern, mail.email, re.I):
                                choices.append(node)
                                break
                if len(choices) != 1:
                    raise PendingLogin("identity_unconfirmed", "未找到与指定邮箱匹配的唯一验证方式")
                previous = mail.fetch()
                self.report["identity_email_requested"] = True
                self.click_and_wait(choices[0])
                identity_selected = True
                continue
            if kind == "code_entry":
                self.report.update(code_sent=True, additional_identity_verification=True)
                code = self.receive_code(mail, previous, code_wait, allow_current)
                self.input_code(code, self.read_page())
                code = previous = None
                return
            profile = find_label(tree, ("Profile", "个人主页", "我"))
            if profile is not None:
                self.click_and_wait(profile)
            else:
                time.sleep(2)
        raise PendingLogin("login_unconfirmed", "密码提交后未确认登录或额外验证页面")

    def verify_profile(self, timeout=60):
        deadline = time.monotonic() + timeout
        while True:
            tree = self.read_page()
            if time.monotonic() < deadline and self.dismiss_info_prompt(tree):
                continue
            evidence = profile_evidence(tree)
            if evidence:
                self.sensitive_input = False
                self.snapshot("logged_in_profile", tree)
                return evidence
            node = find_label(tree, ("Not now", "Skip", "暂不", "跳过", "Don't allow", "不允许", "Profile", "个人主页", "我"))
            if node is not None:
                self.click_and_wait(node)
            elif time.monotonic() >= deadline:
                raise PendingLogin("login_unconfirmed", "未看到本人的资料编辑入口和账号名，登录状态待确认")
            else:
                time.sleep(2)
            if time.monotonic() >= deadline:
                raise PendingLogin("login_unconfirmed", "未确认登录后的个人资料页")

    def run(self, db_id, email, start_stopped=False, keep_running=False, verify_only=False, code_wait=120,
            expected_username=None, login_mode="email_code", password=None, allow_current_code=False):
        if device_ids(db_id) != [db_id] or not 1 <= code_wait <= 600:
            raise ValueError("必须指定单台云机，验证码等待时间为1至600秒")
        if login_mode not in ("email_code", "password"):
            raise ValueError("登录方式必须为email_code或password")
        self.report = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"), "host": self.host,
                       "db_id": db_id, "account_email": email, "mode": "verify" if verify_only else "login",
                       "status": "pending", "success": False, "started_by_script": False}
        if expected_username:
            if not re.fullmatch(r"[A-Za-z0-9_.]{2,24}", expected_username.lstrip("@")):
                raise ValueError("期望的TikTok账号名无效")
            self.report["expected_username"] = "@" + expected_username.lstrip("@")
        self.ui = ControlAPI(self.host, db_id)
        stage = "preflight"
        try:
            mapping = read_json(self.mapping_path)
            entry = mapping.get("devices", {}).get(db_id)
            if mapping.get("host") != self.host or not isinstance(entry, dict):
                raise ValueError("设备映射主机或目标不匹配")
            if entry.get("account_email") not in (None, email):
                raise ValueError("设备已绑定其他账号，未切换账号")
            instance = self.device.list_instances().get(db_id)
            if not instance:
                raise ValueError("目标云机不存在")
            state = instance.get("state") or instance.get("status")
            self.report["original_state"] = state
            if state == "stopped" and start_stopped and not verify_only:
                self.report["started_by_script"] = True
                self.save()
                self.log("启动指定云机")
                try:
                    response = self.device.post_action("run", [db_id])
                    if str(response.get("code")) != "200":
                        raise ValueError("VMOS拒绝启动目标")
                except requests.RequestException:
                    self.log("启动响应待确认，回查ROM")
            elif state != "running":
                raise ValueError("云机未运行；登录可指定--start-stopped，只读验证需手动启动")
            if not self.device.wait_ready([db_id]):
                raise ValueError("ROM等待超时")
            if not self.device.verifier.verify_installed(db_id, self.config["tiktok"]["package_name"]):
                raise ValueError("TikTok安装未确认")
            if verify_only:
                evidence = profile_evidence(self.ui.dump())
                if not evidence or entry.get("account_email") != email or entry.get("tiktok_username") != evidence["username"]:
                    raise PendingLogin("login_unconfirmed", "当前页面或已验证账号绑定不足以确认，请进入本人资料页后回查")
            else:
                proxy_entries = read_json(ROOT / self.config["proxy"]["config_file"])["proxies"]
                selected = [p for p in proxy_entries if p.get("id") == entry.get("proxy_id")]
                if len(selected) != 1:
                    raise ValueError("目标云机未绑定唯一代理")
                stage = "proxy_preflight"
                local = check_proxy(selected[0], self.config["proxy"])
                verified = self.proxy.verify(db_id, local["exit_ip"], self.config["proxy"], 30, selected[0])
                self.report["proxy"] = verified
                if not verified["verified"]:
                    raise PendingLogin("proxy_unavailable", "云机代理出口或网页联网未确认，未执行登录")
                if selected[0].get("region") is not None:
                    stage = "system_preflight"
                    self.report["system"] = {}
                    system = SystemSetup(self.proxy)
                    region = system.resolve(db_id, selected[0], local["exit_ip"])
                    system.configure(db_id, region, self.report["system"], verify_only=True,
                                     previous=entry.get("system_config"))
                self.log("代理出口和配置的网页探测通过；TikTok应用尚未验证")
                stage = "ui_navigation"
                self.ui.call("activity/start", {"package_name": self.config["tiktok"]["package_name"]})
                time.sleep(2)
                tree, evidence = self.navigate_to_email()
                if evidence:
                    if entry.get("account_email") != email or entry.get("tiktok_username") != evidence["username"]:
                        raise PendingLogin("existing_account_unconfirmed", "已登录某账号，但无法确认是指定邮箱；未退出或切换")
                    time.sleep(2)
                    evidence = self.verify_profile()
                else:
                    self.enter_email(email, tree)
                    stage = "email_code"
                    mail = EmailVerifier(self.config["tiktok"]["email_code"], email)
                    if login_mode == "password":
                        self.login_with_password(mail, password, code_wait, allow_current_code)
                        password = None
                    else:
                        self.report["login_method"] = "email_code"
                        previous, code_tree = self.send_code(mail)
                        code = self.receive_code(mail, previous, code_wait, allow_current_code)
                        self.input_code(code, code_tree)
                        code = previous = None
                    stage = "profile_verification"
                    evidence = self.verify_profile()
            if expected_username and evidence["username"].casefold() != ("@" + expected_username.lstrip("@")).casefold():
                raise PendingLogin("account_mismatch", "登录后的TikTok账号名与本地指定账号不一致，未绑定设备")
            self.report.update(status="logged_in", success=True, verification=evidence,
                               verified_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            if not verify_only:
                latest = read_json(self.mapping_path)
                if latest.get("host") != self.host or latest["devices"][db_id].get("account_email") not in (None, email):
                    raise ValueError("设备账号绑定已发生变化，未覆盖")
                latest["devices"][db_id].update(account_email=email, tiktok_username=evidence["username"],
                                               login_verified_at=self.report["verified_at"])
                save_json(self.mapping_path, latest)
                self.report["mapping_updated"] = True
                self.log("指定账号登录与本人资料页已确认")
        except PendingLogin as error:
            self.report.update(status=error.status, error=str(error), success=False, stage=stage)
            self.log(str(error))
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            status = "proxy_unavailable" if stage == "proxy_preflight" else "system_unavailable" if stage == "system_preflight" else "mail_unavailable" if stage == "email_code" and type(error) is ValueError else "failed"
            self.report.update(status=status, success=False, stage=stage,
                               error=str(error) if type(error) is ValueError else type(error).__name__)
            self.log("本次未完成: " + self.report["error"])
        finally:
            password = None
            if self.report["started_by_script"] and not keep_running:
                restored = self.device.restore_stopped([db_id])
                self.report["restore_state"] = {"success": restored.get("success") is True}
                if not self.report["restore_state"]["success"]:
                    self.report.update(success=False, cleanup_failed=True)
            try:
                self.report["final_state"] = self.device.list_instances().get(db_id, {}).get("state", "unknown")
            except (requests.RequestException, ValueError):
                self.report["final_state"] = "unknown"
            self.log("完成: " + self.report["status"])
            self.save()
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-id", required=True)
    parser.add_argument("--email", required=True, help="必须存在于启用的本地账号列表")
    parser.add_argument("--config", type=Path, default=ROOT / "配置文件" / "config.json")
    parser.add_argument("--start-stopped", action="store_true")
    parser.add_argument("--keep-running", action="store_true", help="保留由脚本启动的云机，便于人工验证或调试")
    parser.add_argument("--verify-only", action="store_true", help="只确认当前本人资料页与已有账号绑定，不启动/导航/发码")
    parser.add_argument("--code-wait", type=int, default=120)
    parser.add_argument("--login-mode", choices=("email_code", "password"), help="默认使用账号配置中的方式")
    parser.add_argument("--password-env", help="存放TikTok密码的环境变量名；不在参数中传递密码")
    parser.add_argument("--allow-current-code", action="store_true", help="新码等待超时后，允许单次提交取码接口当前值；默认拒绝历史值")
    args = parser.parse_args()
    try:
        config = read_json(args.config)
        accounts = read_json(ROOT / config["tiktok"]["account_file"])["accounts"]
        selected = [a for a in accounts if a.get("email", "").casefold() == args.email.casefold()
                    and a.get("enabled", True) is True and a.get("login_mode") in ("email_code", "password")]
        if len(selected) != 1:
            raise ValueError("测试账号不存在、重复、未启用或登录方式不符")
        login_mode = args.login_mode or selected[0]["login_mode"]
        password_env = args.password_env or selected[0].get("password_env")
        password = os.environ.get(password_env) if password_env and not args.verify_only else None
        result = TikTokLogin(config).run(args.db_id, selected[0]["email"], args.start_stopped,
                                        args.keep_running, args.verify_only, args.code_wait, selected[0].get("username"),
                                        login_mode, password, args.allow_current_code)
        return 0 if result["success"] else 1 if result["status"] == "failed" or result.get("cleanup_failed") else 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error) if type(error) is ValueError else type(error).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
