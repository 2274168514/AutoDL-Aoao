"""Read the AutoDL token only from this app's dedicated, visible Chrome session.

Normal Chrome profiles, cookies, passwords, and other origins are never read.
The browser stays open; every DevTools connection is closed after each request.
"""

from __future__ import annotations

import http.client
import io
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


_ORIGIN = "https://www.autodl.com"
_LOGIN_URL = _ORIGIN + "/console/instance/list"
_PROFILE_MARKER = ".autodl-gpu-watch-session"
_MARKER_CONTENT = b"AutoDLGPUWatch dedicated Chrome session v1\n"
_MAX_JSON = 64 * 1024
_MAX_WIRE = 128 * 1024
_TOKEN_EXPRESSION = (
    "(() => { if (location.origin !== 'https://www.autodl.com') return null; "
    "const value = localStorage.getItem('token'); "
    "return typeof value === 'string' && value.length <= 16384 ? value : null; })()"
)


class ChromeLoginError(Exception):
    """A safe user-facing failure, with no browser response or token attached."""


class ChromeLoginCancelled(Exception):
    """The user cancelled waiting for Chrome login."""


class _Unavailable(Exception):
    """A local endpoint is not ready, closed, or temporarily navigating."""


def _check(stop: threading.Event, deadline: float) -> float:
    if stop.is_set():
        raise ChromeLoginCancelled("已取消 Chrome 连接；专用浏览器窗口可自行关闭。")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Unavailable()
    return min(0.5, remaining)


class _SocketReader(io.RawIOBase):
    def __init__(self, connection):
        self.connection = connection
        # Keep the native socket file reference alive when HTTPConnection closes
        # its socket after receiving a "Connection: close" response header.
        self.stream = connection.connection.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        data = self.connection._bounded_read(self.stream.read, len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        try:
            self.stream.close()
        finally:
            super().close()


class _BoundedSocket:
    """Bound the entire HTTP/WebSocket exchange, including headers and frames.

The byte budget applies before websocket-client can accumulate an oversized
frame. A short per-read timeout and a total deadline bound trickling responses.
"""

    def __init__(self, connection, stop: threading.Event, deadline: float):
        self.connection = connection
        self.stop = stop
        self.deadline = deadline
        self.remaining = _MAX_WIRE

    def _bounded_read(self, reader, size):
        self.connection.settimeout(_check(self.stop, self.deadline))
        if self.remaining <= 0:
            raise ChromeLoginError("Chrome 连接返回内容过大，请关闭专用窗口后重试。")
        data = reader(min(size, self.remaining))
        self.remaining -= len(data)
        return data

    def recv(self, size, *args):
        return self._bounded_read(lambda count: self.connection.recv(count, *args), size)

    def send(self, data, *args):
        self.connection.settimeout(_check(self.stop, self.deadline))
        return self.connection.send(data, *args)

    def sendall(self, data, *args):
        self.connection.settimeout(_check(self.stop, self.deadline))
        return self.connection.sendall(data, *args)

    def makefile(self, mode="rb", *args, **kwargs):
        if mode != "rb":
            raise ChromeLoginError("Chrome 本地连接方式不受支持。")
        return io.BufferedReader(_SocketReader(self))

    def __getattr__(self, name):
        return getattr(self.connection, name)


def _local_socket(port: int, stop: threading.Event, deadline: float) -> _BoundedSocket:
    # Literal IPv4 loopback plus an already connected socket prevents proxy use,
    # DNS resolution, and redirected WebSocket connections to another host.
    connection = socket.create_connection(("127.0.0.1", port), timeout=_check(stop, deadline))
    return _BoundedSocket(connection, stop, deadline)


def _http_json(port: int, path: str, stop: threading.Event, deadline: float):
    if path not in ("/json/version", "/json/list"):
        raise ChromeLoginError("Chrome 本地请求不受支持。")
    request_deadline = min(deadline, time.monotonic() + 2)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.sock = _local_socket(port, stop, request_deadline)
        connection.request("GET", path, headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        # HTTPConnection never follows redirects or reads proxy environment vars.
        if response.status != 200:
            raise _Unavailable()
        data = response.read(_MAX_JSON + 1)
        if len(data) > _MAX_JSON:
            raise ChromeLoginError("Chrome 页面信息过多，请关闭专用窗口中无关页面后重试。")
        return json.loads(data)
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        raise _Unavailable() from None
    finally:
        connection.close()


def _profile(data_dir: Path, *, create: bool = True) -> Path:
    base = Path(data_dir).expanduser().resolve()
    path = base / "chrome-session"
    if path.resolve() != path:
        raise ChromeLoginError("Chrome 专用会话目录不能是链接，请检查应用数据目录。")
    if not create and not path.is_dir():
        raise ChromeLoginError("尚未打开专用 Chrome，请先点击“打开 Chrome”，登录后再检测。")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = path / _PROFILE_MARKER
    if marker.is_symlink():
        raise ChromeLoginError("Chrome 专用会话目录无法验证，已停止连接；请检查应用数据目录。")
    if marker.exists():
        with marker.open("rb") as stream:
            content = stream.read(len(_MARKER_CONTENT) + 1)
        if content != _MARKER_CONTENT:
            raise ChromeLoginError("Chrome 专用会话目录无法验证，已停止连接；请检查应用数据目录。")
    else:
        if not create:
            raise ChromeLoginError("未找到本应用的专用 Chrome 会话，请先点击“打开 Chrome”。")
        if any(path.iterdir()):
            raise ChromeLoginError("Chrome 会话目录已有其他数据，已停止连接；请使用本应用创建的专用会话。")
        with marker.open("xb") as stream:
            stream.write(_MARKER_CONTENT)
        try:
            marker.chmod(0o600)
        except OSError:
            pass
    return path


def _chrome_executable() -> Path:
    candidates = []
    if sys.platform == "win32":
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(key):
                candidates.append(Path(os.environ[key]) / "Google" / "Chrome" / "Application" / "chrome.exe")
        try:
            import winreg

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                        value, _ = winreg.QueryValueEx(key, "")
                        if isinstance(value, str):
                            candidates.append(Path(value.strip('"')))
                except OSError:
                    pass
        except ImportError:
            pass
    elif sys.platform == "darwin":
        candidates.extend([
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ])
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chrome.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_absolute() and candidate.is_file():
            return candidate
    raise ChromeLoginError("未找到 Google Chrome，请先安装 Chrome 后重试；也可以手动填入登录 Token。")


def _launch(profile: Path) -> None:
    subprocess.Popen(
        [
            str(_chrome_executable()),
            "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--new-window", _LOGIN_URL,
        ],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _websocket_url(value, port: int, kind: str) -> str | None:
    if not isinstance(value, str) or len(value) > 512:
        return None
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "ws" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
            and parsed.port == port and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r"/devtools/" + kind + r"/[A-Za-z0-9_-]{1,128}", parsed.path)
        )
    except ValueError:
        return None
    return f"ws://127.0.0.1:{port}{parsed.path}" if valid else None


def _endpoint(profile: Path, stop: threading.Event, deadline: float) -> int | None:
    port_file = profile / "DevToolsActivePort"
    try:
        if port_file.is_symlink():
            return None
        with port_file.open("rb") as stream:
            lines = stream.read(513).decode("ascii").splitlines()
        if len(lines) != 2 or len("\n".join(lines)) > 512 or not lines[0].isdigit():
            return None
        port = int(lines[0])
        if not 1 <= port <= 65535 or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]{16,128}", lines[1]):
            return None
        version = _http_json(port, "/json/version", stop, deadline)
        if not isinstance(version, dict):
            return None
        browser_url = _websocket_url(version.get("webSocketDebuggerUrl"), port, "browser")
        # The random browser ID must match this profile's file, not merely a
        # currently listening port that could belong to a different Chrome.
        if browser_url != f"ws://127.0.0.1:{port}{lines[1]}":
            return None
        return port
    except (OSError, UnicodeError, ValueError, _Unavailable):
        return None


def _autodl_page(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https" and parsed.hostname == "www.autodl.com"
            and parsed.port in (None, 443) and not parsed.username and not parsed.password
        )
    except ValueError:
        return False


def _read_token(url: str, port: int, stop: threading.Event, deadline: float) -> str | None:
    import websocket

    request_deadline = min(deadline, time.monotonic() + 2)
    connection = None
    direct_socket = None
    try:
        direct_socket = _local_socket(port, stop, request_deadline)
        connection = websocket.create_connection(
            url, socket=direct_socket, suppress_origin=True, redirect_limit=0,
            timeout=0.5, enable_multithread=False,
        )
        if connection.getstatus() != 101:
            raise _Unavailable()
        connection.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": _TOKEN_EXPRESSION, "returnByValue": True, "silent": True, "timeout": 1000},
        }))
        for _ in range(16):
            _check(stop, request_deadline)
            raw = connection.recv()
            if not isinstance(raw, (str, bytes)) or len(raw) > _MAX_JSON:
                raise ChromeLoginError("Chrome 连接返回内容异常，请关闭专用窗口后重试。")
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("id") != 1:
                continue
            result = message.get("result")
            if not isinstance(result, dict) or "exceptionDetails" in result:
                return None
            remote = result.get("result")
            if not isinstance(remote, dict) or remote.get("type") != "string":
                return None
            token = remote.get("value")
            if isinstance(token, str) and 0 < len(token) <= 16384 and not any(ord(char) < 33 or ord(char) > 126 for char in token):
                return token
            return None
        return None
    except (OSError, ValueError, RecursionError, websocket.WebSocketException):
        raise _Unavailable() from None
    finally:
        if connection is not None:
            connection.shutdown()
        if direct_socket is not None:
            try:
                direct_socket.close()
            except OSError:
                pass


def _deadline(timeout: float, maximum: float = 1800) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 1800:
        raise ChromeLoginError("Chrome 登录等待时间无效。")
    return time.monotonic() + min(timeout, maximum)


def _require_websocket() -> None:
    try:
        import websocket  # noqa: F401: PyInstaller discovers this literal import
    except ImportError:
        raise ChromeLoginError("当前程序缺少 Chrome 连接组件，请重新安装完整客户端。") from None


@contextmanager
def _safe_browser_errors():
    try:
        yield
    except ChromeLoginCancelled:
        raise
    except ChromeLoginError:
        raise
    except _Unavailable:
        raise ChromeLoginError("专用 Chrome 暂时无法响应，请等待页面加载后再次检测登录。") from None
    except Exception:
        # Third-party transport/browser errors can contain HTTP or JS details.
        # Never allow their raw message, response, or traceback into the UI.
        raise ChromeLoginError("无法连接专用 Chrome，请重新打开专用窗口后再次检测登录。") from None


def _pages(port: int, stop: threading.Event, deadline: float) -> list[dict]:
    pages = _http_json(port, "/json/list", stop, deadline)
    if not isinstance(pages, list) or len(pages) > 128:
        raise ChromeLoginError("Chrome 页面信息异常，请关闭专用窗口后重试。")
    return [page for page in pages if isinstance(page, dict) and page.get("type") == "page" and _autodl_page(page.get("url"))]


def _open_profile(profile: Path, stop: threading.Event, on_status: Callable[[str], None], deadline: float) -> None:
    _check(stop, deadline)
    port = _endpoint(profile, stop, deadline)
    if port is not None and _pages(port, stop, deadline):
        on_status("专用 Chrome 已打开；请在该窗口登录 AutoDL，完成后点击“检测登录”。")
        return
    _launch(profile)
    on_status("已打开专用 Chrome；请完成 AutoDL 登录，再回到客户端点击“检测登录”。")
    while time.monotonic() < deadline:
        _check(stop, deadline)
        if _endpoint(profile, stop, deadline) is not None:
            return
        if stop.wait(min(0.25, max(0.0, deadline - time.monotonic()))):
            _check(stop, deadline)
    raise ChromeLoginError("专用 Chrome 未能就绪，请关闭本应用打开的专用窗口后再点“打开 Chrome”；无需关闭日常 Chrome。")


def _detect_profile(profile: Path, stop: threading.Event, on_status: Callable[[str], None], deadline: float) -> str:
    on_status("正在检测专用 Chrome 中的 AutoDL 登录状态…")
    reason = "尚未检测到登录信息，请在专用 Chrome 完成登录后再次点击“检测登录”。"
    while time.monotonic() < deadline:
        _check(stop, deadline)
        port = _endpoint(profile, stop, deadline)
        if port is None:
            raise ChromeLoginError("专用 Chrome 尚未打开或连接已失效，请先点击“打开 Chrome”，再检测登录。")
        try:
            pages = _pages(port, stop, deadline)
            if not pages:
                reason = "专用 Chrome 中未找到 AutoDL 页面，请先点击“打开 Chrome”，登录后再检测。"
            else:
                reason = "尚未检测到登录信息，请在专用 Chrome 完成登录后再次点击“检测登录”。"
                for page in pages:
                    url = _websocket_url(page.get("webSocketDebuggerUrl"), port, "page")
                    if url is None:
                        continue
                    try:
                        token = _read_token(url, port, stop, deadline)
                    except _Unavailable:
                        reason = "AutoDL 页面正在加载或暂时无法响应，请稍后再次点击“检测登录”。"
                        continue
                    if token is not None:
                        _check(stop, deadline)
                        on_status("已检测到登录信息，正在交给客户端验证；本地读取连接已断开。")
                        return token
        except _Unavailable:
            reason = "专用 Chrome 暂时无法响应，请等待页面加载后再次检测登录。"
        if stop.wait(min(0.3, max(0.0, deadline - time.monotonic()))):
            _check(stop, deadline)
    raise ChromeLoginError(reason)


def open_chrome(data_dir: Path, stop: threading.Event, on_status: Callable[[str], None]) -> None:
    """Open/reuse only the owned Chrome window; never read login credentials."""
    with _safe_browser_errors():
        deadline = _deadline(12)
        _check(stop, deadline)
        _open_profile(_profile(data_dir), stop, on_status, deadline)


def detect_chrome(
    data_dir: Path,
    stop: threading.Event,
    on_status: Callable[[str], None],
    timeout: float = 6,
) -> str:
    """Inspect an existing owned session briefly, without creating any window.

    Returning a token only means it was found; the caller must verify it with
    AutoDL before replacing an existing account or showing a connected state.
    """
    with _safe_browser_errors():
        deadline = _deadline(timeout, maximum=30)
        _check(stop, deadline)
        _require_websocket()
        return _detect_profile(_profile(data_dir, create=False), stop, on_status, deadline)


def connect_chrome(
    data_dir: Path,
    stop: threading.Event,
    on_status: Callable[[str], None],
    timeout: float = 300,
) -> str:
    """Compatibility helper: open the owned browser, then wait for its login."""
    with _safe_browser_errors():
        deadline = _deadline(timeout)
        _check(stop, deadline)
        _require_websocket()
        profile = _profile(data_dir)
        _open_profile(profile, stop, on_status, min(deadline, time.monotonic() + 12))
        return _detect_profile(profile, stop, on_status, deadline)
