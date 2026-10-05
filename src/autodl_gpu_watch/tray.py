"""Small Windows notification icon; callbacks must only enqueue GUI events.

The hidden top-level window and every native resource belong to the worker
thread. A message-only window would miss Explorer's TaskbarCreated broadcast.
No Windows libraries are loaded on other platforms.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import sys
import threading
from typing import Callable


_WM_CLOSE, _WM_DESTROY, _WM_QUIT = 0x0010, 0x0002, 0x0012
_WM_QUERYENDSESSION, _WM_ENDSESSION = 0x0011, 0x0016
_WM_CONTEXTMENU, _WM_LBUTTONUP, _WM_RBUTTONUP = 0x007B, 0x0202, 0x0205
_WM_TRAY = 0x8001
_NIN_SELECT, _NIN_KEYSELECT = 0x0400, 0x0401
_NIM_ADD, _NIM_DELETE, _NIM_SETVERSION = 0, 2, 4
_SHOW, _EXIT = 1, 2
_START_TIMEOUT = 1.5
_WNDPROC = getattr(c, "WINFUNCTYPE", c.CFUNCTYPE)(
    c.c_ssize_t, w.HWND, w.UINT, c.c_size_t, c.c_ssize_t
)


class _WindowClass(c.Structure):
    _fields_ = [
        ("style", w.UINT), ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", c.c_int), ("cbWndExtra", c.c_int),
        ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
        ("hCursor", w.HANDLE), ("hbrBackground", w.HANDLE),
        ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR),
    ]


class _GUID(c.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD),
                ("Data3", w.WORD), ("Data4", c.c_ubyte * 8)]


class _NotifyIconData(c.Structure):
    _fields_ = [
        ("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT),
        ("uFlags", w.UINT), ("uCallbackMessage", w.UINT),
        ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
        ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
        ("szInfo", w.WCHAR * 256), ("uVersion", w.UINT),
        ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
        ("guidItem", _GUID), ("hBalloonIcon", w.HICON),
    ]


class _WindowsAPI:
    """Explicit pointer-sized signatures also make native calls easy to mock."""

    def __init__(self):
        user = c.WinDLL("user32", use_last_error=True)
        shell = c.WinDLL("shell32", use_last_error=True)
        kernel = c.WinDLL("kernel32", use_last_error=True)

        def bind(library, name, result, *arguments):
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
            setattr(self, name, function)

        bind(kernel, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
        bind(kernel, "GetCurrentThreadId", w.DWORD)
        bind(user, "RegisterWindowMessageW", w.UINT, w.LPCWSTR)
        bind(user, "RegisterClassW", w.ATOM, c.POINTER(_WindowClass))
        bind(user, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)
        bind(user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR,
             w.DWORD, c.c_int, c.c_int, c.c_int, c.c_int,
             w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
        bind(user, "DefWindowProcW", c.c_ssize_t, w.HWND, w.UINT, c.c_size_t, c.c_ssize_t)
        bind(user, "DestroyWindow", w.BOOL, w.HWND)
        bind(user, "PostMessageW", w.BOOL, w.HWND, w.UINT, c.c_size_t, c.c_ssize_t)
        bind(user, "PostThreadMessageW", w.BOOL, w.DWORD, w.UINT, c.c_size_t, c.c_ssize_t)
        bind(user, "PostQuitMessage", None, c.c_int)
        bind(user, "GetMessageW", w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
        bind(user, "TranslateMessage", w.BOOL, c.POINTER(w.MSG))
        bind(user, "DispatchMessageW", c.c_ssize_t, c.POINTER(w.MSG))
        bind(user, "LoadImageW", w.HANDLE, w.HINSTANCE, w.LPCWSTR,
             w.UINT, c.c_int, c.c_int, w.UINT)
        bind(user, "DestroyIcon", w.BOOL, w.HICON)
        bind(user, "GetSystemMetrics", c.c_int, c.c_int)
        bind(user, "CreatePopupMenu", w.HMENU)
        bind(user, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, c.c_size_t, w.LPCWSTR)
        bind(user, "DestroyMenu", w.BOOL, w.HMENU)
        bind(user, "GetCursorPos", w.BOOL, c.POINTER(w.POINT))
        bind(user, "SetForegroundWindow", w.BOOL, w.HWND)
        bind(user, "TrackPopupMenu", w.UINT, w.HMENU, w.UINT, c.c_int,
             c.c_int, c.c_int, w.HWND, c.POINTER(w.RECT))
        bind(shell, "Shell_NotifyIconW", w.BOOL, w.DWORD, c.POINTER(_NotifyIconData))


class TrayIcon:
    """One icon with a separate message loop; start confirms its availability.

    All callbacks run on the worker (or the caller when start fails early).
    They must be short queue writes, never Tk calls. stop is idempotent and
    waits at most one second; a stalled Windows shell cannot hold app exit.
    """

    def __init__(self, icon_path: str | Path, on_show: Callable[[], None],
                 on_exit: Callable[[], None], on_error: Callable[[str], None]):
        self._icon_path = str(Path(icon_path).resolve())
        self._on_show, self._on_exit, self._on_error = on_show, on_exit, on_error
        self._lock = threading.Lock()
        self._ready, self._stop = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._api = None
        self._hwnd = self._thread_id = 0
        self._present = self._error_reported = False
        self._version4 = False
        self._taskbar_message = 0
        self._data = None

    @property
    def alive(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive()
                        and self._present and not self._stop.is_set())

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._stop.is_set():
                    return False
            else:
                self._ready.clear()
                self._stop.clear()
                self._present = self._error_reported = False
                self._thread = threading.Thread(target=self._run, name="autodl-tray", daemon=True)
                try:
                    self._thread.start()
                except RuntimeError:
                    self._thread = None
                    self._ready.set()
        if self._thread is None:
            self._fail("系统托盘启动失败，窗口将保持显示。")
            return False
        if not self._ready.wait(_START_TIMEOUT):
            self._fail("系统托盘暂时无法响应，窗口将保持显示。")
            self._request_stop()
            return False
        return self.alive

    def _request_stop(self):
        self._stop.set()
        with self._lock:
            api, hwnd, thread_id, thread = self._api, self._hwnd, self._thread_id, self._thread
        if api:
            posted = False
            try:
                posted = bool(hwnd and api.PostMessageW(hwnd, _WM_CLOSE, 0, 0))
            except Exception:
                pass
            if not posted and thread_id:
                try:
                    api.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
                except Exception:
                    pass
        return thread

    def stop(self) -> None:
        thread = self._request_stop()
        if thread and thread is not threading.current_thread():
            try:
                thread.join(timeout=1.0)
            except RuntimeError:
                pass

    def _fail(self, message: str) -> None:
        with self._lock:
            if self._error_reported:
                return
            self._error_reported = True
            self._present = False
        try:
            self._on_error(message)
        except Exception:
            # Exceptions must never escape a native WNDPROC callback.
            pass

    def _callback(self, callback) -> None:
        try:
            callback()
        except Exception:
            self._fail("托盘操作未完成，请在主窗口继续操作。")

    def _add_icon(self) -> bool:
        self._data.uVersion = 0
        if not self._api.Shell_NotifyIconW(_NIM_ADD, c.byref(self._data)):
            return False
        self._data.uVersion = 4
        self._version4 = bool(self._api.Shell_NotifyIconW(_NIM_SETVERSION, c.byref(self._data)))
        with self._lock:
            self._present = True
        return True

    def _menu(self, hwnd) -> None:
        api = self._api
        menu = api.CreatePopupMenu()
        if not menu:
            raise OSError("Cannot create tray menu")
        try:
            if not (api.AppendMenuW(menu, 0, _SHOW, "显示窗口")
                    and api.AppendMenuW(menu, 0, _EXIT, "退出")):
                raise OSError("Cannot populate tray menu")
            point = w.POINT()
            if not api.GetCursorPos(c.byref(point)):
                raise OSError("Cannot locate tray menu")
            api.SetForegroundWindow(hwnd)
            # TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON.
            command = api.TrackPopupMenu(menu, 0x0182, point.x, point.y, 0, hwnd, None)
            api.PostMessageW(hwnd, 0, 0, 0)
            if command == _SHOW:
                self._callback(self._on_show)
            elif command == _EXIT:
                self._callback(self._on_exit)
        finally:
            api.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        api = self._api
        try:
            if message == _WM_QUERYENDSESSION:
                return 1  # Never veto Windows logoff/shutdown.
            if message == _WM_ENDSESSION:
                if wparam:
                    self._stop.set()
                    self._callback(self._on_exit)
                    api.DestroyWindow(hwnd)
                return 0
            if message == _WM_CLOSE:
                api.DestroyWindow(hwnd)
                return 0
            if message == _WM_DESTROY:
                with self._lock:
                    self._hwnd = 0
                api.PostQuitMessage(0)
                return 0
            if self._stop.is_set():
                return api.DefWindowProcW(hwnd, message, wparam, lparam)
            if message == self._taskbar_message and message:
                if self._data is None:
                    return 0
                with self._lock:
                    self._present = False
                if not self._add_icon():
                    self._fail("Windows 托盘重建失败，已恢复主窗口。")
                    api.DestroyWindow(hwnd)
                return 0
            if message == _WM_TRAY:
                event = lparam & 0xFFFF if self._version4 else lparam
                show_events = (_NIN_SELECT, _NIN_KEYSELECT) if self._version4 else (_WM_LBUTTONUP,)
                menu_event = _WM_CONTEXTMENU if self._version4 else _WM_RBUTTONUP
                if event in show_events:
                    self._callback(self._on_show)
                elif event == menu_event:
                    self._menu(hwnd)
                return 0
            return api.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception:
            self._fail("系统托盘发生错误，已恢复主窗口。")
            try:
                api.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            except Exception:
                pass
            return 0

    def _run(self) -> None:
        api = None
        icon = instance = 0
        registered = False
        class_name = f"AutoDLGPUWatchTray_{id(self):x}"
        window_proc = _WNDPROC(self._window_proc)  # Keep callback alive until class removal.
        try:
            api = _WindowsAPI()
            with self._lock:
                self._api, self._thread_id = api, api.GetCurrentThreadId()
            instance = api.GetModuleHandleW(None)
            self._taskbar_message = api.RegisterWindowMessageW("TaskbarCreated")
            if not instance or not self._taskbar_message:
                raise OSError("Cannot register tray messages")
            icon = api.LoadImageW(None, self._icon_path, 1,
                                  api.GetSystemMetrics(49), api.GetSystemMetrics(50), 0x0010)
            if not icon:
                raise OSError("Cannot load tray icon")
            window_class = _WindowClass(lpfnWndProc=window_proc, hInstance=instance,
                                        lpszClassName=class_name)
            if not api.RegisterClassW(c.byref(window_class)):
                raise OSError("Cannot register tray window")
            registered = True
            # Invisible top-level window receives Explorer and shutdown broadcasts.
            hwnd = api.CreateWindowExW(0, class_name, "AutoDL Aoao", 0,
                                      0, 0, 0, 0, None, None, instance, None)
            if not hwnd:
                raise OSError("Cannot create tray window")
            with self._lock:
                self._hwnd = hwnd
            if self._stop.is_set():
                return
            self._data = _NotifyIconData(
                cbSize=c.sizeof(_NotifyIconData), hWnd=hwnd, uID=1,
                uFlags=0x0087, uCallbackMessage=_WM_TRAY, hIcon=icon,
                szTip="AutoDL Aoao · 点击显示窗口",
            )
            if not self._add_icon():
                raise OSError("Cannot add tray icon")
            self._ready.set()
            message = w.MSG()
            while not self._stop.is_set():
                result = api.GetMessageW(c.byref(message), None, 0, 0)
                if result <= 0:
                    if not self._stop.is_set() and not self._error_reported:
                        self._fail("系统托盘已停止，已恢复主窗口。")
                    break
                api.TranslateMessage(c.byref(message))
                api.DispatchMessageW(c.byref(message))
        except Exception:
            if not self._stop.is_set():
                self._fail("系统托盘不可用，窗口将保持显示。")
        finally:
            if api:
                cleanup = []
                if self._data is not None:
                    cleanup.append((api.Shell_NotifyIconW, (_NIM_DELETE, c.byref(self._data))))
                with self._lock:
                    hwnd = self._hwnd
                if hwnd:
                    cleanup.append((api.DestroyWindow, (hwnd,)))
                if registered:
                    cleanup.append((api.UnregisterClassW, (class_name, instance)))
                if icon:
                    cleanup.append((api.DestroyIcon, (icon,)))
                for release, arguments in cleanup:
                    try:
                        release(*arguments)
                    except Exception:
                        # One failed release must not skip the remaining handles
                        # or leave start waiting forever for the ready signal.
                        if not self._stop.is_set():
                            self._fail("系统托盘已停止，已恢复主窗口。")
            with self._lock:
                self._present = False
                self._api = self._data = None
                self._hwnd = self._thread_id = 0
            self._ready.set()
