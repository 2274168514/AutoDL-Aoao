"""Small cross-platform CLI. All network commands are explicit."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config, load_env_file, require_secret


def example_config() -> dict:
    return {
        "autodl": {"token_env": "AUTODL_TOKEN", "sub_account": "", "app_version": ""},
        "targets": [{"kind": "instance", "value": "替换为实例ID或完整名称", "min_gpus": None}],
        "poll_seconds": 60,
        "timeout_seconds": 20,
        "cooldown_seconds": 300,
        "auto_start": False,
        "state_file": "state.json",
        "email": {
            "host": "smtp.163.com", "port": 465, "security": "ssl",
            "sender": "yourname@163.com", "recipients": ["recipient@example.com"],
            "password_env": "SMTP_PASSWORD", "timeout": 20,
        },
    }


def _write_new(path: Path, text: str, private: bool = False) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(path), flags, 0o600 if private else 0o644)
    except FileExistsError as error:
        raise ConfigError("目标文件已存在；为避免覆盖，请指定新路径或自行编辑原文件") from error
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _log(message: str) -> None:
    # Terminal control characters in remote instance names must not act as commands.
    safe = "".join(c if c >= " " and c != "\x7f" else " " for c in str(message))
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {safe}", flush=True)


def _secrets(config, output: Path) -> None:
    if not sys.stdin.isatty():
        raise ConfigError("secrets 需要交互终端；自动部署请直接设置环境变量")
    if output.exists():
        raise ConfigError("凭据文件已存在；请自行编辑，或用 --output 指定新路径")
    print("输入不会回显。文件仅保存在本地，内容未加密，请勿上传或分享。")
    token = getpass.getpass("AutoDL 网页登录 Token: ").strip()
    password = getpass.getpass("163 SMTP 授权码（不是登录密码）: ").strip()
    if not token or not password or any(ord(c) < 32 for c in token + password):
        raise ConfigError("凭据不能为空或包含控制字符")
    if any(c in token + password for c in ('"', "'")):
        raise ConfigError("凭据含意外引号，请检查是否多复制了引号")
    _write_new(output, f"{config.autodl.token_env}={token}\n{config.email.password_env}={password}\n", private=True)
    print("本地凭据文件已创建。运行时通过 --env-file 指定它；环境变量优先。")


def _print_observations(observations) -> None:
    for item in observations:
        state = "可用" if item.available is True else "不足/不适用" if item.available is False else "未知"
        idle = "?" if item.idle_gpus is None else str(item.idle_gpus)
        need = "?" if item.required_gpus is None else str(item.required_gpus)
        _log(f"{item.label} | {state} | 空闲 {idle} / 需要 {need} | {item.detail}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(description="AutoDL Aoao · GPU 空卡监控 · Windows / macOS / Linux · 163 邮件提醒")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=Path("config.json"), help="配置文件，默认 config.json")
    parser.add_argument("--env-file", type=Path, help="显式加载本地 KEY=value 凭据文件；已存在的环境变量优先")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gui", help="打开桌面客户端；使用用户目录中的独立配置")
    sub.add_parser("init", help="创建配置模板，不覆盖已有文件")
    secret_parser = sub.add_parser("secrets", help="在本地隐藏输入凭据并保存到文件")
    secret_parser.add_argument("--output", type=Path, default=Path(".env"))
    sub.add_parser("validate", help="仅验证配置结构，不联网")
    sub.add_parser("list", help="只读列出当前账号的实例和宿主机 ID，不发邮件")
    sub.add_parser("check", help="只检查一次，不开机、不发邮件、不修改提醒状态")
    sub.add_parser("test-email", help="向配置的收件人发送一封测试邮件")
    sub.add_parser("run", help="开始监控，满足条件时发送提醒；Ctrl+C 停止")
    args = parser.parse_args(argv)

    try:
        if args.command == "gui":
            from .gui import main as gui_main
            return gui_main()
        if args.command == "init":
            _write_new(args.config, json.dumps(example_config(), ensure_ascii=False, indent=2) + "\n")
            print("配置模板已创建。填写目标实例、发件邮箱和收件邮箱，再运行 secrets 配置凭据。")
            return 0
        config = load_config(args.config)
        if args.command == "validate":
            print(f"配置有效：{len(config.targets)} 个目标，每 {config.poll_seconds:g} 秒检查。未联网。")
            return 0
        if args.command == "secrets":
            _secrets(config, args.output)
            return 0
        if args.env_file:
            load_env_file(args.env_file)
        from .notifier import EmailNotifier
        if args.command == "test-email":
            EmailNotifier(config.email, require_secret(config.email.password_env)).send_test()
            print("SMTP 服务器已接受测试邮件；请检查收件箱或垃圾邮件文件夹。")
            return 0
        from .api import AutoDLClient, evaluate_target
        client = AutoDLClient(require_secret(config.autodl.token_env), config.autodl, config.timeout_seconds)
        if args.command == "list":
            rows = client.fetch_instances()
            if not rows:
                print("当前账号没有实例。")
            for row in rows:
                _log(f"实例 {row.get('uuid') or row.get('instance_uuid') or '?'} | 名称 {row.get('name', '?')} | 宿主机 {row.get('machine_id', '?')} | 状态 {row.get('status', '?')} | 空闲GPU {row.get('gpu_idle_num', '?')}")
            return 0
        if args.command == "check":
            rows = client.fetch_instances()
            observations = [evaluate_target(target, rows) for target in config.targets]
            _print_observations(observations)
            return 3 if any(item.available is None for item in observations) else 0
        from .monitor import InstanceLock, Monitor, StateStore
        notifier = EmailNotifier(config.email, require_secret(config.email.password_env))
        lock_path = config.state_file.with_name(config.state_file.name + ".lock")
        with InstanceLock(lock_path):
            store = StateStore(config.state_file)
            watcher = Monitor(config, client, notifier, store, log=_log)
            _log(f"监控已启动：{len(config.targets)} 个目标，间隔 {config.poll_seconds:g} 秒。Ctrl+C 停止。")
            watcher.run()
        return 0
    except KeyboardInterrupt:
        print("\n监控已停止。")
        return 0
    except ConfigError as error:
        print("配置错误：" + str(error), file=sys.stderr)
        return 2
    except Exception as error:
        from .api import APIError
        from .notifier import MailError
        # Only application-controlled messages are safe to print. Never echo raw
        # HTTP/SMTP response bodies, environment values or untrusted tracebacks.
        if isinstance(error, (APIError, MailError)) or error.__class__.__module__ == "autodl_gpu_watch.monitor":
            _log("已停止：" + str(error))
        else:
            _log("已停止：发生本地错误（" + type(error).__name__ + "），请检查文件权限及配置。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
