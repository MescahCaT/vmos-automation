"""按明确的云机名与账号对应关系登录TikTok；单台失败继续，验证码不重发。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "阶段6-TikTok自动登录"))
from auto_login import TikTokLogin
from email_verifier import protected_session
from check_vmos_connection import poll_readback, read_json, save_json
from setup_proxy import MAPPING_LOCK, ProxySetup


def load_accounts(path, config):
    """JSON或用户Excel账号表；表内TK密码只在内存中转为DPAPI密文。"""
    path = Path(os.path.expandvars(str(path))).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"账号文件不存在：{path}")
    suffix = path.suffix.casefold()
    if suffix == ".json":
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise ValueError("账号JSON必须包含accounts数组")
        return data["accounts"]
    if suffix not in (".xls", ".xlsx"):
        raise ValueError("账号路径仅支持.json、.xls或.xlsx文件")
    try:
        if suffix == ".xls":
            import xlrd
            book = xlrd.open_workbook(str(path), on_demand=True)
            try:
                sheet = book.sheet_by_index(0)
                rows = [sheet.row_values(i) for i in range(sheet.nrows)]
            finally:
                book.release_resources()
        else:
            from openpyxl import load_workbook
            book = load_workbook(path, read_only=True, data_only=True)
            try:
                rows = list(book.worksheets[0].iter_rows(values_only=True))
            finally:
                book.close()
    except ImportError:
        raise ValueError("读取Excel需先执行：python -m pip install -r 阶段6-TikTok自动登录/requirements.txt") from None
    except Exception:
        raise ValueError("账号Excel读取失败，请确认文件格式正确且未加密") from None
    return spreadsheet_accounts(rows, config)


def spreadsheet_accounts(rows, config):
    if not rows:
        raise ValueError("账号表为空")
    header = [str(v or "").strip().casefold() for v in rows[0]]
    def column(aliases, fallback=None):
        matches = [i for i, value in enumerate(header) if value in aliases]
        if len(matches) == 1:
            return matches[0]
        if not matches and fallback is not None:
            return fallback
        raise ValueError("账号表须有唯一的云机名、tk账号、密码、邮箱列")
    name_col = column({"云机名", "云机名称", "云机", "device_name"}, 0 if header and not header[0] else None)
    user_col = column({"tk账号", "tiktok账号", "username"})
    pass_col = column({"密码", "tk密码", "tiktok密码", "password"})
    email_col = column({"邮箱", "email"})
    urls = [i for i, h in enumerate(header) if h in {"辅助验证邮箱/验证地址", "验证地址", "取码地址"}]
    if len(urls) > 1:
        raise ValueError("账号表取码地址列重复")
    endpoint = urlsplit(config["tiktok"]["email_code"]["endpoint"])
    accounts = []
    for number, source in enumerate(rows[1:], 2):
        if not any(v is not None and str(v).strip() for v in source):
            continue
        def cell(index):
            return "" if index >= len(source) or source[index] is None else str(source[index]).strip()
        name, username, email = (cell(i) for i in (name_col, user_col, email_col))
        password = source[pass_col] if pass_col < len(source) else None
        if not isinstance(password, str):
            raise ValueError(f"账号表第{number}行的TK密码须为文本，避免Excel丢失前导零")
        if not all((name, username, password, email)) or re.search(r"[\r\n\0]", password):
            raise ValueError(f"账号表第{number}行缺少云机名、TK账号、密码或邮箱，或密码含非法换行")
        if urls and cell(urls[0]):
            url = urlsplit(cell(urls[0]))
            if ((url.scheme, url.netloc, url.path) != (endpoint.scheme, endpoint.netloc, endpoint.path)
                    or parse_qs(url.query).get("username") != [email]):
                raise ValueError(f"账号表第{number}行取码地址与主配置或该行邮箱不一致")
        accounts.append({"device_name": name, "username": username.lstrip("@"), "email": email,
                         "login_mode": "email_code", "enabled": True,
                         "password_protected": protected_session({"email": email, "password": password})})
    if not accounts:
        raise ValueError("账号表没有可用账号")
    return accounts


def select_accounts(accounts, names=None, email=None, bindings=None):
    if not isinstance(accounts, list) or any(not isinstance(a, dict) for a in accounts):
        raise ValueError("账号清单必须是accounts对象数组")
    active = [a for a in accounts if a.get("enabled", True) is True]
    by_email = {}
    for account in active:
        address = account.get("email", "")
        username = account.get("username", "")
        if (not isinstance(address, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address)
                or not isinstance(username, str) or not re.fullmatch(r"@?[A-Za-z0-9_.]{2,24}", username)
                or account.get("login_mode") not in ("email_code", "password")):
            raise ValueError("启用账号必须有有效email、username和login_mode")
        if address.casefold() in by_email:
            raise ValueError("账号邮箱重复，未执行登录")
        by_email[address.casefold()] = account
    if names is not None and (not names or any(not isinstance(n, str) for n in names)):
        raise ValueError("指定云机名称不能为空")
    if names and (len({n.casefold() for n in names}) != len(names)
                  or any(not n.strip() or n != n.strip() for n in names)):
        raise ValueError("指定云机名称为空或重复")
    if email is not None:
        if not names or len(names) != 1 or bindings is not None:
            raise ValueError("--email只能配合一个云机名称，且不与--bindings同时使用")
        bindings = {names[0]: email}
    if bindings is None:
        bindings = {}
        for account in active:
            name = account.get("device_name")
            if name is not None:
                if not isinstance(name, str) or not name.strip() or name in bindings:
                    raise ValueError("账号device_name无效或重复")
                bindings[name] = account["email"]
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError("请用--name和--email指定单台，或配置device_name/--bindings明确每台账号")
    selected_names = names if names is not None else list(bindings)
    if any(n not in bindings for n in selected_names):
        raise ValueError("所选云机缺少明确的账号对应关系")
    selected = []
    for name in selected_names:
        address = bindings[name]
        if not isinstance(name, str) or not name.strip() or not isinstance(address, str) or address.casefold() not in by_email:
            raise ValueError("云机账号对应表引用了无效、未启用或不存在的账号")
        selected.append((name, by_email[address.casefold()]))
    if len({a["email"].casefold() for _, a in selected}) != len(selected):
        raise ValueError("同一批不能把同一账号分配给多台云机")
    if len({a["username"].lstrip("@").casefold() for _, a in selected}) != len(selected):
        raise ValueError("同一批不能把同一TikTok用户名分配给多台云机")
    return selected


def account_password(account, env=None):
    env = env or account.get("password_env")
    if env:
        return os.environ.get(env)
    token = account.get("password_protected")
    if token:
        data = protected_session(token=token)
        if not isinstance(data, dict) or data.get("email", "").casefold() != account["email"].casefold() or not isinstance(data.get("password"), str):
            raise ValueError("加密密码与目标账号不匹配")
        return data["password"]
    return None


def task_completed(record, completion):
    if record.get("cleanup_failed"):
        return False
    if completion == "password-transition":
        return bool(record.get("success") is True and record.get("password_completion_confirmed") is True
                    or record.get("password_transition_confirmed") is True
                    and record.get("email_code_verified_before_password") is True)
    return record.get("success") is True and not record.get("password_completion_confirmed")


def execute(config, targets, job, workers=1, verify_only=False, start_stopped=True,
            keep_running=False, code_wait=120, login_mode=None, password_env=None,
            allow_current_code=False, resume=False, restart_login=False, completion="profile"):
    if not targets or type(workers) is not int or workers < 1 or type(code_wait) is not int or not 1 <= code_wait <= 600:
        raise ValueError("workers必须为正整数，code-wait为1至600秒")
    if resume and (verify_only or restart_login):
        raise ValueError("续跑不能与只读核验或重新登录同时使用")
    if completion not in ("profile", "password-transition") or verify_only and completion != "profile":
        raise ValueError("只读核验须使用profile完成标准")
    if completion == "password-transition" and login_mode == "password":
        raise ValueError("密码后跳转终点必须使用email_code登录")
    if completion == "password-transition":
        login_mode = "email_code"
        keep_running = True
    host = f"http://{config['vmos']['host_ip']}:{int(config['vmos']['port'])}"
    proxy = ProxySetup(host)
    with MAPPING_LOCK:
        mapping = read_json(proxy.mapping_path)
    if mapping.get("host") != host or not isinstance(mapping.get("devices"), dict):
        raise ValueError("设备映射的主机或格式不符")
    instances = poll_readback(proxy.device.list_instances, print, "登录批次实例查询")
    job = Path(job)
    job.mkdir(parents=True, exist_ok=True)
    previous = {}
    status_path = job / "batch_status.json"
    if status_path.exists():
        if not resume:
            raise ValueError("输出目录已有批次报告，请使用新目录或--resume")
        old = read_json(status_path)
        if old.get("host") != host or not isinstance(old.get("devices"), list):
            raise ValueError("原批次主机或报告不匹配，未覆盖报告")
        if old.get("completion", "profile") != completion:
            raise ValueError("续跑的完成标准与原批次不同，未覆盖报告")
        previous = {r["name"]: r for r in old["devices"]}
        for name, account in targets:
            saved = previous.get(name)
            if saved and (saved.get("account_email", "").casefold() != account["email"].casefold()
                          or saved.get("expected_username", "").lstrip("@").casefold() != account["username"].lstrip("@").casefold()):
                raise ValueError("续跑账号与原批次不匹配，未覆盖报告")
        save_json(job / ("batch_status-before-resume-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json"), old)
    elif resume:
        raise ValueError("缺少原批次报告，未执行续跑")
    report = {"operation": "login_verify" if verify_only else "login", "success": False,
              "host": host, "completion": completion, "task_completed": False,
              "total": len(targets), "workers": min(workers, len(targets)), "devices": [],
              "not_started": [name for name, _ in targets], "failed_names": []}
    records = [None] * len(targets)
    lock = threading.Lock()
    def publish(index, record):
        with lock:
            records[index] = record
            report["devices"] = [r for r in records if r is not None]
            report["success_count"] = sum(r["success"] for r in report["devices"])
            report["completed_count"] = sum(r["task_completed"] for r in report["devices"])
            report["failed_names"] = [r["name"] for r in report["devices"] if not r["task_completed"]]
            report["failed_count"] = len(report["failed_names"])
            report["not_started"] = [targets[i][0] for i, r in enumerate(records) if r is None]
            report["task_completed"] = not report["not_started"] and not report["failed_names"]
            report["success"] = not report["not_started"] and report["success_count"] == report["total"]
            save_json(job / "batch_status.json", report)
            (job / "failed_names.txt").write_text("".join(n + "\n" for n in report["failed_names"]), encoding="utf-8")
    def run(index, name, account):
        record = {"name": name, "account_email": account["email"],
                  "expected_username": "@" + account["username"].lstrip("@"), "success": False, "status": "failed"}
        try:
            matches = [(i, v) for i, v in instances.items() if (v.get("user_name") or v.get("name")) == name]
            if len(matches) != 1:
                raise ValueError("云机名称不存在或不唯一，未执行登录")
            db_id, instance = matches[0]
            record["db_id"] = db_id
            current = mapping["devices"].get(db_id)
            if not isinstance(current, dict) or current.get("name") != name:
                raise ValueError("当前云机ID与本地名称映射不匹配，请先更新设备映射")
            if current.get("account_email") is not None and current["account_email"].casefold() != account["email"].casefold():
                raise ValueError("云机已绑定其他邮箱，未切换账号")
            if any(i != db_id and i in instances and v.get("account_email", "").casefold() == account["email"].casefold()
                   for i, v in mapping["devices"].items() if isinstance(v, dict) and isinstance(v.get("account_email"), str)):
                raise ValueError("账号已绑定其他在册云机，未重复登录")
            saved = previous.get(name)
            if saved and task_completed(saved, completion):
                if saved.get("db_id") != db_id:
                    raise ValueError("已成功设备的ID发生变化，未沿用旧结果")
                if saved.get("success") and (str(current.get("account_email", "")).casefold() != account["email"].casefold()
                        or str(current.get("tiktok_username", "")).lstrip("@").casefold() != account["username"].lstrip("@").casefold()):
                    raise ValueError("已成功设备的账号映射发生变化，未沿用旧结果")
                record.update(saved, resumed_skip=True, task_completed=True)
                publish(index, record)
                print(f"{name}：沿用原批次已完成记录，未重新登录", flush=True)
                return
            flow = TikTokLogin(config, output_dir=job / db_id, mapping_path=proxy.mapping_path)
            log = flow.log
            flow.log = lambda message: log(f"{name}：{message}")
            flow.device.log = flow.log
            flow.proxy.log = flow.log
            password = account_password(account, password_env) if not verify_only else None
            has_session = ((job / db_id / ("login_session-" + db_id + ".json")).exists()
                           or (proxy.mapping_path.parent / "login-locks" / ("login_session-" + db_id + ".json")).exists())
            if resume and saved and not has_session:
                old_report = Path(saved.get("report", str(job / db_id / "login_status.json")))
                details = read_json(old_report) if old_report.is_file() else saved
                if any(details.get(k) for k in ("email_submitted", "password_submitted", "code_sent", "code_entered")):
                    raise ValueError("原流程已提交登录但续跑状态缺失，须人工确认页面；未重新发送")
            result = flow.run(db_id, account["email"], start_stopped=start_stopped,
                keep_running=keep_running, verify_only=verify_only, code_wait=code_wait,
                expected_username=account["username"], login_mode=login_mode or account["login_mode"],
                password=password, allow_current_code=allow_current_code,
                resume=resume and has_session, restart_login=restart_login, completion=completion)
            password = None
            record.update({k: v for k, v in result.items() if k not in ("screenshots", "proxy", "system")})
            record["report"] = str(job / db_id / ("login_verify.json" if verify_only else "login_status.json"))
        except Exception as error:
            record.update(success=False, status="failed", error=str(error) if type(error) is ValueError else type(error).__name__)
        record["task_completed"] = task_completed(record, completion)
        publish(index, record)
        print(f"{name}：{'已完成指定终点' if record['task_completed'] else '未完成，原因已记录；继续后续云机'}", flush=True)
    save_json(job / "batch_status.json", report)
    if targets:
        with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as pool:
            futures = [pool.submit(run, i, name, account) for i, (name, account) in enumerate(targets)]
            for future in as_completed(futures):
                future.result()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--name", help="指定一台云机名称")
    group.add_argument("--names", nargs="+", help="指定一台或多台，空格分隔")
    parser.add_argument("--email", help="单台账号邮箱，必须在启用的账号配置中")
    parser.add_argument("--bindings", type=Path, help="云机名→邮箱的JSON对象")
    parser.add_argument("--config", type=Path, default=ROOT / "配置文件/config.json")
    parser.add_argument("--accounts-file", type=Path, help="账号文件路径，支持JSON/XLS/XLSX；路径含空格请加双引号")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--code-wait", type=int, default=240)
    parser.add_argument("--completion", choices=("password-transition", "profile"),
                        help="默认password-transition：邮箱验证码后密码提交成功即完成，保留运行；profile核验本人主页")
    parser.add_argument("--login-mode", choices=("email_code", "password"))
    parser.add_argument("--password-env")
    parser.add_argument("--allow-current-code", action="store_true")
    parser.add_argument("--resume", action="store_true", help="续接--output-dir内同一批登录，不重新发码")
    parser.add_argument("--restart-login", action="store_true", help="人工返回邮箱输入页后，明确允许替换旧续跑状态并重新登录")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.workers < 1 or not 1 <= args.code_wait <= 600:
            raise ValueError("workers必须为正整数，code-wait为1至600秒")
        if args.resume and (args.output_dir is None or not args.output_dir.is_dir()):
            raise ValueError("--resume必须指定原批次--output-dir")
        completion = args.completion or ("profile" if args.verify_only else "password-transition")
        if args.verify_only and completion != "profile":
            raise ValueError("--verify-only不能配合password-transition")
        if args.login_mode == "password" and completion == "password-transition":
            raise ValueError("直接密码登录请使用--completion profile；默认终点要求先邮箱验证码后密码验证")
        config = read_json(args.config)
        accounts = load_accounts(args.accounts_file or ROOT / config["tiktok"]["account_file"], config)
        names = [args.name] if args.name else args.names
        targets = select_accounts(accounts, names, args.email, read_json(args.bindings) if args.bindings else None)
        logs = ROOT / "日志"
        logs.mkdir(exist_ok=True)
        job = args.output_dir or Path(tempfile.mkdtemp(prefix="login-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-", dir=logs))
        print(f"登录目标 {len(targets)} 台，并发 {min(args.workers, len(targets))}；结果目录：{job}", flush=True)
        print("完成标准：" + ("邮箱验证码→密码提交成功；登录入口异常时按返回键，结束保留运行" if completion == "password-transition" else "本人主页用户名匹配"), flush=True)
        result = execute(config, targets, job, args.workers, args.verify_only, True, args.keep_running,
                         args.code_wait, args.login_mode, args.password_env, args.allow_current_code, args.resume, args.restart_login, completion)
        print(f"指定终点完成：{result['completed_count']}/{result['total']}；未完成名单：{job / 'failed_names.txt'}", flush=True)
        return 0 if result["task_completed"] else 1 if any(r["status"] == "failed" or r.get("cleanup_failed") for r in result["devices"]) else 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("登录任务未完成：" + (str(error) if type(error) is ValueError else type(error).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
