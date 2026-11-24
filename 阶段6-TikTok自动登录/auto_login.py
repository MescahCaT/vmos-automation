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
from email_verifier import EmailVerifier, protected_session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "阶段3-网络代理配置"))
from setup_proxy import ProxySetup, read_json, save_json, device_ids, MAPPING_LOCK
from check_vmos_connection import poll_readback


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
    if any(word in text for word in ("when's your birthday", "when’s your birthday", "what's your date of birth", "birthday", "birthdate", "出生日期", "生日")):
        return "signup_required"
    if any(word in text for word in ("please try again or log in with a different method", "network is unstable", "unstable network", "network error", "no internet connection", "网络不稳定", "网络错误")):
        return "login_request_failed"
    if any(word in text for word in ("enter 6-digit code", "enter the 6-digit code", "enter verification code", "enter code", "enter the code sent to", "entering the code sent to", "6-digit code sent", "输入验证码", "输入6位验证码")):
        return "code_entry"
    if "verify it’s really you" in text or "verify it's really you" in text:
        return "identity_method"
    return "unknown"


def notification_permission(tree):
    packages = {n.get("package") for n in tree.iter("node") if n.get("package")}
    return (bool(packages) and packages <= {"com.android.permissioncontroller", "com.google.android.permissioncontroller"}
            and any(t.casefold() == "allow tiktok to send you notifications?" for t in labels(tree))
            and find_label(tree, ("Don't allow", "Don’t allow")) is not None)


def profile_evidence(tree):
    """自己的资料编辑控件与账号名同时出现，首页/浏览他人主页不算登录。"""
    names = labels(tree)
    edit = any(text.casefold() in ("edit profile", "set up profile", "编辑资料", "编辑个人资料", "完善个人资料") for text in names)
    modern_own_profile = {"edit", "profile menu", "private videos"}.issubset({text.casefold() for text in names})
    handles = {text for text in names if re.fullmatch(r"@[A-Za-z0-9_.]{2,24}", text)}
    handle = next(iter(handles)) if len(handles) == 1 else None
    return {"own_profile": True, "username": handle} if (edit or modern_own_profile) and handle else None


def login_entry_page(tree, package):
    names = {text.casefold() for text in labels(tree)}
    packages = {node.get("package") for node in tree.iter("node") if node.get("package")}
    has_login_title = bool(names & {"log in", "log in to tiktok", "登录"})
    has_login_method = bool(names & {"phone", "phone number", "email", "email / username", "username", "邮箱/用户名", "手机号"})
    return package in packages and has_login_title and has_login_method


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
        self.session_path = None
        self.session_copy_path = None
        self.session_state = None
        self.completion = "profile"

    def checkpoint(self, phase, previous=None):
        if self.session_path is None:
            return
        state = {"host": self.host, "db_id": self.report["db_id"],
                 "email": self.report["account_email"], "username": self.report["expected_username"],
                 "phase": phase, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        state["password_submitted"] = bool(self.report.get("password_submitted")
                                          or (self.session_state or {}).get("password_submitted"))
        state["additional_password_verification"] = bool(self.report.get("additional_password_verification")
                                                        or (self.session_state or {}).get("additional_password_verification"))
        state["password_submission_confirmed"] = bool(self.report.get("password_submission_confirmed")
                                                      or (self.session_state or {}).get("password_submission_confirmed"))
        state["previous"] = previous if phase != "code_submitted" else None
        payload = {"protected": protected_session(state)}
        save_json(self.session_path, payload)
        if self.session_copy_path is not None and self.session_copy_path != self.session_path:
            save_json(self.session_copy_path, payload)
        self.session_state = state

    def baseline(self, mail):
        return poll_readback(mail.fetch, self.log, "发码前邮箱基线读取", timeout=30)

    def clear_checkpoint(self):
        if self.session_path is not None:
            self.session_path.unlink(missing_ok=True)
        if self.session_copy_path is not None:
            self.session_copy_path.unlink(missing_ok=True)
        self.session_state = None

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
        packages = {n.get("package") for n in tree.iter("node") if n.get("package")}
        package = self.config["tiktok"]["package_name"]
        permission_packages = {"com.android.permissioncontroller", "com.google.android.permissioncontroller"}
        notification_prompt = notification_permission(tree)
        if packages and package not in packages and not (notification_prompt or navigating and packages <= permission_packages):
            raise PendingLogin("app_not_foreground", "当前页面不属于TikTok，未输入账号或验证码")
        kind = page_kind(tree)
        self.report["page_kind"] = kind
        if kind in ("app_unavailable", "captcha_required", "rate_limited", "code_rejected", "password_rejected", "account_not_found", "signup_required", "login_request_failed") and not (navigating and kind in ("login_request_failed", "signup_required")):
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
        try:
            self.ui.click(node)
        except (requests.RequestException, ValueError):
            self.log("点击响应未确认，后续只读检查页面；未重发点击")
            return False
        time.sleep(1)
        return True

    def password_submission_result(self, tree=None):
        self.report.update(password_submission_confirmed=True, password_completion_confirmed=True,
                           email_code_verified_before_password=True)
        self.sensitive_input = False
        self.log("邮箱验证码与密码提交已确认，按指定完成标准标记登录成功")
        package = self.config["tiktok"]["package_name"]
        try:
            tree = tree if tree is not None else self.ui.dump()
            if login_entry_page(tree, package):
                for _ in range(2):
                    self.ui.call("input/keyevent", {"key_code": 4})
                    time.sleep(0.5)
                    tree = self.ui.dump()
                    if not login_entry_page(tree, package):
                        break
                if login_entry_page(tree, package):
                    self.log("TikTok登录入口仍显示；已完成最多两次返回键尝试")
        except (requests.RequestException, ValueError):
            self.log("登录成功标准已达到，但返回登录页的界面操作未确认")
        return {"method": "email_code_and_password_submission",
                "username": self.report.get("expected_username")}

    def dismiss_info_prompt(self, tree):
        names = set(labels(tree))
        resources = {n.get("resource-id") for n in tree.iter("node")}
        if notification_permission(tree) and not self.report.get("notification_denied"):
            self.report["notification_denied"] = True
            self.click_and_wait(find_label(tree, ("Don't allow", "Don’t allow")))
            return True
        if (names == {"Swipe up", "Videos are personalized for you based on what you watch, like, and share.", "Start watching"}
                and not self.report.get("dismissed_start_watching")):
            self.report["dismissed_start_watching"] = True
            self.click_and_wait(find_label(tree, ("Start watching",)))
            return True
        consent = find_label(tree, ("Agree and continue",))
        if (consent is not None and "Dialog" in names
                and any(text.startswith("By tapping “Agree and continue”") for text in names)
                and not any(n.get("class") == "android.widget.EditText" for n in tree.iter("node"))
                and not self.report.get("accepted_terms_prompt")):
            self.report["accepted_terms_prompt"] = True
            self.click_and_wait(consent)
            return True
        prompt = None
        if (names == {"@2131893000", "Learn more"}
                and {"com.zhiliaoapp.musically:id/q7b", "com.zhiliaoapp.musically:id/q7c"} <= resources):
            prompt = "family_pairing"
        elif names in ({"Your avatar, your style", "Create your TikTok avatar and bring it to chats, Stories, and more.", "Get started"},
                       {"Create your TikTok avatar", "This selfie you’ll take, and data about your face, will be used to create and improve your avatar. You can delete this data at any time.", "Start"}):
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
        clicked = set()
        click_attempts = {}
        unchanged = 0
        signup_back = False
        account_choice_clicked = False
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
            if page_kind(tree) == "signup_required":
                choice = find_label(tree, ("Get more great features? Sign up", "Sign up", "注册"))
                if choice is not None and not account_choice_clicked:
                    account_choice_clicked = True
                    unchanged = 0
                    self.click_and_wait(choice)
                    continue
                if account_choice_clicked:
                    unchanged += 1
                    if unchanged >= 5:
                        raise PendingLogin("signup_required", "Sign up点击后仍未进入账号选择页；已回查5次，未重发点击或返回取消")
                    time.sleep(min(timeout / 5, max(0, deadline - time.monotonic())))
                    continue
                if signup_back:
                    raise PendingLogin("signup_required", "返回后仍在生日/注册页，未填写生日或创建账号")
                signup_back = True
                self.ui.call("input/keyevent", {"key_code": 4})
                time.sleep(1)
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
            node = find_label(tree, ("Already have an account? Log in", "Already have an account? Sign in",
                                     "已有账号？登录", "已有账号? 登录"))
            if node is None and any(t.casefold() == "sign up for tiktok" for t in labels(tree)):
                node = find_label(tree, ("Log in", "Sign in", "登录"))
            if node is None:
                node = find_label(tree, ("Use phone / email / username", "使用手机/邮箱/用户名", "Email / Username", "邮箱/用户名"))
            if node is None and not notification_permission(tree):
                node = find_label(tree, ("Agree and continue", "同意并继续", "Skip", "跳过", "Not now", "暂不", "Don't allow", "Don’t allow", "不允许"))
            if node is None:
                node = find_label(tree, ("Profile", "个人主页", "我"))
            if node is None:
                node = find_label(tree, ("Log in", "登录", "Already have an account? Log in", "已有账号？登录", "已有账号? 登录"))
                if node is not None and node.get("clickable") != "true":
                    node = None
            if node is not None:
                key = (node.get("text"), node.get("content-desc"), node.get("bounds"))
                label = (node.get("text") or node.get("content-desc") or "").strip().casefold()
                retryable_entry = label in ("use phone / email / username", "使用手机/邮箱/用户名")
                attempts = click_attempts.get(key, 0)
                if retryable_entry and attempts >= 5:
                    raise PendingLogin("unknown_page", "登录方式入口点击5次后仍停留在当前页；未输入账号")
                if key not in clicked or retryable_entry:
                    click_attempts[key] = attempts + 1
                    if attempts:
                        self.log(f"登录方式入口仍在当前页，重新点击：第{attempts + 1}/5次")
                    self.click_and_wait(node)
                    clicked.add(key)
                    unchanged = 0
                else:
                    unchanged += 1
                    time.sleep(2)
            else:
                unchanged += 1
                time.sleep(2)
            if unchanged >= 5 or time.monotonic() >= deadline:
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
        previous = self.baseline(mail)
        self.report["baseline_read"] = True
        tree = self.ui.dump()
        node = find_label(tree, ("Continue", "Next", "继续", "下一步"))
        if node is None:
            raise PendingLogin("unknown_page", "未找到邮箱提交按钮")
        self.report["email_submitted"] = True
        self.checkpoint("email_pending", previous)
        self.click_and_wait(node)
        deadline = time.monotonic() + timeout
        clicked_code_login = False
        for attempt in range(5):
            tree = self.read_page()
            if page_kind(tree) == "code_entry":
                self.report.update(code_sent=True, code_sent_at=datetime.now().astimezone().isoformat(timespec="seconds"))
                self.checkpoint("code_wait", previous)
                self.log("已进入验证码页，等待网页返回新代码")
                return previous, tree
            node = find_label(tree, ("Log in with code", "Use code", "Use verification code", "Log in with a verification code", "Send code", "使用验证码登录", "验证码登录", "获取验证码", "发送验证码"))
            if node is not None and not clicked_code_login:
                # 明确发码按钮前再次读取基线；只点击一次，避免验证码发送限频。
                previous = self.baseline(mail)
                clicked_code_login = True
                self.report["code_request_clicked"] = True
                self.checkpoint("code_wait", previous)
                self.click_and_wait(node)
            elif attempt == 4 or time.monotonic() >= deadline:
                self.snapshot("send_code_unconfirmed", tree)
                raise PendingLogin("send_code_unconfirmed", "邮箱请求未进入验证码页；未重发请求")
            else:
                time.sleep(min(timeout / 5, max(0, deadline - time.monotonic())))
        raise PendingLogin("send_code_unconfirmed", "邮箱请求未进入验证码页；未重发请求")

    def input_code(self, code, tree):
        tree = self.read_page()
        if page_kind(tree) != "code_entry":
            raise PendingLogin("unknown_page", "当前页面已离开验证码输入页，未输入验证码")
        for address in re.findall(r"[A-Za-z0-9_.*+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", " ".join(labels(tree))):
            pattern = re.escape(address).replace(r"\*", "[^@]*")
            if not re.fullmatch(pattern, self.report.get("account_email", ""), re.I):
                raise PendingLogin("identity_unconfirmed", "验证码页的邮箱与指定账号不匹配，未输入")
        fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText" and n.get("enabled") != "false"]
        if not fields:
            raise PendingLogin("unknown_page", "未识别到验证码输入框")
        self.sensitive_input = True
        try:
            self.ui.click(fields[0])
        except (requests.RequestException, ValueError):
            raise PendingLogin("code_focus_unconfirmed", "验证码输入框焦点未确认，未盲目输入") from None
        self.checkpoint("code_submitted")
        self.report["code_entered"] = True
        self.save()
        try:
            self.ui.text(code)
        except (requests.RequestException, ValueError):
            self.log("验证码输入响应未确认，检查实际页面；未重复输入")
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

    def submit_password(self, tree, mail, password):
        fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText" and n.get("password") == "true"]
        if len(fields) != 1:
            raise PendingLogin("password_entry_unconfirmed", "密码输入框不唯一，未输入")
        if not isinstance(password, str) or not password or re.search(r"[\r\n\0]", password):
            raise PendingLogin("password_required", "需要指定账号的TikTok密码以完成身份验证")
        self.sensitive_input = True
        self.ui.fill(fields[0], password)
        self.report["password_entered"] = True
        tree = self.read_page()
        submit = find_label(tree, ("Continue", "Log in", "Verify", "Next", "登录", "继续", "验证", "下一步"))
        if submit is None:
            raise PendingLogin("unknown_page", "未找到密码提交按钮")
        previous = self.baseline(mail)
        self.report["password_submission_requested"] = True
        self.report["password_submitted"] = True
        self.checkpoint("password_pending", previous)
        if not self.click_and_wait(submit):
            raise PendingLogin("password_submit_unconfirmed", "密码提交请求响应未确认；未重复点击")
        self.report["password_submission_confirmed"] = True
        self.checkpoint("password_pending", previous)
        self.log("密码提交请求已被TikTok界面控制接口接受；密码未记录")
        return previous

    def login_with_password(self, mail, password, code_wait, allow_current=False, timeout=60):
        if not isinstance(password, str) or not password or re.search(r"[\r\n\0]", password):
            raise PendingLogin("password_required", "密码登录需要通过 --password-env 指定已设置的环境变量")
        self.sensitive_input = True
        previous = self.baseline(mail)
        tree = self.read_page()
        submit = find_label(tree, ("Continue", "Next", "继续", "下一步"))
        if submit is None:
            raise PendingLogin("unknown_page", "未找到邮箱提交按钮")
        self.report.update(email_submitted=True, login_method="password", baseline_read=True)
        self.checkpoint("email_pending", previous)
        self.click_and_wait(submit)
        deadline = time.monotonic() + timeout
        help_clicked = False
        password_option_clicked = False
        for attempt in range(5):
            tree = self.read_page()
            fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText" and n.get("password") == "true"]
            if len(fields) == 1:
                previous = self.submit_password(tree, mail, password)
                password = None
                break
            option = find_label(tree, ("Use password instead", "Log in with password", "使用密码登录"))
            if option is not None and not password_option_clicked:
                self.click_and_wait(option)
                password_option_clicked = True
                continue
            help_node = find_label(tree, ("Need help logging in?",))
            if help_node is not None and page_kind(tree) == "code_entry" and not help_clicked:
                self.click_and_wait(help_node)
                help_clicked = True
            else:
                if attempt < 4:
                    time.sleep(min(timeout / 5, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                raise PendingLogin("password_entry_unconfirmed", "未进入密码页，未重复提交")
        else:
            raise PendingLogin("password_entry_unconfirmed", "未进入密码页，未猜测坐标或反复提交")
        deadline = time.monotonic() + timeout
        identity_selected = False
        profile_clicked = False
        for attempt in range(5):
            tree = self.read_page()
            if profile_evidence(tree):
                return
            if self.dismiss_info_prompt(tree):
                continue
            kind = page_kind(tree)
            if kind == "identity_method":
                if identity_selected:
                    if attempt == 4 or time.monotonic() >= deadline:
                        raise PendingLogin("identity_unconfirmed", "邮箱验证方式尚未跳转；未重复发送")
                    time.sleep(min(timeout / 5, max(0, deadline - time.monotonic())))
                    continue
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
                previous = self.baseline(mail)
                self.report["identity_email_requested"] = True
                self.checkpoint("code_wait", previous)
                self.click_and_wait(choices[0])
                identity_selected = True
                continue
            if kind == "code_entry":
                self.report.update(code_sent=True, additional_identity_verification=True)
                self.checkpoint("code_wait", previous)
                code = self.receive_code(mail, previous, code_wait, allow_current)
                self.input_code(code, self.read_page())
                code = previous = None
                return
            profile = find_label(tree, ("Profile", "个人主页", "我"))
            if profile is not None and not profile_clicked:
                self.click_and_wait(profile)
                profile_clicked = True
            elif attempt < 4:
                time.sleep(min(timeout / 5, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
        raise PendingLogin("login_unconfirmed", "密码提交后未确认登录或额外验证页面")

    def verify_profile(self, timeout=60, mail=None, password=None):
        deadline = time.monotonic() + timeout
        clicked = set()
        state = self.session_state or {}
        password_was_submitted = bool(self.report.get("password_submitted") or state.get("password_submitted"))
        password_submission_confirmed = bool(self.report.get("password_submission_confirmed")
                                             or state.get("password_submission_confirmed"))
        password_verification_followed_code = bool(self.report.get("additional_password_verification")
                                                   or state.get("additional_password_verification"))
        initial_tree = None
        if self.completion == "password-transition" and password_was_submitted and password_verification_followed_code:
            if password_submission_confirmed:
                return self.password_submission_result()
            try:
                current = self.read_page()
            except (requests.RequestException, ValueError):
                current = None
            if current is not None and login_entry_page(current, self.config["tiktok"]["package_name"]):
                self.report["password_submission_confirmed"] = True
                self.report["password_submission_inferred_from_login_entry"] = True
                return self.password_submission_result(current)
            initial_tree = current
        password_after_code = mail is not None and (
            self.report.get("code_entered") or (self.session_state or {}).get("phase") == "code_submitted")
        password_after_code = password_after_code and not (
            self.report.get("password_submitted") or (self.session_state or {}).get("password_submitted")
            or (self.session_state or {}).get("phase") == "password_pending")
        for attempt in range(5):
            tree = initial_tree if attempt == 0 and initial_tree is not None else self.read_page()
            if time.monotonic() < deadline and self.dismiss_info_prompt(tree):
                continue
            evidence = profile_evidence(tree)
            if evidence:
                self.sensitive_input = False
                self.snapshot("logged_in_profile", tree)
                return evidence
            if password_after_code:
                method = find_label(tree, ("Password", "密码")) if page_kind(tree) == "identity_method" else None
                fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText" and n.get("password") == "true"]
                if method is not None or fields:
                    self.report["additional_password_verification"] = True
                    if not isinstance(password, str) or not password:
                        raise PendingLogin("password_required", "邮箱验证码后要求密码验证，请提供该账号的TikTok密码")
                    if not fields:
                        self.click_and_wait(method)
                        def password_page():
                            current = self.read_page()
                            fields = [n for n in current.iter("node") if n.get("class") == "android.widget.EditText" and n.get("password") == "true"]
                            if len(fields) != 1:
                                raise ValueError("密码验证输入框尚未唯一确认")
                            return current
                        tree = poll_readback(password_page, self.log, "密码验证页面确认", timeout=30)
                    self.submit_password(tree, mail, password)
                    password = None
                    if self.completion == "password-transition":
                        return self.password_submission_result()
                    return self.verify_profile(timeout=timeout)
            node = None if notification_permission(tree) else find_label(tree, (
                "Not now", "Skip", "暂不", "跳过", "Don't allow", "Don’t allow", "不允许", "Profile", "个人主页", "我"))
            key = (node.get("text"), node.get("content-desc"), node.get("bounds")) if node is not None else None
            if node is not None and key not in clicked:
                self.click_and_wait(node)
                clicked.add(key)
            elif time.monotonic() >= deadline:
                raise PendingLogin("login_unconfirmed", "未看到本人的资料编辑入口和账号名，登录状态待确认")
            else:
                if attempt < 4:
                    time.sleep(min(timeout / 5, max(0, deadline - time.monotonic())))
            if attempt == 4 or time.monotonic() >= deadline:
                raise PendingLogin("login_unconfirmed", "未确认登录后的个人资料页")

    def resume_login(self, mail, code_wait, allow_current, password=None):
        state = self.session_state
        if state is None:
            raise PendingLogin("resume_unavailable", "没有可验证的登录续跑状态；未重新发码")
        if (self.completion == "password-transition" and state.get("phase") == "password_pending"
                and state.get("password_submitted") is True and state.get("additional_password_verification") is True
                and state.get("password_submission_confirmed") is True):
            return self.password_submission_result()
        self.sensitive_input = True
        tree = self.read_page(navigating=True)
        if page_kind(tree) == "signup_required" or any(t.casefold() == "sign up for tiktok" for t in labels(tree)):
            tree, evidence = self.navigate_to_email()
            self.report["login_entry_recovered"] = True
            if evidence:
                return self.verify_profile()
            raise PendingLogin("resume_restart_required",
                               "已从生日页经Sign up→Log in进入邮箱登录页；原流程已提交过登录，未重复输入或发送，请明确重新登录")
        if (self.completion == "password-transition" and state.get("phase") == "password_pending"
                and state.get("password_submitted") is True and page_kind(tree) == "code_entry"
                and state.get("password_submission_confirmed") is not True):
            raise PendingLogin("resume_unconfirmed", "密码提交响应待确认且当前又出现验证码页；未读取或再次提交验证码")
        if profile_evidence(tree) or state["phase"] == "code_submitted" or self.dismiss_info_prompt(tree):
            return self.verify_profile(mail=mail, password=password)
        if state["phase"] == "password_pending" and page_kind(tree) != "code_entry":
            return self.verify_profile()
        if page_kind(tree) != "code_entry":
            raise PendingLogin("resume_unconfirmed", "当前未处于验证码页或本人主页；未重新提交账号或发码")
        if state["phase"] not in ("email_pending", "password_pending", "code_wait"):
            raise PendingLogin("resume_unconfirmed", "续跑阶段无法确认，未输入验证码")
        code = self.receive_code(mail, state.get("previous"), code_wait, allow_current)
        self.input_code(code, tree)
        code = None
        return self.verify_profile(mail=mail, password=password)

    def run(self, db_id, email, start_stopped=False, keep_running=False, verify_only=False, code_wait=120,
            expected_username=None, login_mode="email_code", password=None, allow_current_code=False, resume=False, restart_login=False,
            completion="profile"):
        if device_ids(db_id) != [db_id]:
            raise ValueError("必须指定单台云机")
        lock_dir = self.mapping_path.parent / "login-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        # ponytail: Windows文件锁只防同一设备的登录进程重叠，关闭进程自动释放。
        import msvcrt
        with (lock_dir / (db_id + ".lock")).open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("同一云机已有登录进程，请等待原任务结束") from None
            try:
                return self._run(db_id, email, start_stopped, keep_running, verify_only, code_wait,
                                 expected_username, login_mode, password, allow_current_code, resume, restart_login, completion)
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _run(self, db_id, email, start_stopped=False, keep_running=False, verify_only=False, code_wait=120,
             expected_username=None, login_mode="email_code", password=None, allow_current_code=False, resume=False, restart_login=False,
             completion="profile"):
        if completion not in ("profile", "password-transition") or verify_only and completion != "profile":
            raise ValueError("完成标准无效，只读核验必须使用profile")
        if completion == "password-transition" and login_mode != "email_code":
            raise ValueError("密码后跳转终点必须使用email_code登录")
        self.completion = completion
        keep_running = keep_running or completion == "password-transition"
        if device_ids(db_id) != [db_id] or not 1 <= code_wait <= 600:
            raise ValueError("必须指定单台云机，验证码等待时间为1至600秒")
        if login_mode not in ("email_code", "password"):
            raise ValueError("登录方式必须为email_code或password")
        if resume and (verify_only or restart_login) or verify_only and restart_login:
            raise ValueError("续跑、重新登录和只读核验不能同时使用")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("邮箱地址无效")
        self.report = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"), "host": self.host,
                       "db_id": db_id, "account_email": email, "mode": "verify" if verify_only else "login",
                       "status": "pending", "success": False, "started_by_script": False, "completion": completion}
        if expected_username:
            if not re.fullmatch(r"[A-Za-z0-9_.]{2,24}", expected_username.lstrip("@")):
                raise ValueError("期望的TikTok账号名无效")
            self.report["expected_username"] = "@" + expected_username.lstrip("@")
        self.ui = ControlAPI(self.host, db_id, proxy=self.proxy)
        self.session_path = None if verify_only else self.mapping_path.parent / "login-locks" / ("login_session-" + db_id + ".json")
        self.session_copy_path = None if verify_only else self.output_dir / ("login_session-" + db_id + ".json")
        self.session_state = None
        stage = "preflight"
        try:
            with MAPPING_LOCK:
                mapping = read_json(self.mapping_path)
            entry = mapping.get("devices", {}).get(db_id)
            if mapping.get("host") != self.host or not isinstance(entry, dict):
                raise ValueError("设备映射主机或目标不匹配")
            if entry.get("account_email") is not None and entry["account_email"].casefold() != email.casefold():
                raise ValueError("设备已绑定其他账号，未切换账号")
            if expected_username is None:
                expected_username = entry.get("tiktok_username")
            if not expected_username or not re.fullmatch(r"@?[A-Za-z0-9_.]{2,24}", expected_username):
                raise ValueError("账号必须配置有效username，不能仅凭邮箱输入认定账号身份")
            self.report["expected_username"] = "@" + expected_username.lstrip("@")
            saved_path = self.session_path if not verify_only and self.session_path.exists() else self.session_copy_path
            if not verify_only and saved_path.exists() and not restart_login:
                self.session_state = protected_session(token=read_json(saved_path).get("protected"))
                saved = self.session_state
                if (not isinstance(saved, dict) or saved.get("host") != self.host or saved.get("db_id") != db_id
                        or saved.get("email", "").casefold() != email.casefold() or saved.get("username") != self.report["expected_username"]):
                    raise PendingLogin("resume_unconfirmed", "续跑状态与主机、云机或账号不一致，未提交登录")
                if saved_path != self.session_path:
                    save_json(self.session_path, read_json(saved_path))
                if not resume:
                    raise PendingLogin("resume_required", "存在未完成登录，请加 --resume 只续接原流程，避免重复发码")
                age = (datetime.now().astimezone() - datetime.fromisoformat(saved["updated_at"])).total_seconds()
                submitted_password = (saved.get("phase") == "password_pending" and saved.get("password_submitted") is True
                                      and saved.get("additional_password_verification") is True)
                if age < 0 or (age > 600 and not (completion == "password-transition" and submitted_password)):
                    raise PendingLogin("resume_expired", "续跑状态时间无效或已超过10分钟；请人工确认页面后重新发起登录")
                if age > 600 and submitted_password:
                    self.report["expired_password_checkpoint_read_only"] = True
            elif resume and not verify_only:
                raise PendingLogin("resume_unavailable", "没有可验证的续跑记录，未提交登录")
            instances = poll_readback(self.device.list_instances, self.log, "登录前实例查询")
            instance = instances.get(db_id)
            if not instance:
                raise ValueError("目标云机不存在")
            if any(i != db_id and i in instances and isinstance(v, dict) and isinstance(v.get("account_email"), str)
                   and v["account_email"].casefold() == email.casefold() for i, v in mapping["devices"].items()):
                raise ValueError("账号已绑定其他实际存在的云机，未重复登录")
            actual_name = instance.get("user_name") or instance.get("name")
            if entry.get("name") and actual_name != entry["name"]:
                raise ValueError("实例名称与本地映射不符，未操作云机")
            if not verify_only:
                proxy_entries = read_json(ROOT / self.config["proxy"]["config_file"])["proxies"]
                selected = [p for p in proxy_entries if p.get("id") == entry.get("proxy_id")]
                if len(selected) != 1:
                    raise ValueError("目标云机未绑定唯一代理，未启动或提交登录")
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
                except (requests.RequestException, ValueError):
                    self.log("启动响应待确认，回查ROM")
            elif state != "running":
                raise ValueError("云机未运行；登录可指定--start-stopped，只读验证需手动启动")
            if not self.device.wait_ready([db_id]):
                raise ValueError("ROM等待超时")
            def installed():
                if not self.device.verifier.verify_installed(db_id, self.config["tiktok"]["package_name"]):
                    raise ValueError("TikTok安装未确认")
            poll_readback(installed, self.log, "TikTok包名确认")
            if verify_only:
                evidence = profile_evidence(self.ui.dump())
                if not evidence or entry.get("account_email") != email or entry.get("tiktok_username") != evidence["username"]:
                    raise PendingLogin("login_unconfirmed", "当前页面或已验证账号绑定不足以确认，请进入本人资料页后回查")
            else:
                stage = "ui_navigation"
                self.report["proxy"] = {"verified": False, "verification_requested": False, "skipped": True}
                self.report["system"] = {"success": False, "verification_requested": False,
                                         "settings_attempted": False, "skipped": True}
                state = self.session_state or {}
                password_checkpoint_resume = (resume and state.get("phase") == "password_pending"
                    and state.get("password_submitted") is True and state.get("additional_password_verification") is True)
                if password_checkpoint_resume:
                    self.log("续接已提交的密码验证，只读处理登录状态；跳过代理预检和账号重提")
                else:
                    self.log("已跳过代理状态、出口IP、网页联网、国家/时区/语言及GPS检测，直接进入TK登录")
                    try:
                        current = self.ui.dump()
                        packages = {n.get("package") for n in current.iter("node") if n.get("package")}
                    except (requests.RequestException, ValueError):
                        current = None
                        packages = set()
                    if self.config["tiktok"]["package_name"] not in packages and not (current is not None and notification_permission(current)):
                        try:
                            self.ui.call("activity/start", {"package_name": self.config["tiktok"]["package_name"]})
                        except (requests.RequestException, ValueError):
                            self.log("打开应用响应不明确，只读确认页面；未重发启动应用请求")
                        time.sleep(2)
                    def foreground():
                        tree = self.ui.dump()
                        packages = {n.get("package") for n in tree.iter("node") if n.get("package")}
                        if packages and self.config["tiktok"]["package_name"] not in packages and not notification_permission(tree):
                            raise ValueError("TikTok尚未进入前台")
                    poll_readback(foreground, self.log, "TikTok前台页面确认", timeout=30)
                if resume:
                    stage = "email_code"
                    mail = EmailVerifier(self.config["tiktok"]["email_code"], email)
                    evidence = self.resume_login(mail, code_wait, allow_current_code, password=password)
                else:
                    tree, evidence = self.navigate_to_email()
                if not resume and evidence:
                    if str(entry.get("account_email", "")).casefold() != email.casefold() or entry.get("tiktok_username") != evidence["username"]:
                        raise PendingLogin("existing_account_unconfirmed", "已登录某账号，但无法确认是指定邮箱；未退出或切换")
                    time.sleep(2)
                    evidence = self.verify_profile()
                elif not resume:
                    if restart_login:
                        fields = [n for n in tree.iter("node") if n.get("class") == "android.widget.EditText" and n.get("password") != "true"]
                        if len(fields) != 1:
                            raise PendingLogin("resume_unconfirmed", "重新登录须先人工返回邮箱输入页")
                        self.clear_checkpoint()
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
                    evidence = self.verify_profile(mail=mail, password=password)
            if expected_username and evidence["username"].casefold() != ("@" + expected_username.lstrip("@")).casefold():
                raise PendingLogin("account_mismatch", "登录后的TikTok账号名与本地指定账号不一致，未绑定设备")
            self.report.update(status="logged_in", success=True, verification=evidence,
                               verified_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            if not verify_only:
                with MAPPING_LOCK:
                    latest = read_json(self.mapping_path)
                    bound = latest.get("devices", {}).get(db_id, {}).get("account_email")
                    if latest.get("host") != self.host or bound is not None and bound.casefold() != email.casefold():
                        raise ValueError("设备账号绑定已发生变化，未覆盖")
                    latest["devices"][db_id].update(account_email=email, tiktok_username=evidence["username"],
                                                   login_verified_at=self.report["verified_at"])
                    save_json(self.mapping_path, latest)
                self.clear_checkpoint()
                self.report["mapping_updated"] = True
                if evidence.get("method") == "email_code_and_password_submission":
                    self.log("指定账号的邮箱验证码与密码提交步骤已确认")
                else:
                    self.log("指定账号登录与本人资料页已确认")
        except PendingLogin as error:
            self.report.update(status=error.status, success=False, stage=stage)
            if error.status == "password_transition_confirmed" and self.report.get("password_transition_confirmed"):
                self.report.update(task_completed=True, message=str(error))
            else:
                self.report["error"] = str(error)
            self.log(str(error))
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            status = "mail_unavailable" if stage == "email_code" and type(error) is ValueError else "failed"
            self.report.update(status=status, success=False, stage=stage,
                               error=str(error) if type(error) is ValueError else type(error).__name__)
            self.log("本次未完成: " + self.report["error"])
        finally:
            password = None
            try:
                if self.report["started_by_script"] and not keep_running:
                    restored = self.device.restore_stopped([db_id])
                    self.report["restore_state"] = {key: restored[key] for key in
                        ("success", "states", "unconfirmed", "stop_requested", "error") if key in restored}
                    if not restored.get("success"):
                        self.report.update(success=False, cleanup_failed=True)
                restored = self.report.get("restore_state", {})
                if db_id in restored.get("states", {}):
                    self.report["final_state"] = restored["states"][db_id]
                elif restored.get("success") is False:
                    self.report["final_state"] = "unknown"
                else:
                    final = poll_readback(self.device.list_instances, self.log, "登录后状态查询").get(db_id, {})
                    self.report["final_state"] = final.get("state") or final.get("status") or "unknown"
            except (requests.RequestException, OSError, ValueError, TypeError):
                self.report["final_state"] = "unknown"
                self.report.update(success=False, cleanup_failed=True)
            self.log("完成: " + self.report["status"])
            self.save()
        return self.report


def main(argv=None):
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
    parser.add_argument("--resume", action="store_true", help="使用同一输出目录内的受保护状态续接验证码/主页核验，不重发登录")
    parser.add_argument("--restart-login", action="store_true", help="人工返回邮箱输入页后明确发起新登录，替换旧续跑状态")
    parser.add_argument("--output-dir", type=Path, help="独立报告和受保护续跑状态目录")
    args = parser.parse_args(argv)
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
        result = TikTokLogin(config, output_dir=args.output_dir).run(args.db_id, selected[0]["email"], args.start_stopped,
                                        args.keep_running, args.verify_only, args.code_wait, selected[0].get("username"),
                                        login_mode, password, args.allow_current_code, args.resume, args.restart_login)
        return 0 if result["success"] else 1 if result["status"] == "failed" or result.get("cleanup_failed") else 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error) if type(error) is ValueError else type(error).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
