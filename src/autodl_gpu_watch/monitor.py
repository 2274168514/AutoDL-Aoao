"""Polling, durable notification state, and a process lock (standard library only)."""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .api import (
    APIError, AuthenticationError, Observation, RateLimitError, StartRejectedError,
    evaluate_target, instance_start_status, resolve_start_candidate,
)
from .config import Config
from .notifier import MailError


class StateError(RuntimeError):
    """State cannot be trusted or saved; sending further mail would risk duplicates."""


class LockError(RuntimeError):
    """The shared state lock could not be acquired."""


def _validated_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "targets"}:
        raise ValueError("invalid state structure")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unsupported state version")
    if not isinstance(value["targets"], dict):
        raise ValueError("invalid target state")
    targets = {}
    for key, entry in value["targets"].items():
        if not isinstance(key, str) or not key.startswith(("instance:", "machine:")):
            raise ValueError("invalid target key")
        if not key.partition(":")[2] or any(ord(char) < 32 for char in key):
            raise ValueError("invalid target key")
        if not isinstance(entry, dict) or set(entry) != {"armed", "last_sent_at"}:
            raise ValueError("invalid notification state")
        if type(entry["armed"]) is not bool:
            raise ValueError("invalid notification flag")
        sent_at = entry["last_sent_at"]
        if sent_at is not None and (
            isinstance(sent_at, bool)
            or not isinstance(sent_at, (int, float))
            or not math.isfinite(sent_at)
            or sent_at < 0
        ):
            raise ValueError("invalid notification timestamp")
        if not entry["armed"] and sent_at is None:
            raise ValueError("missing notification timestamp")
        targets[key] = {"armed": entry["armed"], "last_sent_at": sent_at}
    return {"version": 1, "targets": targets}


class StateStore:
    """Store only target identifiers, notification flags, and successful-send times."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "targets": {}}
        except UnicodeError:
            return self._corrupt()
        except OSError as error:
            raise StateError("无法读取通知状态文件，请检查文件路径和权限。") from error
        try:
            return _validated_state(json.loads(text))
        except (ValueError, TypeError, OverflowError, RecursionError):
            return self._corrupt()

    def _corrupt(self) -> dict[str, Any]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.path.with_name(self.path.name + f".corrupt-{stamp}-{uuid.uuid4().hex[:8]}")
        try:
            shutil.copyfile(self.path, backup)
        except OSError as error:
            raise StateError(
                "通知状态文件损坏，且无法创建备份；原文件保留，已停止发送。请检查路径和权限。"
            ) from error
        # Keep the invalid original too: a restart must not silently discard the
        # notification history and send every available target again.
        raise StateError(
            f"通知状态文件损坏，已备份至 {backup}；原文件保留，已停止发送。"
            "请检查后修复状态文件；确认接受重新提醒后，也可手动移走原文件。"
        )

    def save(self, state: dict[str, Any]) -> None:
        try:
            clean = _validated_state(state)
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            raise StateError("通知状态无效，已停止发送；没有覆盖现有文件。") from error
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix="." + self.path.name + ".", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(clean, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, UnicodeError) as error:
            raise StateError("无法保存通知状态，已停止发送；请检查磁盘空间和文件权限。") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _empty_auto_start_state() -> dict[str, Any]:
    return {"version": 1, "status": "idle", "instance_uuid": None, "target_key": None, "label": None, "mail_sent": False}


def _validated_auto_start_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(_empty_auto_start_state()):
        raise ValueError("invalid auto-start state")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unsupported auto-start state")
    if value["status"] not in ("idle", "pending", "accepted", "unknown", "succeeded", "rejected"):
        raise ValueError("invalid auto-start status")
    if type(value["mail_sent"]) is not bool or (value["mail_sent"] and value["status"] != "succeeded"):
        raise ValueError("invalid auto-start notification")
    if value["status"] == "idle":
        if any(value[name] is not None for name in ("instance_uuid", "target_key", "label")):
            raise ValueError("unexpected auto-start target")
    else:
        for name in ("instance_uuid", "target_key", "label"):
            text = value[name]
            if not isinstance(text, str) or not text or len(text) > 512 or any(ord(char) < 32 for char in text):
                raise ValueError("invalid auto-start identity")
        if not value["target_key"].startswith("instance:") or not value["target_key"].partition(":")[2]:
            raise ValueError("invalid auto-start target")
    return dict(value)


class AutoStartStore:
    """One durable authorization round, independent of notification schema v1."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                text = stream.read(65537)
            if len(text) > 65536:
                raise ValueError("oversized auto-start state")
            return _validated_auto_start_state(json.loads(text))
        except FileNotFoundError:
            return _empty_auto_start_state()
        except (OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError) as error:
            # Never discard an uncertain paid operation because its file is bad.
            raise StateError("自动开机记录无法读取或已损坏，已禁用自动开机；普通监控继续。") from error

    def save(self, value: dict[str, Any]) -> None:
        temporary = None
        try:
            clean = _validated_auto_start_state(value)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix="." + self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(clean, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError) as error:
            raise StateError("无法保存自动开机记录，已禁用自动开机；普通监控继续。") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


class InstanceLock:
    """Nonblocking kernel lock, automatically released when the process exits."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stream = None

    def __enter__(self) -> "InstanceLock":
        if self._stream is not None:
            raise LockError("当前对象已持有监控锁。")
        stream = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open("a+b")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if stream is not None:
                stream.close()
            raise LockError(
                "无法取得监控锁：可能已有程序使用同一状态文件，或锁文件路径不可写。"
            ) from error
        self._stream = stream
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
        # Do not unlink: another process may already hold this same inode.


def _compact(value: str, limit: int = 180) -> str:
    return " ".join("".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in value).split())[:limit]


class Monitor:
    def __init__(
        self,
        config: Config,
        client,
        notifier,
        store: StateStore,
        log: Callable[[str], None] = print,
        stop_event: threading.Event | None = None,
        on_observations: Callable[[list[Observation]], None] | None = None,
        new_auto_start_round: bool = False,
        on_auto_start: Callable[[dict], None] | None = None,
    ):
        self.config = config
        self.client = client
        self.notifier = notifier
        self.store = store
        self.log = log
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.on_observations = on_observations
        self._state: dict[str, Any] | None = None
        self._check_lock = threading.Lock()
        self._halted = False
        self._update_lock = threading.Lock()
        self._pending_update: tuple[Config, Any] | None = None
        self._wake_event = threading.Event()
        self._auto_store = AutoStartStore(config.state_file.with_name(config.state_file.name + ".autostart.json"))
        self._auto_state: dict[str, Any] | None = None
        self._auto_disabled = False
        self._new_auto_start_round = new_auto_start_round
        self._on_auto_start = on_auto_start
        self._last_auto_event = None

    def queue_update(self, config: Config, notifier) -> None:
        """Stage an append/recipient change without waiting for a network call."""
        with self._update_lock:
            previous = self._pending_update[0] if self._pending_update is not None else self.config
            unchanged = replace(config, targets=previous.targets, email=replace(config.email, recipients=previous.email.recipients))
            if unchanged != previous or config.targets[:len(previous.targets)] != previous.targets:
                raise ValueError("运行中只能追加目标或修改收件人。")
            self._pending_update = (config, notifier)
            self._wake_event.set()

    def wake(self) -> None:
        """Wake a normal polling wait, including when the caller requests stop."""
        self._wake_event.set()

    def _apply_pending_update(self) -> None:
        with self._update_lock:
            if self._pending_update is not None:
                self.config, self.notifier = self._pending_update
                self._pending_update = None
            # Clearing at the START of a poll cannot discard an update received
            # during its request, SMTP send, or state-file commit.
            self._wake_event.clear()

    def _auto_event(self, state: str, message: str) -> None:
        event = {"state": state, "message": message}
        if self._auto_state is not None and self._auto_state["instance_uuid"] is not None:
            event.update(instance_uuid=self._auto_state["instance_uuid"], target_key=self._auto_state["target_key"],
                         label=self._auto_state["label"])
        if event == self._last_auto_event:
            return
        self._last_auto_event = event
        self.log(message)
        if self._on_auto_start is not None:
            try:
                self._on_auto_start(dict(event))
            except Exception:
                # GUI feedback is not part of the paid-operation transaction.
                pass

    def _save_auto_state(self, state: dict[str, Any]) -> bool:
        try:
            self._auto_store.save(state)
        except StateError as error:
            self._auto_disabled = True
            self._auto_event("blocked", str(error))
            return False
        self._auto_state = state
        return True

    def _load_auto_state(self) -> bool:
        if self._auto_disabled:
            return False
        if self._auto_state is not None:
            return True
        if self.config.autodl.sub_account or any(target.kind != "instance" for target in self.config.targets):
            self._auto_disabled = True
            self._auto_event("blocked", "自动开机只支持主账号的具体实例；普通监控继续。")
            return False
        try:
            self._auto_state = self._auto_store.load()
        except StateError as error:
            self._auto_disabled = True
            self._auto_event("blocked", str(error))
            return False
        # Only an explicit new GUI authorization may reset a completed round.
        # A pending/accepted/unknown operation survives even a fresh checkbox.
        if self._new_auto_start_round and self._auto_state["status"] in ("succeeded", "rejected"):
            return self._save_auto_state(_empty_auto_start_state())
        return True

    def _send_auto_started(self) -> None:
        state = self._auto_state
        assert state is not None and state["status"] == "succeeded"
        self._auto_event("succeeded", f"已确认 {_compact(state['label'])} 使用 GPU 运行；本轮抢卡完成，继续监控。")
        if not state["mail_sent"] and not self.stop_event.is_set():
            # SMTP failure leaves succeeded durable; retries can only send mail.
            self.notifier.send_auto_started(state["instance_uuid"], state["label"])
            self._save_auto_state({**state, "mail_sent": True})

    def _reconcile_auto_start(self, rows: list[dict]) -> None:
        state = self._auto_state
        assert state is not None
        status = instance_start_status(state["instance_uuid"], rows)
        if status == "running":
            if self._save_auto_state({**state, "status": "succeeded"}):
                self._send_auto_started()
        elif status == "starting":
            self._auto_event("pending", "开机请求正在确认中；本轮不会再对其他实例发起开机。")
        else:
            if state["status"] != "unknown" and not self._save_auto_state({**state, "status": "unknown"}):
                return
            self._auto_event("unknown", "开机结果尚未确认，将继续查询；不会重复开机或尝试其他实例。")

    def _check_auto_start(self, rows: list[dict]) -> bool:
        """Act before SMTP; True suppresses availability mail for this snapshot."""
        if self.config.auto_start is not True or not self._load_auto_state():
            return False
        state = self._auto_state
        assert state is not None
        status = state["status"]
        if status == "succeeded":
            self._send_auto_started()
            return False
        if status == "rejected":
            self._auto_event("blocked", "本轮开机请求已被明确拒绝；请检查账号条件，重新勾选后开始新一轮。")
            return False
        if status in ("pending", "accepted", "unknown"):
            self._reconcile_auto_start(rows)
            return True
        self._auto_event("waiting", "等待满足开机条件的实例：须已关机、GPU 数据完整且空卡足够；本轮只抢一台。")
        for target in self.config.targets:
            candidate = resolve_start_candidate(target, rows)
            if candidate is None:
                continue
            if self.stop_event.is_set():
                return True
            pending = {"version": 1, "status": "pending", "instance_uuid": candidate.instance_uuid,
                       "target_key": target.key, "label": candidate.label, "mail_sent": False}
            if not self._save_auto_state(pending):
                return False
            self._auto_event("pending", f"正在尝试开机：{_compact(candidate.label)}；本轮只抢一台。")
            if self.stop_event.is_set():
                # The HTTP method has not been entered: cancellation is certain.
                self._save_auto_state({**pending, "status": "rejected"})
                return True
            try:
                self.client.start_instance(candidate.instance_uuid)
            except StartRejectedError as error:
                if error.retryable is True:
                    # Only the API's explicit definite-rejection classification
                    # may unlock another try; re-query on the NEXT poll first.
                    if self._save_auto_state(_empty_auto_start_state()):
                        self._auto_event("waiting", "开机请求已明确拒绝且可重试；下次检查会重新确认空卡，不在本轮继续提交。")
                elif self._save_auto_state({**pending, "status": "rejected"}):
                    self._auto_event("blocked", f"{_compact(str(error))} 本轮自动开机已暂停，普通监控继续。")
                return True
            except AuthenticationError:
                self._save_auto_state({**pending, "status": "rejected"})
                self._auto_event("blocked", "自动开机认证失败；请重新登录后再授权。")
                raise
            except APIError:
                if self._save_auto_state({**pending, "status": "unknown"}):
                    self._auto_event("unknown", "开机结果不明确，将仅查询确认，不会重复开机。")
                raise
            except Exception:
                if self._save_auto_state({**pending, "status": "unknown"}):
                    self._auto_event("unknown", "开机结果不明确，将仅查询确认，不会重复开机。")
                raise APIError("开机结果不明确，将继续查询确认；不会重复提交。") from None
            if not self._save_auto_state({**pending, "status": "accepted"}):
                return True
            self._auto_event("pending", "开机请求已受理，正在查询实际运行状态。")
            if not self.stop_event.is_set():
                self._reconcile_auto_start(self.client.fetch_instances())
            return True
        return False

    def check_once(self, notify: bool = True) -> list[Observation]:
        """Fetch once; query-only checks never change notification state."""
        with self._check_lock:
            self._apply_pending_update()
            if notify and self._halted:
                raise StateError("本次运行的通知状态保存曾失败，已停止发送；请修复后重启。")
            if notify and self._state is None:
                self._state = self.store.load()
            rows = self.client.fetch_instances()
            observations = [evaluate_target(target, rows) for target in self.config.targets]
            if self.on_observations is not None:
                self.on_observations(observations)
            for item in observations:
                status = "可用" if item.available is True else "不可用" if item.available is False else "未知"
                idle = "?" if item.idle_gpus is None else str(item.idle_gpus)
                required = "?" if item.required_gpus is None else str(item.required_gpus)
                self.log(f"{status} | {_compact(item.label)} | 空闲 {idle} / 需要 {required} | {_compact(item.detail)}")
            if not notify:
                return observations

            if self._check_auto_start(rows):
                return observations

            state = self._state
            assert state is not None
            entries = state["targets"]
            now = time.time()
            changed = False
            pending = []
            for item in observations:
                if item.available is None:
                    continue
                entry = entries.get(item.target_key)
                if entry is None:
                    entry = {"armed": True, "last_sent_at": None}
                    entries[item.target_key] = entry
                    changed = True
                if item.available is False:
                    if not entry["armed"]:
                        entry["armed"] = True
                        changed = True
                    continue
                sent_at = entry["last_sent_at"]
                if entry["armed"] and (sent_at is None or now - sent_at >= self.config.cooldown_seconds):
                    pending.append(item)

            # Persist re-arming before SMTP. This also checks that the destination
            # is writable before attempting a first notification.
            if changed or pending:
                self._save_state()
            if not pending:
                return observations
            self.notifier.send_available(pending)
            sent_at = time.time()
            for item in pending:
                entries[item.target_key] = {"armed": False, "last_sent_at": sent_at}
            try:
                self._save_state()
            except StateError as error:
                raise StateError(
                    "邮件已被 SMTP 接受，但通知状态未能保存；已停止发送，避免本次运行重复提醒。"
                    "请检查磁盘和状态文件；直接重启可能再次发送。"
                ) from error
            self.log(f"已发送 {len(pending)} 个可用目标的合并提醒。")
            return observations

    def _save_state(self) -> None:
        assert self._state is not None
        try:
            self.store.save(self._state)
        except StateError:
            self._halted = True
            raise

    def run(self) -> None:
        """Poll serially with interruptible waits; credential failures stop the run."""
        failures = 0
        try:
            while not self.stop_event.is_set():
                retry_wait = False
                try:
                    self.check_once()
                except AuthenticationError:
                    self.log("认证或权限校验失败，已停止轮询；请检查 AutoDL Token、子账号权限和配置后重启。")
                    raise
                except StateError:
                    self.log("通知状态不可用，已停止轮询；请修复状态文件或保存权限。")
                    raise
                except (APIError, MailError) as error:
                    retry_wait = True
                    failures += 1
                    base = max(15.0, min(self.config.poll_seconds, 900.0))
                    delay = min(900.0, base * 2 ** min(failures - 1, 10))
                    if isinstance(error, RateLimitError):
                        retry = error.retry_after
                        if isinstance(retry, (int, float)) and not isinstance(retry, bool) and math.isfinite(retry):
                            delay = max(delay, min(max(0.0, retry), 86400.0))
                    self.log(f"本轮失败：{_compact(str(error))}；{delay:g} 秒后重试。")
                else:
                    failures = 0
                    delay = self.config.poll_seconds
                # A configuration update never bypasses an API/SMTP backoff or
                # Retry-After. Only normal polling intervals can be shortened.
                if retry_wait:
                    stopped = self.stop_event.wait(delay)
                else:
                    deadline = time.monotonic() + delay
                    while not self.stop_event.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or self._wake_event.wait(min(remaining, 0.25)):
                            break
                    stopped = self.stop_event.is_set()
                if stopped:
                    break
        except KeyboardInterrupt:
            self.stop_event.set()
            self.log("已停止监控。")
