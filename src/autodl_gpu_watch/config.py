"""Configuration and opt-in local environment file loading (no dependencies)."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    kind: str
    value: str
    min_gpus: int | None = None

    @property
    def key(self) -> str:
        return self.kind + ":" + self.value


@dataclass(frozen=True)
class AutoDLSettings:
    token_env: str = "AUTODL_TOKEN"
    sub_account: str = ""
    app_version: str = ""


@dataclass(frozen=True)
class EmailSettings:
    host: str = "smtp.163.com"
    port: int = 465
    security: str = "ssl"
    sender: str = ""
    recipients: tuple[str, ...] = ()
    password_env: str = "SMTP_PASSWORD"
    timeout: float = 20.0


@dataclass(frozen=True)
class Config:
    autodl: AutoDLSettings
    email: EmailSettings
    targets: tuple[Target, ...]
    poll_seconds: float = 60
    timeout_seconds: float = 20
    cooldown_seconds: float = 300
    state_file: Path = field(default_factory=lambda: Path("state.json"))
    auto_start: bool = False


def _object(value: Any, label: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(label + " 必须是 JSON 对象")
    if set(value) - allowed:
        # Do not echo unknown keys: the user may have accidentally pasted a secret.
        raise ConfigError(label + " 包含未知配置项，请对照 config.example.json")
    return value


def _text(value: Any, label: str, empty: bool = False) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ConfigError(label + " 必须是单行文本")
    value = value.strip()
    if not value and not empty:
        raise ConfigError(label + " 不能为空")
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(label + " 必须是数字")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigError(f"{label} 必须在 {minimum:g}～{maximum:g} 之间")
    return float(value)


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(label + " 必须是整数")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{label} 必须在 {minimum}～{maximum} 之间")
    return value


def _env_name(value: Any, label: str) -> str:
    value = _text(value, label)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ConfigError(label + " 必须是有效的环境变量名称")
    return value


def _address(value: Any, label: str) -> str:
    value = _text(value, label)
    if len(value) > 254 or not re.fullmatch(r"[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+", value):
        raise ConfigError(label + " 必须填写纯邮箱地址，例如 name@163.com")
    return value


def load_config(path: Path | str) -> Config:
    path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise ConfigError("无法读取配置文件，请先运行 init 创建配置") from error
    except (ValueError, UnicodeError) as error:
        raise ConfigError("配置文件不是有效的 UTF-8 JSON") from error
    return parse_config(raw, path.parent)


def parse_config(raw: Any, base_dir: Path | str, *, allow_empty_targets: bool = False) -> Config:
    """Validate an in-memory profile, resolving state relative to its directory."""
    base_dir = Path(base_dir).expanduser().resolve()
    root = _object(raw, "配置", {"autodl", "email", "targets", "poll_seconds", "timeout_seconds", "cooldown_seconds", "state_file", "auto_start"})
    auto_start = root.get("auto_start", False)
    if type(auto_start) is not bool:
        raise ConfigError("auto_start 必须是 true 或 false")
    ad = _object(root.get("autodl", {}), "autodl", {"token_env", "sub_account", "app_version"})
    autodl = AutoDLSettings(
        _env_name(ad.get("token_env", "AUTODL_TOKEN"), "autodl.token_env"),
        _text(ad.get("sub_account", ""), "autodl.sub_account", empty=True),
        _text(ad.get("app_version", ""), "autodl.app_version", empty=True),
    )
    em = _object(root.get("email", {}), "email", {"host", "port", "security", "sender", "recipients", "password_env", "timeout"})
    host = _text(em.get("host", "smtp.163.com"), "email.host")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ConfigError("email.host 应为 SMTP 主机名，不含协议或路径")
    recipients = em.get("recipients", [])
    if not isinstance(recipients, list) or not recipients:
        raise ConfigError("email.recipients 必须是至少含一个地址的数组")
    security = em.get("security", "ssl")
    if security not in ("ssl", "starttls"):
        raise ConfigError("email.security 只支持 ssl 或 starttls")
    email = EmailSettings(
        host=host, port=_integer(em.get("port", 465), "email.port", 1, 65535), security=security,
        sender=_address(em.get("sender", ""), "email.sender"),
        recipients=tuple(dict.fromkeys(_address(v, "email.recipients") for v in recipients)),
        password_env=_env_name(em.get("password_env", "SMTP_PASSWORD"), "email.password_env"),
        timeout=_number(em.get("timeout", 20), "email.timeout", 1, 120),
    )
    if autodl.token_env.casefold() == email.password_env.casefold():
        raise ConfigError("AutoDL Token 和 SMTP 授权码必须使用不同的环境变量名称")
    target_list = root.get("targets", [])
    if not isinstance(target_list, list) or (not target_list and not allow_empty_targets) or len(target_list) > 100:
        raise ConfigError("targets 必须包含 1～100 个监控目标")
    targets = []
    seen = set()
    for item in target_list:
        item = _object(item, "target", {"kind", "value", "min_gpus"})
        kind = item.get("kind", "instance")
        if kind not in ("instance", "machine"):
            raise ConfigError("target.kind 只支持 instance 或 machine")
        value = _text(item.get("value", ""), "target.value")
        minimum = item.get("min_gpus")
        if minimum is not None:
            minimum = _integer(minimum, "target.min_gpus", 1, 1024)
        if kind == "machine" and minimum is None:
            raise ConfigError("machine 目标必须指定 min_gpus")
        target = Target(kind, value, minimum)
        if target.key in seen:
            raise ConfigError("targets 中存在重复监控目标")
        seen.add(target.key)
        targets.append(target)
    if auto_start and any(target.kind != "instance" for target in targets):
        raise ConfigError("自动开机只支持具体实例，不能用于机器目标。")
    if auto_start and autodl.sub_account:
        raise ConfigError("自动开机暂不支持子账号，请使用主账号。")
    state_name = _text(root.get("state_file", "state.json"), "state_file")
    state_path = Path(state_name).expanduser()
    if not state_path.is_absolute():
        state_path = base_dir / state_path
    return Config(
        autodl, email, tuple(targets),
        _number(root.get("poll_seconds", 60), "poll_seconds", 15, 86400),
        _number(root.get("timeout_seconds", 20), "timeout_seconds", 1, 120),
        _number(root.get("cooldown_seconds", 300), "cooldown_seconds", 0, 86400),
        state_path.resolve(),
        auto_start,
    )


def load_env_file(path: Path | str, override: bool = False) -> None:
    """Read literal KEY=value entries. Never execute shell syntax or expand variables."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise ConfigError("无法读取环境变量文件") from error
    entries = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, equals, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not equals or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ConfigError(f"环境变量文件第 {number} 行格式错误，需 KEY=value")
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError(f"环境变量文件第 {number} 行引号不完整")
            value = value[1:-1]
        if any(ord(c) < 32 for c in value):
            raise ConfigError(f"环境变量文件第 {number} 行包含控制字符")
        entries[name] = value
    for name, value in entries.items():
        if override or name not in os.environ:
            os.environ[name] = value


def require_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or any(ord(c) < 32 for c in value):
        raise ConfigError(f"缺少有效的环境变量 {name}；请使用 --env-file 或在终端中设置")
    return value
