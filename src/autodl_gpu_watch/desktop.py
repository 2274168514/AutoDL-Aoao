"""Desktop controller: background work, cancellation and per-user settings."""

from __future__ import annotations

import base64
import copy
import ctypes
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Callable

from .api import APIError, AuthenticationError, AutoDLClient, StartRejectedError
from .cli import example_config
from .config import ConfigError, parse_config
from .monitor import InstanceLock, LockError, Monitor, StateError, StateStore
from .notifier import EmailNotifier, MailError


class OperationStopped(Exception):
    """Cancellation at a safe boundary; never counts as a sent email."""


def desktop_data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "AutoDLGPUWatch"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AutoDLGPUWatch"
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".config"
    return base / "AutoDLGPUWatch"


def _atomic_private_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, filename = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _protect_windows(value: bytes, decrypt: bool = False) -> bytes:
    """Use the current Windows account's DPAPI; do not fall back to plaintext."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    protect = crypt.CryptProtectData
    unprotect = crypt.CryptUnprotectData
    protect.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    unprotect.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    protect.restype = unprotect.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    if decrypt:
        ok = unprotect(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = protect(ctypes.byref(source), "AutoDL GPU Watch", None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise ConfigError("无法使用当前 Windows 账户加密或解密凭据，请重新输入，或取消记住凭据")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def _credential(value: object, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ConfigError(label + " 必须是单行文本")
    value = value.strip()
    if required and not value:
        raise ConfigError("请先在连接设置中填写" + label)
    return value


class _CancellableClient(AutoDLClient):
    def __init__(self, token, settings, timeout, stop):
        super().__init__(token, settings, timeout)
        self._stop = stop

    def _fetch_page(self, page_index):
        if self._stop.is_set():
            raise OperationStopped()
        result = super()._fetch_page(page_index)
        if self._stop.is_set():
            raise OperationStopped()
        return result

    def fetch_machines(self, machine_ids):
        rows = []
        for machine_id in dict.fromkeys(machine_ids):
            if self._stop.is_set():
                raise OperationStopped()
            rows.extend(super().fetch_machines([machine_id]))
            if self._stop.is_set():
                raise OperationStopped()
        return rows

    def _fetch_machine_page(self, machine_id, page_index):
        if self._stop.is_set():
            raise OperationStopped()
        result = super()._fetch_machine_page(machine_id, page_index)
        if self._stop.is_set():
            raise OperationStopped()
        return result

    def start_instance(self, instance_uuid):
        if self._stop.is_set():
            raise StartRejectedError("已停止，尚未发送开机请求。")
        # Once sent, return the actual outcome even if Stop was clicked meanwhile.
        # The monitor must persist it before allowing another operation.
        return super().start_instance(instance_uuid)


class _StopAwareNotifier:
    def __init__(self, notifier, stop):
        self.notifier, self.stop = notifier, stop

    def send_available(self, observations):
        if self.stop.is_set():
            raise OperationStopped()
        # After this point allow SMTP and the subsequent state save to finish.
        return self.notifier.send_available(observations)

    def send_auto_started(self, instance_uuid, label):
        if self.stop.is_set():
            raise OperationStopped()
        return self.notifier.send_auto_started(instance_uuid, label)


class _ObservedClient:
    def __init__(self, client, emit):
        self.client, self.emit = client, emit

    def fetch_instances(self):
        rows = self.client.fetch_instances()
        self.emit("connection", {"state": "verified", "message": "AutoDL 连接正常"})
        return rows

    def fetch_machines(self, machine_ids):
        rows = self.client.fetch_machines(machine_ids)
        self.emit("connection", {"state": "verified", "message": "AutoDL 连接正常"})
        return rows

    def start_instance(self, instance_uuid):
        return self.client.start_instance(instance_uuid)


class DesktopController:
    def __init__(self, emit: Callable[[str, object], None], data_dir: Path | None = None):
        self.data_dir = (Path(data_dir) if data_dir is not None else desktop_data_dir()).expanduser().resolve()
        self._emit_callback = emit
        self._lock = threading.Lock()
        self._running = False
        self._action_busy = False
        self._stopping = False
        self._stop_event = threading.Event()
        self._action_stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._action_thread: threading.Thread | None = None
        self._watcher: Monitor | None = None
        self._live_profile: dict | None = None
        self._live_config = None
        self._redactions: tuple[str, ...] = ()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._running or self._action_busy

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def action_busy(self) -> bool:
        with self._lock:
            return self._action_busy

    @property
    def stopping(self) -> bool:
        with self._lock:
            return self._stopping

    def _emit_lifecycle(self) -> None:
        with self._lock:
            running, action, stopping = self._running, self._action_busy, self._stopping
        self._emit("running", running)
        self._emit("action_busy", action)
        self._emit("stopping", stopping)
        self._emit("busy", running or action)

    @property
    def secret_storage_label(self) -> str:
        if sys.platform == "win32":
            return "记住凭据（Windows 账户加密，仅本机使用）"
        return "记住凭据（本地明文，仅当前用户可读写）"

    def _emit(self, kind: str, payload: object) -> None:
        if isinstance(payload, str):
            for secret in self._redactions:
                if secret:
                    payload = payload.replace(secret, "[已隐藏]")
            payload = "".join(c if c.isprintable() or c == "\n" else " " for c in payload)
        self._emit_callback(kind, payload)

    def _raw(self, profile: dict) -> dict:
        raw = {key: copy.deepcopy(value) for key, value in profile.items() if not key.startswith("_")}
        raw["state_file"] = "state.json"
        return raw

    def _saved_raw(self, profile: dict) -> dict:
        raw = self._raw(profile)
        # A paid-action opt-in applies to one manually started monitoring round.
        # Reopening the desktop app must never silently restore authorization.
        raw["auto_start"] = False
        return raw

    def _config(self, profile: dict, *, mail: bool = True, empty: bool = False):
        raw = self._raw(profile)
        if not mail:
            raw["email"] = {"sender": "unused@example.invalid", "recipients": ["unused@example.invalid"]}
        return parse_config(raw, self.data_dir, allow_empty_targets=empty)

    def _operation_config(self, profile: dict, operation: str):
        # Validate only settings used by the requested one-shot operation.
        raw = example_config()
        raw["targets"] = []
        if operation == "mail":
            raw["email"] = copy.deepcopy(profile.get("email", {}))
        else:
            raw["autodl"] = copy.deepcopy(profile.get("autodl", {}))
            raw["timeout_seconds"] = profile.get("timeout_seconds", 20)
            if operation == "check":
                raw["targets"] = copy.deepcopy(profile.get("targets", []))
        return self._config(raw, mail=operation == "mail", empty=operation != "check")

    def load_profile(self) -> dict:
        profile = example_config()
        profile["targets"] = []
        profile["email"]["sender"] = ""
        profile["email"]["recipients"] = []
        path = self.data_dir / "profile.json"
        if path.exists():
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(profile, dict) or any(key.startswith("_") for key in profile):
                    raise ValueError()
                profile["auto_start"] = False
                config = self._config(profile, empty=True)
                profile["targets"] = [
                    {"kind": target.kind, "value": target.value, "min_gpus": target.min_gpus}
                    for target in config.targets
                ]
            except (OSError, ValueError, UnicodeError):
                raise ConfigError("桌面配置无法读取，原文件已保留。请检查配置目录中的 profile.json") from None
        profile.update({"_token": "", "_password": "", "_remember": False, "auto_start": False})
        secret_path = self.data_dir / "credentials.json"
        if secret_path.exists():
            try:
                secret = json.loads(secret_path.read_text(encoding="utf-8"))
                if secret.get("version") != 1:
                    raise ValueError()
                if secret.get("kind") == "dpapi" and sys.platform == "win32":
                    data = json.loads(_protect_windows(base64.b64decode(secret["data"], validate=True), decrypt=True))
                elif secret.get("kind") == "plain" and sys.platform != "win32":
                    data = secret["data"]
                else:
                    raise ValueError()
                profile["_token"] = _credential(data.get("token", ""), "AutoDL Token", required=False)
                profile["_password"] = _credential(data.get("password", ""), "SMTP 授权码", required=False)
                profile["_remember"] = True
            except (OSError, ValueError, UnicodeError, TypeError, KeyError, AttributeError):
                # Keep usable non-secret settings even if OS/account protection changed.
                self._emit("error", "保存的凭据无法读取，请重新输入；原凭据文件已保留。")
        return profile

    def save_profile(self, profile: dict) -> None:
        if self.busy:
            raise ConfigError("请先停止当前操作，再保存设置")
        self._config(profile, empty=True)
        token = _credential(profile.get("_token", ""), "AutoDL Token", required=False)
        password = _credential(profile.get("_password", ""), "SMTP 授权码", required=False)
        try:
            secret_path = self.data_dir / "credentials.json"
            if profile.get("_remember", False):
                data = {"token": token, "password": password}
                if sys.platform == "win32":
                    payload = base64.b64encode(_protect_windows(json.dumps(data).encode("utf-8"))).decode("ascii")
                    secret = {"version": 1, "kind": "dpapi", "data": payload}
                else:
                    secret = {"version": 1, "kind": "plain", "data": data}
                _atomic_private_write(secret_path, json.dumps(secret).encode("utf-8"))
            else:
                secret_path.unlink(missing_ok=True)
            _atomic_private_write(self.data_dir / "profile.json", json.dumps(self._saved_raw(profile), ensure_ascii=False, indent=2).encode("utf-8"))
        except OSError:
            raise ConfigError("无法保存本地配置，请检查配置目录的写入权限") from None
        self._emit("log", "设置已保存。" + ("凭据已按所选方式保存在本机。" if profile.get("_remember") else "凭据仅在当前窗口内保留。"))

    def _launch(
        self, profile: dict, task: Callable, *, monitoring: bool = False,
        completion: str = "操作完成", allow_monitoring: bool = False, monitor_config=None,
    ) -> None:
        with self._lock:
            if self._stopping:
                raise ConfigError("正在停止，请等待当前请求结束")
            if self._action_busy or (self._running and (monitoring or not allow_monitoring)):
                raise ConfigError("已有操作在进行，请等待完成或先停止监控")
            if monitoring:
                self._running = True
                self._stop_event = threading.Event()
                stop = self._stop_event
                self._live_profile = copy.deepcopy(profile)
                self._live_config = monitor_config
            else:
                self._action_busy = True
                self._action_stop_event = threading.Event()
                stop = self._action_stop_event
            secrets = tuple(str(profile.get(name, "")).strip() for name in ("_token", "_password"))
            self._redactions = tuple(dict.fromkeys((*self._redactions, *secrets)))
        self._emit_lifecycle()

        def worker():
            try:
                task(stop)
                if stop.is_set():
                    self._emit("status", "正在停止…" if self.stopping else "已停止")
                elif not monitoring and self.running:
                    self._emit("status", completion + "；监控继续运行")
                else:
                    self._emit("status", completion)
            except OperationStopped:
                self._emit("status", "已停止")
            except (ConfigError, APIError, MailError, StateError, LockError) as error:
                if isinstance(error, AuthenticationError):
                    self._emit("connection", {"state": "error", "message": "AutoDL 登录已失效，请重新连接 Chrome"})
                self._emit("error", str(error))
                self._emit("status", "本次操作失败，监控继续运行" if not monitoring and self.running else "操作失败，请查看提示")
            except Exception:
                self._emit("error", "操作未完成，请检查配置和网络。未显示底层异常，以避免泄露凭据。")
                self._emit("status", "本次操作失败，监控继续运行" if not monitoring and self.running else "操作失败")
            finally:
                with self._lock:
                    if monitoring:
                        self._running = False
                        self._watcher = None
                        self._live_profile = None
                        self._live_config = None
                    else:
                        self._action_busy = False
                        self._action_thread = None
                    if not self._running and not self._action_busy:
                        self._stopping = False
                self._emit_lifecycle()

        thread = threading.Thread(target=worker, name="autodl-monitor" if monitoring else "autodl-action", daemon=False)
        if monitoring:
            self._thread = thread
        else:
            self._action_thread = thread
            if not self.running:
                self._thread = thread
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                if monitoring:
                    self._running = False
                    self._live_profile = None
                    self._live_config = None
                else:
                    self._action_busy = False
                    self._action_thread = None
                if not self._running and not self._action_busy:
                    self._stopping = False
            self._emit_lifecycle()
            raise ConfigError("无法启动后台任务，请关闭后重试") from None

    @staticmethod
    def _same_credentials(profile: dict, previous: dict) -> bool:
        return all(
            _credential(profile.get(key, ""), label, required=False)
            == _credential(previous.get(key, ""), label, required=False)
            for key, label in (("_token", "AutoDL Token"), ("_password", "SMTP 授权码"))
        ) and profile.get("_remember", False) == previous.get("_remember", False)

    def _action_profile(self, profile: dict) -> dict:
        """Auxiliary requests use the accepted running snapshot, never edits."""
        with self._lock:
            if self._stopping:
                raise ConfigError("正在停止，请等待当前请求结束")
            if self._running:
                if self._live_profile is None or self._config(profile) != self._live_config or not self._same_credentials(profile, self._live_profile):
                    raise ConfigError("监控中请先应用追加目标或收件人变更；其他设置和凭据需停止后修改")
                return copy.deepcopy(self._live_profile)
        return copy.deepcopy(profile)

    def update_live_profile(self, profile: dict) -> None:
        """Persist only allowed live edits, then apply them at the next poll."""
        candidate = copy.deepcopy(profile)
        config = self._config(candidate)
        with self._lock:
            previous = self._live_config
            if not self._running or self._stopping or previous is None or self._live_profile is None:
                raise ConfigError("当前没有可更新的监控，请等待停止完成后再修改设置")
            unchanged = replace(config, targets=previous.targets, email=replace(config.email, recipients=previous.email.recipients))
            if unchanged != previous or not self._same_credentials(candidate, self._live_profile):
                raise ConfigError("监控中只能追加目标和修改收件人；其他设置或凭据请停止后修改")
            if config.targets[:len(previous.targets)] != previous.targets:
                raise ConfigError("监控中只能追加目标，不能移除、重排或修改现有监控目标")
            if config == previous:
                return
            raw = self._saved_raw(candidate)
            # Construct first (no network), then persist before making the change
            # visible to the running poller. Never touch credentials.json here.
            password = _credential(self._live_profile.get("_password", ""), "SMTP 授权码")
            notifier = _StopAwareNotifier(EmailNotifier(config.email, password), self._stop_event)
            try:
                _atomic_private_write(self.data_dir / "profile.json", json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8"))
            except OSError:
                raise ConfigError("无法保存运行中设置，监控保持原配置；请检查配置目录权限") from None
            self._live_config = config
            self._live_profile = candidate
            if self._watcher is not None:
                self._watcher.queue_update(config, notifier)
        self._emit("live_profile_updated", {
            "targets": [{"kind": target.kind, "value": target.value, "min_gpus": target.min_gpus} for target in config.targets],
            "recipients": list(config.email.recipients),
        })
        self._emit("log", "监控设置已保存，将在下一轮检查时应用；提醒记录保持不变。")

    def fetch_instances(self, profile: dict) -> None:
        profile = self._action_profile(profile)
        config = self._operation_config(profile, "list")
        token = _credential(profile.get("_token", ""), "AutoDL Token")

        def task(stop):
            self._emit("status", "正在读取实例…")
            rows = _CancellableClient(token, config.autodl, config.timeout_seconds, stop).fetch_instances()
            self._emit("connection", {"state": "verified", "message": "AutoDL 连接正常"})
            self._publish_instances(rows)
        self._launch(profile, task, allow_monitoring=True, completion="实例列表已更新")

    def _publish_instances(self, rows: list[dict]) -> None:
        keys = ("uuid", "name", "machine_id", "status", "gpu_idle_num", "req_gpu_amount")
        safe = []
        for row in rows:
            item = {key: row.get(key) for key in keys}
            item["uuid"] = row.get("uuid") or row.get("instance_uuid") or ""
            safe.append(item)
        self._emit("instances", safe)
        self._emit("log", f"已读取 {len(safe)} 个实例，选择需要监控的目标。")

    def open_chrome(self, profile: dict) -> None:
        """Open the dedicated browser without waiting for login or reading a token."""
        def task(stop):
            from .chrome_login import ChromeLoginCancelled, ChromeLoginError, open_chrome

            self._emit("connection", {"state": "connecting", "message": "正在打开 Chrome"})
            try:
                open_chrome(self.data_dir, stop, lambda message: self._emit("status", message))
                if stop.is_set():
                    raise OperationStopped()
                self._emit("connection", {"state": "opened", "message": "待检测"})
            except (ChromeLoginCancelled, OperationStopped):
                self._emit("connection", {"state": "error", "message": "打开已取消"})
                raise OperationStopped() from None
            except ChromeLoginError as error:
                self._emit("connection", {"state": "error", "message": "Chrome 未打开"})
                raise ConfigError(str(error)) from None
        self._launch(profile, task, completion="登录后点击“检测登录”。")

    def detect_chrome(self, profile: dict) -> None:
        """Detect the existing dedicated session without opening another window."""
        self._read_chrome_session(profile, "detect_chrome")

    def connect_chrome(self, profile: dict) -> None:
        """Compatibility entry point for the combined open-and-wait workflow."""
        self._read_chrome_session(profile, "connect_chrome")

    def _read_chrome_session(self, profile: dict, operation: str) -> None:
        config = self._operation_config(profile, "list")

        def task(stop):
            from .chrome_login import ChromeLoginCancelled, ChromeLoginError, connect_chrome, detect_chrome

            self._emit("connection", {"state": "connecting", "message": "正在检测登录"})
            try:
                reader = detect_chrome if operation == "detect_chrome" else connect_chrome
                token = reader(self.data_dir, stop, lambda message: self._emit("status", message))
                if stop.is_set():
                    raise OperationStopped()
                token = _credential(token, "AutoDL 登录信息")
                self._redactions = tuple(dict.fromkeys((*self._redactions, token)))
                self._emit("status", "已获取登录信息，正在验证 AutoDL 连接…")
                rows = _CancellableClient(token, config.autodl, config.timeout_seconds, stop).fetch_instances()
                if stop.is_set():
                    raise OperationStopped()
                # This event is private view state, never a log/status message.
                # Replace the previous credential only after API verification.
                self._emit("chrome_connected", {"token": token, "instance_count": len(rows)})
                self._emit("connection", {"state": "verified", "message": "Chrome 登录已连接"})
                self._emit("log", "AutoDL 连接成功。专用 Chrome 窗口可以关闭，监控会独立运行。")
                self._publish_instances(rows)
            except (ChromeLoginCancelled, OperationStopped):
                self._emit("connection", {"state": "error", "message": "连接已取消"})
                raise OperationStopped() from None
            except AuthenticationError:
                self._emit("connection", {"state": "error", "message": "AutoDL 登录已失效或无权限"})
                raise ConfigError("请在专用 Chrome 窗口重新登录 AutoDL，再点击“检测登录”；子账号请同时检查子账号设置。") from None
            except ChromeLoginError as error:
                self._emit("connection", {"state": "error", "message": "Chrome 连接未完成"})
                raise ConfigError(str(error)) from None
            except Exception:
                self._emit("connection", {"state": "error", "message": "连接未能验证，请检查提示后重试"})
                raise
        self._launch(profile, task, completion="登录检测成功，请选择实例。")

    def check(self, profile: dict) -> None:
        config = self._operation_config(profile, "check")
        token = _credential(profile.get("_token", ""), "AutoDL Token")

        def task(stop):
            self._emit("status", "正在检查空卡…")
            client = _ObservedClient(_CancellableClient(token, config.autodl, config.timeout_seconds, stop), self._emit)
            watcher = Monitor(config, client, None, StateStore(config.state_file), stop_event=stop,
                              log=lambda text: self._emit("log", text))
            observations = watcher.check_once(notify=False)
            self._emit("observations", observations)
            self._emit("log", "检查完成。本次未发送邮件，也未更改提醒状态。")
        self._launch(profile, task)

    def test_email(self, profile: dict) -> None:
        profile = self._action_profile(profile)
        config = self._operation_config(profile, "mail")
        password = _credential(profile.get("_password", ""), "SMTP 授权码")

        def task(stop):
            if stop.is_set():
                raise OperationStopped()
            self._emit("status", "正在发送测试邮件…")
            EmailNotifier(config.email, password).send_test()
            self._emit("mail_sent", "邮件服务器已接受测试邮件，请检查收件箱或垃圾邮件文件夹。")
        self._launch(profile, task, allow_monitoring=True, completion="测试邮件已发送")

    def start(self, profile: dict) -> None:
        config = self._config(profile)
        token = _credential(profile.get("_token", ""), "AutoDL Token")
        password = _credential(profile.get("_password", ""), "SMTP 授权码")
        self.save_profile(profile)

        def task(stop):
            lock_path = config.state_file.with_name(config.state_file.name + ".lock")
            with InstanceLock(lock_path):
                self._emit("status", f"监控中 · 每 {config.poll_seconds:g} 秒检查")
                self._emit("log", "监控已启动。成功启动一台后停止抢卡；开机会按 AutoDL 原有规则计费。" if config.auto_start else "监控已启动。检测到足够空卡时会发送邮件。")
                client = _ObservedClient(_CancellableClient(token, config.autodl, config.timeout_seconds, stop), self._emit)
                with self._lock:
                    live_config = self._live_config
                    notifier = _StopAwareNotifier(EmailNotifier(live_config.email, password), stop)
                    watcher = Monitor(
                        live_config, client, notifier, StateStore(config.state_file),
                        log=lambda text: self._emit("log", text), stop_event=stop,
                        on_observations=lambda observations: self._emit("observations", observations),
                        new_auto_start_round=config.auto_start,
                        on_auto_start=lambda result: self._emit("auto_start", result),
                    )
                    self._watcher = watcher
                watcher.run()
        self._launch(profile, task, monitoring=True, monitor_config=config)

    def stop(self) -> None:
        with self._lock:
            if not self._running and not self._action_busy:
                return
            self._stopping = True
            self._stop_event.set()
            self._action_stop_event.set()
            watcher = self._watcher
        if watcher is not None:
            watcher.wake()
        self._emit_lifecycle()
        self._emit("status", "正在停止，等待当前请求结束…")
