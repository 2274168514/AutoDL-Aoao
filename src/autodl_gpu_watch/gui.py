"""Compact native client. Worker events enter a queue, never Tk directly."""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack
from datetime import datetime
from importlib import resources
import queue
import re
import sys
import webbrowser
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from .config import ConfigError
from .desktop import DesktopController

BG, WHITE, INK, MUTED = "#F6F8FB", "#FFFFFF", "#24364D", "#7A8798"
BORDER, BLUE, GREEN = "#DDE5EE", "#2869CF", "#21856B"
SMTP_AUTH_HELP_URL = "https://mail.163.com/"


def _enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _safe_label(value):
    return "".join(c if c.isprintable() else " " for c in str(value))[:240]


def _friendly_error(message):
    text = str(message)
    specific = {
        "email.recipients 必须是至少含一个地址的数组": "请填写发件邮箱，或在更多设置中填写收件邮箱。",
        "targets 必须包含 1～100 个监控目标": "请先选择要监控的实例。",
        "target.kind 只支持 instance 或 machine": "目标类型只能选择实例或机器。",
        "machine 目标必须指定 min_gpus": "监控机器时，请填写需要的 GPU 数量。",
    }
    if text in specific:
        return specific[text]
    fields = {
        "email.host": "邮件服务器", "email.port": "邮件端口", "email.security": "邮件加密方式",
        "email.sender": "发件邮箱", "email.recipients": "收件邮箱", "email.password_env": "授权码变量名",
        "email.timeout": "邮件请求超时", "autodl.token_env": "登录信息变量名", "autodl.sub_account": "子账号名称",
        "autodl.app_version": "网页版本", "target.kind": "目标类型", "target.value": "实例名称或 ID",
        "target.min_gpus": "需要 GPU 数量", "min_gpus": "需要 GPU 数量", "poll_seconds": "检查间隔",
        "timeout_seconds": "请求超时", "cooldown_seconds": "提醒冷却", "state_file": "提醒状态文件",
        "targets": "监控目标", "autodl": "AutoDL 设置", "email": "邮件设置", "target": "监控目标",
    }
    pattern = r"(?<![A-Za-z0-9_.])(?:" + "|".join(re.escape(k) for k in sorted(fields, key=len, reverse=True)) + r")(?![A-Za-z0-9_.])"
    return re.sub(pattern, lambda match: fields[match.group()], text)


class _ScrollPane(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0, width=1, height=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.body = ttk.Frame(self.canvas, padding=(8, 10, 14, 10))
        self.body.columnconfigure(0, weight=1)
        self.window = self.canvas.create_window((0, 0), anchor="nw", window=self.body)
        self.canvas.bind("<Configure>", self.fit)
        self.body.bind("<Configure>", self.fit)

    def fit(self, event=None):
        width = max(1, self.canvas.winfo_width())
        self.canvas.itemconfigure(self.window, width=width)
        self.canvas.configure(scrollregion=(0, 0, width, self.body.winfo_reqheight()))


class DesktopApp:
    def __init__(self, root: tk.Tk, controller_factory: Callable = DesktopController):
        self.root = root
        self._events = queue.Queue()
        self._after_id = None
        self._closing = self._stop_requested = self._built = False
        self._destroyed = self._hidden = False
        self._hidden_windows = []
        self._restore_state = "normal"
        self._pending_instances = None
        self._tray = self._tray_resources = None
        self._tray_generation = 0
        self._active_action = ""
        self._operation_error = None
        self._primary_mode = "wait"
        self._monitor_was_running = False
        self._auto_start_result = None
        self._status_full, self._status_error = "打开 Chrome 登录后，点击“检测登录”。", False
        self._editable, self._picker_widgets = [], []
        self._live_widgets = set()
        self._log_lines = deque(maxlen=200)
        self._observations, self._instance_names = {}, {}
        self._picker = self._more = self._manual = self.log_text = None
        self.controller = controller_factory(self._emit)
        try:
            self._profile = self.controller.load_profile()
        except (ConfigError, OSError, ValueError):
            self._profile = {}
            self._status_full, self._status_error = "本地设置无法读取，请重新填写。", True
        self._configure_window()
        self._make_variables()
        self._build()
        self._built = True
        self._render_targets()
        for name in ("token", "sub_account", "app_version"):
            self.vars[name].trace_add("write", self._credentials_changed)
        for name in ("sender", "recipients"):
            self.vars[name].trace_add("write", lambda *args: self._refresh_recipient_hint())
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Tk dispatches Windows session shutdown through this protocol too.
        root.protocol("WM_SAVE_YOURSELF", self._session_query)
        if sys.platform == "darwin":
            root.createcommand("::tk::mac::Quit", self._request_exit)
        root.bind("<Configure>", self._window_resized, add="+")
        root.bind("<Map>", self._window_mapped, add="+")
        if sys.platform == "win32":
            self._ensure_tray()
        self._after_id = root.after(80, self._drain_events)

    def _configure_window(self):
        root = self.root
        root.title("AutoDL Aoao")
        root.configure(background=BG)
        self._scale = max(1.0, min(2.0, float(root.tk.call("tk", "scaling")) / (96 / 72)))
        width = min(round(680 * self._scale), root.winfo_screenwidth() - 60)
        height = min(round(475 * self._scale), root.winfo_screenheight() - 90)
        root.geometry(f"{max(560, width)}x{max(450, height)}")
        root.minsize(560, 450)
        families = set(tkfont.families(root))
        self._font = next((f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Arial") if f in families), "TkDefaultFont")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=self._font, size=10)
        self._small_font = tkfont.Font(root, family=self._font, size=9)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=(self._font, 10), background=BG, foreground=INK)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Muted.TLabel", foreground=MUTED, font=(self._font, 9))
        style.configure("TButton", padding=(11, 6), background=WHITE, bordercolor=BORDER, borderwidth=1, focusthickness=1, focuscolor=BLUE)
        style.map("TButton", background=[("active", "#EEF3FA")], foreground=[("disabled", "#A1ADBC")])
        style.configure("Accent.TButton", background=BLUE, foreground=WHITE, bordercolor=BLUE)
        style.map("Accent.TButton", background=[("disabled", "#D5E1F2"), ("active", "#2059B0")], foreground=[("disabled", "#8CA1BE"), ("!disabled", WHITE)])
        style.configure("Link.TButton", background=BG, foreground=BLUE, borderwidth=0, padding=(0, 4), anchor="w")
        style.map("Link.TButton", background=[("active", BG)], foreground=[("disabled", "#A1ADBC"), ("!disabled", BLUE)])
        style.configure("Small.Link.TButton", font=(self._font, 9), padding=(0, 0))
        style.configure("TEntry", padding=(7, 6), fieldbackground=WHITE, bordercolor=BORDER)
        style.map("TEntry", fieldbackground=[("disabled", "#EDF1F6")], foreground=[("disabled", MUTED)])
        style.configure("TCombobox", padding=(6, 6), fieldbackground=WHITE, bordercolor=BORDER)
        style.map("TCombobox", fieldbackground=[("disabled", "#EDF1F6"), ("readonly", WHITE)])
        style.configure("TCheckbutton", background=BG)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("Treeview", background=WHITE, fieldbackground=WHITE, foreground=INK, rowheight=round(29 * self._scale), bordercolor=BORDER)
        style.configure("Treeview.Heading", background="#EDF2F8", foreground=MUTED, padding=(7, 6), font=(self._font, 9))
        style.map("Treeview", background=[("selected", "#E7F0FE")], foreground=[("selected", INK)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(12, 6))
        try:
            asset = resources.files("autodl_gpu_watch").joinpath("assets/app.png")
            with resources.as_file(asset) as path:
                self._window_icon = tk.PhotoImage(master=root, file=str(path))
            # Tk retains the decoded pixels after the resource context closes.
            self._window_icons = [
                self._window_icon.subsample(max(1, self._window_icon.width() // size))
                for size in (32, 64)
            ] + [self._window_icon]
        except (OSError, tk.TclError, ModuleNotFoundError):
            self._window_icon = tk.PhotoImage(master=root, width=32, height=32)
            self._window_icon.put(BLUE, to=(0, 0, 32, 32))
            self._window_icon.put(WHITE, to=(8, 9, 25, 24))
            self._window_icon.put(BLUE, to=(10, 11, 23, 22))
            for x in (11, 16, 21):
                self._window_icon.put(WHITE, to=(x, 5, x + 2, 9))
                self._window_icon.put(WHITE, to=(x, 24, x + 2, 28))
            self._window_icons = [self._window_icon]
        root.iconphoto(True, *self._window_icons)

    def _make_variables(self):
        p = self._profile
        ad, mail = p.get("autodl", {}), p.get("email", {})
        values = {
            "token": p.get("_token", ""), "password": p.get("_password", ""),
            "sub_account": ad.get("sub_account", ""), "app_version": ad.get("app_version", ""),
            "sender": mail.get("sender", ""), "recipients": "; ".join(mail.get("recipients", [])),
            "host": mail.get("host", "smtp.163.com"), "port": str(mail.get("port", 465)),
            "security": mail.get("security", "ssl"), "poll_seconds": str(p.get("poll_seconds", 60)),
            "timeout_seconds": str(p.get("timeout_seconds", 20)), "cooldown_seconds": str(p.get("cooldown_seconds", 300)),
            "target_kind": "实例", "target_value": "", "target_min": "",
        }
        self.vars = {k: tk.StringVar(self.root, value=v) for k, v in values.items()}
        self.vars["remember"] = tk.BooleanVar(self.root, value=p.get("_remember", False))
        self.vars["auto_start"] = tk.BooleanVar(self.root, value=False)
        self._targets = [dict(t) for t in p.get("targets", [])]
        self._connection_state = "unverified" if values["token"] else "disconnected"
        self.connection_var = tk.StringVar(self.root, value="待验证" if values["token"] else "未连接")
        self.status_var = tk.StringVar(self.root, value=self._status_full)
        self.recipient_hint_var = tk.StringVar(self.root)
        self._refresh_recipient_hint()

    def _refresh_recipient_hint(self):
        sender = self.vars["sender"].get().strip()
        recipients = [part.strip() for part in re.split(r"[,，;；\n]+", self.vars["recipients"].get()) if part.strip()]
        self.recipient_hint_var.set("发给自己" if not recipients or recipients == [sender] else "改为发给自己")

    def _track(self, widget, state="normal", live=False):
        self._editable.append((widget, state))
        if live:
            self._live_widgets.add(widget)
        return widget

    def _entry(self, parent, name, live=False, **options):
        return self._track(ttk.Entry(parent, textvariable=self.vars[name], **options), live=live)

    def _button(self, parent, text, command, style="TButton", live=False):
        return self._track(ttk.Button(parent, text=text, command=command, style=style), live=live)

    def _field(self, parent, title, name, masked=False, auth_help=False):
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        heading = ttk.Frame(frame)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text=title).grid(row=0, column=0, sticky="w")
        if auth_help:
            self.smtp_help_button = self._button(heading, "获取授权码", self._open_smtp_help, "Small.Link.TButton")
            self.smtp_help_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self._entry(frame, name, show="*" if masked else "").grid(row=1, column=0, sticky="ew")
        return frame

    def _build(self):
        body = ttk.Frame(self.root, padding=(16, 12, 16, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        connection = ttk.Frame(body)
        connection.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        connection.columnconfigure(3, weight=1)
        self.connect_button = self._button(connection, "打开 Chrome", lambda: self._action("open_chrome"))
        self.connect_button.grid(row=0, column=0)
        self.detect_button = self._button(connection, "检测登录", lambda: self._action("detect_chrome"))
        self.detect_button.grid(row=0, column=1, padx=(8, 0))
        self.connection_label = ttk.Label(connection, textvariable=self.connection_var, style="Muted.TLabel")
        self.connection_label.grid(row=0, column=2, padx=(10, 0))
        self.fetch_button = self._button(connection, "选择实例", lambda: self._action("fetch_instances"), live=True)
        self.fetch_button.grid(row=0, column=4)
        table = ttk.Frame(body)
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.targets_tree = ttk.Treeview(table, columns=("name", "gpu", "status"), show="headings", height=4, selectmode="extended")
        for key, title, width, stretch in (("name", "监控目标", 300, True), ("gpu", "空闲 / 需要", 105, False), ("status", "状态", 105, False)):
            self.targets_tree.heading(key, text=title, anchor="w")
            self.targets_tree.column(key, width=round(width * self._scale), minwidth=70, stretch=stretch, anchor="w")
        self.targets_tree.tag_configure("available", foreground=GREEN)
        self.targets_tree.tag_configure("muted", foreground=MUTED)
        self.targets_tree.tag_configure("warning", foreground="#B04B47")
        self.targets_tree.grid(row=0, column=0, sticky="nsew")
        sy = ttk.Scrollbar(table, orient="vertical", command=self.targets_tree.yview)
        sy.grid(row=0, column=1, sticky="ns")
        self.targets_tree.configure(yscrollcommand=sy.set)
        self.targets_tree.bind("<<TreeviewSelect>>", lambda event: self._target_selection())
        links = ttk.Frame(body)
        links.grid(row=2, column=0, sticky="ew", pady=(3, 8))
        self.manual_button = self._button(links, "手动添加", self._show_manual, "Link.TButton", live=True)
        self.manual_button.pack(side="left")
        self.remove_button = self._button(links, "移除", self._remove_target, "Link.TButton")
        self.remove_button.pack(side="left", padx=(17, 0))
        self.mail_area = ttk.Frame(body)
        self.mail_area.grid(row=3, column=0, sticky="ew")
        self.mail_area.columnconfigure(0, weight=1, uniform="mail")
        self.mail_area.columnconfigure(1, weight=1, uniform="mail")
        self._mail_fields = [self._field(self.mail_area, "发件邮箱", "sender"), self._field(self.mail_area, "SMTP 授权码", "password", True, auth_help=True)]
        self.mail_area.bind("<Configure>", self._layout_mail)
        email_options = ttk.Frame(body)
        email_options.grid(row=4, column=0, sticky="ew", pady=(5, 8))
        email_options.columnconfigure(3, weight=1)
        self.self_recipient_button = self._track(ttk.Button(email_options, textvariable=self.recipient_hint_var, command=self._set_self_recipient, style="Link.TButton"), live=True)
        self.self_recipient_button.grid(row=0, column=0)
        self.test_email_button = self._button(email_options, "测试邮件", lambda: self._action("test_email"), "Link.TButton", live=True)
        self.test_email_button.grid(row=0, column=1, padx=(13, 0))
        remember_label = "记住凭据" if sys.platform == "win32" else "记住凭据（本地明文）"
        self._track(ttk.Checkbutton(email_options, text=remember_label, variable=self.vars["remember"])).grid(row=0, column=4)
        self.auto_start_button = self._track(ttk.Checkbutton(
            email_options, text="自动开机", variable=self.vars["auto_start"], command=self._auto_start_changed,
        ))
        self.auto_start_button.grid(row=0, column=2, sticky="w", padx=(13, 0))
        footer = ttk.Frame(body)
        footer.grid(row=5, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)
        self.more_button = ttk.Button(footer, text="更多设置", command=self._show_more, style="Link.TButton")
        self.more_button.grid(row=0, column=0, sticky="w")
        self.start_button = ttk.Button(footer, text="开始监控", command=self._primary_action, style="Accent.TButton", width=13)
        self.start_button.grid(row=0, column=2, sticky="e")
        self.stop_button = self.start_button
        self.status_label = ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel", width=1, anchor="w")
        self.status_label.grid(row=0, column=1, sticky="ew", padx=(14, 12))
        self.status_label.bind("<Configure>", lambda event: self._render_status())

    def _new_window(self, title, width, height):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.configure(background=BG)
        window.transient(self.root)
        w = min(round(width * self._scale), self.root.winfo_screenwidth() - 70)
        h = min(round(height * self._scale), self.root.winfo_screenheight() - 100)
        window.geometry(f"{w}x{h}")
        window.minsize(min(width, 440), min(height, 300))
        return window

    def _close_aux(self, name):
        window = getattr(self, "_" + name)
        if window is not None and window.winfo_exists():
            window.destroy()
        setattr(self, "_" + name, None)
        if name == "more":
            self.log_text = None
        self._editable = [(w, s) for w, s in self._editable if w.winfo_exists()]
        self._live_widgets.intersection_update(w for w, _ in self._editable)
        self._refresh_controls()

    def _show_more(self):
        if self._closing:
            return
        if self._more is not None and self._more.winfo_exists():
            self._more.lift()
            return
        window = self._new_window("更多设置", 540, 490)
        self._more = window
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_aux("more"))
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        tabs = ttk.Notebook(window)
        tabs.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 0))
        general, login = _ScrollPane(tabs), _ScrollPane(tabs)
        logs = ttk.Frame(tabs, padding=8)
        tabs.add(general, text="设置")
        tabs.add(login, text="手工登录")
        tabs.add(logs, text="活动记录")
        body = general.body
        self._field(body, "收件邮箱（留空则发给自己）", "recipients").grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._field(body, "邮件服务器", "host").grid(row=1, column=0, sticky="ew", pady=(0, 10))
        pair = ttk.Frame(body)
        pair.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        pair.columnconfigure(0, weight=1)
        pair.columnconfigure(1, weight=1)
        self._field(pair, "端口", "port").grid(row=0, column=0, sticky="ew", padx=(0, 10))
        security = ttk.Frame(pair)
        security.grid(row=0, column=1, sticky="ew")
        security.columnconfigure(0, weight=1)
        ttk.Label(security, text="连接加密").grid(row=0, column=0, sticky="w", pady=(0, 5))
        self._track(ttk.Combobox(security, textvariable=self.vars["security"], values=("ssl", "starttls"), state="readonly", width=9), "readonly").grid(row=1, column=0, sticky="ew")
        timing = ttk.Frame(body)
        timing.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        timing.columnconfigure(1, weight=1)
        for i, (label, key) in enumerate((("检查间隔（秒）", "poll_seconds"), ("请求超时（秒）", "timeout_seconds"), ("提醒冷却（秒）", "cooldown_seconds"))):
            ttk.Label(timing, text=label).grid(row=i, column=0, sticky="w", padx=(0, 12), pady=4)
            self._entry(timing, key, width=9).grid(row=i, column=1, sticky="ew", pady=4)
        storage = ttk.Label(body, text=self.controller.secret_storage_label, style="Muted.TLabel", wraplength=430)
        storage.grid(row=4, column=0, sticky="w", pady=(0, 6))
        path = ttk.Label(body, text="配置位置：" + str(self.controller.data_dir), style="Muted.TLabel", wraplength=430)
        path.grid(row=5, column=0, sticky="w")
        body.bind("<Configure>", lambda e: (storage.configure(wraplength=max(170, e.width - 25)), path.configure(wraplength=max(170, e.width - 25))), add="+")
        for i, (label, name, masked) in enumerate((("登录 Token", "token", True), ("子账号名称（主账号留空）", "sub_account", False), ("网页 AppVersion（可选）", "app_version", False))):
            self._field(login.body, label, name, masked).grid(row=i, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(login.body, text="也可以打开 Chrome 登录后，点击“检测登录”。", style="Muted.TLabel", wraplength=400).grid(row=3, column=0, sticky="w")
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(0, weight=1)
        self.log_text = tk.Text(logs, state="disabled", wrap="word", background=WHITE, foreground=MUTED, font=(self._font, 9), relief="flat", padx=8, pady=8, height=8, width=1)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(logs, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        self._render_log()
        footer = ttk.Frame(window, padding=12)
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)
        self.check_button = self._button(footer, "检查一次", lambda: self._action("check"), "Link.TButton")
        self.check_button.grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="退出程序", command=self._request_exit, style="Link.TButton").grid(row=0, column=1, sticky="w", padx=(16, 0))
        self._button(footer, "保存设置", lambda: self._action("save_profile")).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(footer, text="完成", command=lambda: self._close_aux("more")).grid(row=0, column=3, padx=(8, 0))
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel", width=1).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            window.bind(event, self._scroll_wheel, add="+")
        self._refresh_controls()

    def _show_manual(self):
        if not self._can_live_action():
            return
        if self._manual is not None and self._manual.winfo_exists():
            self._manual.lift()
            return
        window = self._new_window("手动添加", 420, 245)
        self._manual = window
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_aux("manual"))
        body = ttk.Frame(window, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        selector = self._track(ttk.Combobox(body, textvariable=self.vars["target_kind"], values=("实例", "机器"), state="readonly", width=7), "readonly", live=True)
        selector.grid(row=0, column=0, padx=(0, 10))
        entry = self._entry(body, "target_value", live=True)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<Return>", lambda event: self._add_target())
        hint_text = lambda: "填写市场主机 ID，无需先租用。" if self.vars["target_kind"].get() == "机器" else "填写完整实例名或 ID"
        hint = ttk.Label(body, text=hint_text(), style="Muted.TLabel")
        hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 12))
        selector.bind("<<ComboboxSelected>>", lambda event: hint.configure(text=hint_text()))
        ttk.Label(body, text="需要 GPU").grid(row=2, column=0, sticky="w")
        minimum_entry = self._entry(body, "target_min", live=True, width=7)
        minimum_entry.grid(row=2, column=1, sticky="w")
        minimum_entry.bind("<Return>", lambda event: self._add_target())
        ttk.Label(body, text="实例可留空；机器需填卡数，仅提醒。", style="Muted.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 12))
        self._button(body, "添加", self._add_target, "Accent.TButton", live=True).grid(row=4, column=1, sticky="e")
        ttk.Label(body, textvariable=self.status_var, style="Muted.TLabel", width=1).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._refresh_controls()

    def _layout_mail(self, event=None):
        compact = self.mail_area.winfo_width() < 430
        if getattr(self, "_mail_compact", None) == compact:
            return
        self._mail_compact = compact
        for i, field in enumerate(self._mail_fields):
            if compact:
                field.grid(row=i, column=0, columnspan=2, sticky="ew", padx=0, pady=(0, 7 if i == 0 else 0))
            else:
                field.grid(row=0, column=i, columnspan=1, sticky="ew", padx=(0, 6) if i == 0 else (6, 0), pady=0)

    def _window_resized(self, event=None):
        if self._built and (event is None or event.widget is self.root):
            self._render_status()

    def _window_mapped(self, event):
        if event.widget is self.root and not self._closing and self.root.state() not in ("withdrawn", "iconic"):
            if self._hidden or self._pending_instances is not None:
                self._restore_window()

    def _session_query(self):
        # Windows may still cancel a shutdown query. A healthy tray receives
        # the later confirmed WM_ENDSESSION and then enqueues the real exit.
        if sys.platform == "win32" and self._tray is not None and self._tray.alive:
            return
        self._emit("session_exit", None)

    def _ensure_tray(self):
        if sys.platform != "win32" or self._closing:
            return False
        if self._tray is not None and self._tray.alive:
            return True
        self._stop_tray()
        self._tray_generation += 1
        generation = self._tray_generation
        self._tray_resources = ExitStack()
        try:
            from .tray import TrayIcon

            asset = resources.files("autodl_gpu_watch").joinpath("assets/app.ico")
            path = self._tray_resources.enter_context(resources.as_file(asset))
            # Native callbacks run on the tray thread. Only the Tk queue is
            # touched here; no widget or controller method crosses threads.
            self._tray = TrayIcon(
                path,
                on_show=lambda: self._emit("tray_show", generation),
                on_exit=lambda: self._emit("tray_exit", generation),
                on_error=lambda message: self._emit("tray_error", (generation, str(message))),
            )
            if self._tray.start() and self._tray.alive:
                return True
        except (ImportError, OSError, RuntimeError, ValueError):
            pass
        self._stop_tray()
        self._emit("tray_error", (generation, "系统托盘暂不可用，窗口可从任务栏恢复。"))
        return False

    def _stop_tray(self):
        tray, self._tray = self._tray, None
        stack, self._tray_resources = self._tray_resources, None
        try:
            if tray is not None:
                tray.stop()
        finally:
            if stack is not None:
                stack.close()

    def _restore_window(self):
        if self._destroyed:
            return
        if not self._hidden and self.root.state() not in ("withdrawn", "iconic"):
            self._restore_state = self.root.state()
        self._hidden = False
        self.root.deiconify()
        if self._restore_state == "zoomed":
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass
        self.root.lift()
        windows, self._hidden_windows = self._hidden_windows, []
        for window, state in windows:
            if window.winfo_exists() and not self._closing:
                window.deiconify()
                if state in ("zoomed", "iconic"):
                    try:
                        window.state(state)
                    except tk.TclError:
                        pass
        pending, self._pending_instances = self._pending_instances, None
        if pending is not None and not self._closing:
            self._show_instances(pending)

    def _tray_failed(self, message):
        self._stop_tray()
        if self._closing or self._destroyed:
            return
        self._restore_window()
        if self._operation_error is None:
            self._operation_error = _safe_label(message)
            self._set_status(self._operation_error, True)
        self._append_log(message)

    def _scroll_wheel(self, event):
        if isinstance(event.widget, (tk.Text, ttk.Treeview)):
            return
        widget = event.widget
        while widget is not None and not isinstance(widget, _ScrollPane):
            widget = getattr(widget, "master", None)
        if widget is None:
            return
        direction = -1 if getattr(event, "num", None) == 4 else 1 if getattr(event, "num", None) == 5 else -1 if event.delta > 0 else 1
        if widget.body.winfo_reqheight() > widget.canvas.winfo_height():
            widget.canvas.yview_scroll(direction * 3, "units")
            return "break"

    @staticmethod
    def _numeric(text, integer=False):
        try:
            return int(text) if integer else float(text)
        except (ValueError, OverflowError):
            return text

    def _collect_profile(self):
        text = lambda key: str(self.vars[key].get()).strip()
        recipients = [p.strip() for p in re.split(r"[,，;；\n]+", text("recipients")) if p.strip()]
        if not recipients and text("sender"):
            recipients = [text("sender")]
        return {
            "autodl": {"token_env": self._profile.get("autodl", {}).get("token_env", "AUTODL_TOKEN"), "sub_account": text("sub_account"), "app_version": text("app_version")},
            "email": {"host": text("host"), "port": self._numeric(text("port"), True), "security": text("security"), "sender": text("sender"),
                      "recipients": recipients, "password_env": self._profile.get("email", {}).get("password_env", "SMTP_PASSWORD"), "timeout": self._numeric(text("timeout_seconds"))},
            "targets": [dict(t) for t in self._targets], "poll_seconds": self._numeric(text("poll_seconds")),
            "timeout_seconds": self._numeric(text("timeout_seconds")), "cooldown_seconds": self._numeric(text("cooldown_seconds")),
            "auto_start": bool(self.vars["auto_start"].get()),
            "state_file": self._profile.get("state_file", "state.json"), "_token": text("token"), "_password": text("password"), "_remember": bool(self.vars["remember"].get()),
        }

    def _auto_start_changed(self):
        self._auto_start_result = None
        self._render_targets()

    def _primary_action(self):
        if self._closing:
            return
        # Follow the action displayed by the last UI refresh. The worker may
        # finish between that refresh and a click on the visible Cancel button.
        if self._primary_mode == "stop":
            if self.controller.busy and not self._stop_requested:
                self._stop()
            else:
                self._refresh_controls()
            return
        if self._primary_mode != "start":
            return
        if self.controller.busy or self._is_stopping():
            self._refresh_controls()
            return
        self._action("start")

    def _is_stopping(self):
        return bool(getattr(self.controller, "stopping", False))

    def _can_live_action(self):
        action_busy = getattr(self.controller, "action_busy", self.controller.busy and not self.controller.running)
        return not (self._closing or self._stop_requested or self._is_stopping() or action_busy) and (not self.controller.busy or self.controller.running)

    def _action(self, method):
        live = method in ("fetch_instances", "test_email")
        if (live and not self._can_live_action()) or (not live and (self._closing or self.controller.busy or self._is_stopping())):
            return
        profile = self._collect_profile()
        self._active_action, self._stop_requested = method, False
        self._operation_error = None
        try:
            getattr(self.controller, method)(profile)
        except (ConfigError, RuntimeError) as error:
            self._operation_error = _friendly_error(error)
            self._set_status(self._operation_error, True)
            return
        if method in ("save_profile", "start"):
            self._profile = profile
        if method == "start":
            self._monitor_was_running = True
            self._auto_start_result = None
            self._render_targets()
        if method == "save_profile":
            self._set_status("设置已保存。")
        if method in ("connect_chrome", "open_chrome", "detect_chrome") and self.controller.busy:
            self._connection_state = "connecting"
            self._set_status("正在检测登录…" if method == "detect_chrome" else "正在打开 Chrome…")
            self._refresh_connection()
        self._refresh_controls()

    def _commit_targets(self, targets):
        if not self._can_live_action():
            return False
        profile = self._collect_profile()
        profile["targets"] = [dict(target) for target in targets]
        if self.controller.running:
            try:
                self.controller.update_live_profile(profile)
            except (ConfigError, RuntimeError) as error:
                self._operation_error = _friendly_error(error)
                self._set_status(self._operation_error, True)
                self._refresh_controls()
                return False
            self._profile = profile
        old_keys = {(target["kind"], target["value"]) for target in self._targets}
        for target in targets:
            if (target["kind"], target["value"]) not in old_keys:
                self._observations.pop(target["kind"] + ":" + target["value"], None)
        # The local list changes only after the running monitor accepts it.
        self._targets = profile["targets"]
        self._operation_error = None
        self._render_targets()
        return True

    def _set_self_recipient(self):
        if not self._can_live_action():
            return
        sender = self.vars["sender"].get().strip()
        if not sender:
            self._operation_error = "请先填写发件邮箱。"
            self._set_status(self._operation_error, True)
            return
        profile = self._collect_profile()
        profile["email"]["recipients"] = [sender]
        if self.controller.running:
            try:
                self.controller.update_live_profile(profile)
            except (ConfigError, RuntimeError) as error:
                self._operation_error = _friendly_error(error)
                self._set_status(self._operation_error, True)
                self._refresh_controls()
                return
            self._profile = profile
        self.vars["recipients"].set(sender)
        self._operation_error = None
        self._set_status("后续邮件将发给自己。")
        self._refresh_controls()

    def _open_smtp_help(self):
        if self._closing or self.controller.busy or self._is_stopping():
            return
        if not SMTP_AUTH_HELP_URL:
            self._set_status("授权码帮助链接暂不可用。", True)
            return
        try:
            opened = webbrowser.open(SMTP_AUTH_HELP_URL, new=2)
        except (webbrowser.Error, OSError):
            opened = False
        self._set_status("163 邮箱：设置 → POP3/SMTP/IMAP → 开启服务，获取授权码。" if opened else "无法打开 163 邮箱，请手动访问 mail.163.com。", not opened)

    def _add_target(self):
        if not self._can_live_action():
            return
        self._operation_error = None
        value = str(self.vars["target_value"].get()).strip()
        kind = "machine" if self.vars["target_kind"].get() == "机器" else "instance"
        minimum = str(self.vars["target_min"].get()).strip()
        if not value or any(not c.isprintable() for c in value):
            self._operation_error = "请填写主机 ID。" if kind == "machine" else "请填写完整实例名或 ID。"
            self._set_status(self._operation_error, True)
            return
        required = None
        if minimum:
            if len(minimum) > 4 or not minimum.isascii() or not minimum.isdecimal() or not 1 <= int(minimum) <= 1024:
                self._operation_error = "GPU 数量应为 1～1024 之间的整数。"
                self._set_status(self._operation_error, True)
                return
            required = int(minimum)
        elif kind == "machine":
            self._operation_error = "监控机器时，请填写需要的 GPU 数量。"
            self._set_status(self._operation_error, True)
            return
        if len(self._targets) >= 100:
            self._operation_error = "最多可监控 100 个目标。"
            self._set_status(self._operation_error, True)
            return
        if any(t["kind"] == kind and t["value"] == value for t in self._targets):
            self._set_status("这个目标已经在列表中。")
            return
        if not self._commit_targets([*self._targets, {"kind": kind, "value": value, "min_gpus": required}]):
            return
        self.vars["target_value"].set("")
        self.vars["target_min"].set("")
        self._set_status("目标已追加，监控继续运行。" if self.controller.running else "目标已添加，开始监控时自动保存。")
        if self._manual is not None:
            self._close_aux("manual")

    def _selected_targets(self):
        return [int(item) for item in self.targets_tree.selection() if item.isdecimal() and int(item) < len(self._targets)]

    def _remove_target(self):
        if self._closing or self.controller.busy or self._is_stopping():
            return
        selected = set(self._selected_targets())
        if not selected:
            return
        for i in selected:
            target = self._targets[i]
            self._observations.pop(target["kind"] + ":" + target["value"], None)
        self._targets = [t for i, t in enumerate(self._targets) if i not in selected]
        self._render_targets()
        self._set_status("已移除所选目标。")

    def _render_targets(self):
        self.targets_tree.delete(*self.targets_tree.get_children())
        for i, target in enumerate(self._targets):
            obs = self._observations.get(target["kind"] + ":" + target["value"])
            name = self._instance_names.get(target["value"], target["value"])
            if obs is None:
                status, gpu, tag = "待检查", "— / " + str(target.get("min_gpus") or "自动"), "muted"
            else:
                status = "可用" if obs.available is True else "暂不可用" if obs.available is False else "状态未知"
                gpu = f"{'—' if obs.idle_gpus is None else obs.idle_gpus} / {'—' if obs.required_gpus is None else obs.required_gpus}"
                tag = "available" if obs.available is True else "muted" if obs.available is None else ""
                if target["kind"] == "instance" and obs.label:
                    name = obs.label
            if name == target["value"] and re.fullmatch(r"[A-Fa-f0-9-]{24,}", name):
                name = name[:9] + "…" + name[-5:]
            if target["kind"] == "machine":
                name = "机器 " + name
            action = self._auto_start_result or {}
            if action.get("target_key") == target["kind"] + ":" + target["value"] or (action.get("instance_uuid") and action.get("instance_uuid") == target["value"]):
                action_state = action.get("state")
                labels = {"pending": "启动中", "accepted": "启动中", "unknown": "待核实", "succeeded": "已抢到", "rejected": "抢卡暂停", "blocked": "抢卡暂停"}
                if action_state in labels:
                    status = labels[action_state]
                    tag = "available" if action_state == "succeeded" else "warning" if action_state in ("unknown", "blocked", "rejected") else "muted"
            self.targets_tree.insert("", "end", iid=str(i), values=(_safe_label(name), gpu, status), tags=(tag,))
        if not self._targets:
            self.targets_tree.insert("", "end", iid="_empty", values=("尚未添加监控目标", "", ""), tags=("muted",))
        self._refresh_connection()
        self._refresh_controls()

    def _target_selection(self):
        self._refresh_controls()
        selected = self._selected_targets()
        if len(selected) == 1 and not self.controller.busy:
            target = self._targets[selected[0]]
            obs = self._observations.get(target["kind"] + ":" + target["value"])
            self._set_status(target["value"] + (" · " + obs.detail if obs else ""))

    def _credentials_changed(self, *args):
        self._connection_state = "unverified" if self.vars["token"].get() else "disconnected"
        self._refresh_connection()

    def _refresh_connection(self):
        if self._built:
            state = self._connection_state
            self.connection_var.set({"connecting": "处理中…", "opened": "待检测", "verified": "已连接", "error": "连接未完成", "unverified": "待验证"}.get(state, "未连接"))
            self.connection_label.configure(foreground=GREEN if state == "verified" else BLUE if state == "connecting" else MUTED)

    def _refresh_controls(self):
        if not self._built:
            return
        # Workers can finish before their queued lifecycle event reaches Tk.
        # Consume this round's opt-in before making Start clickable again.
        if self._monitor_was_running and not self.controller.running:
            self._monitor_was_running = False
            self.vars["auto_start"].set(False)
        self._editable = [(w, s) for w, s in self._editable if w.winfo_exists()]
        self._live_widgets.intersection_update(w for w, _ in self._editable)
        if not self.controller.busy and not self._is_stopping():
            self._stop_requested = False
        busy = self.controller.busy or self._closing or self._is_stopping()
        live_enabled = self._can_live_action()
        for widget, state in self._editable:
            enabled = live_enabled if widget in self._live_widgets else not busy
            widget.configure(state=state if enabled else "disabled")
        self.remove_button.configure(state="disabled" if busy or not self._selected_targets() else "normal")
        self._picker_widgets = [w for w in self._picker_widgets if w.winfo_exists()]
        for widget in self._picker_widgets:
            widget.configure(state="normal" if live_enabled else "disabled")
        if self._closing or self._is_stopping() or (self._stop_requested and self.controller.busy):
            self._primary_mode = "wait"
            self.start_button.configure(text="正在退出…" if self._closing else "正在停止…", state="disabled", style="TButton")
        elif self.controller.busy:
            self._primary_mode = "stop"
            text = "停止监控" if self.controller.running else {"connect_chrome": "取消连接", "open_chrome": "取消打开", "detect_chrome": "取消检测"}.get(self._active_action, "取消操作")
            self.start_button.configure(text=text, state="normal", style="TButton")
        else:
            self._stop_requested = False
            self._primary_mode = "start" if self._targets else "wait"
            self.start_button.configure(text="开始监控", state="normal" if self._targets else "disabled", style="Accent.TButton")

    def _show_instances(self, rows):
        if self._closing or self._stop_requested or self._is_stopping():
            return
        if self._hidden or self.root.state() in ("withdrawn", "iconic"):
            self._pending_instances = rows
            return
        self._connection_state = "verified"
        self._refresh_connection()
        for row in rows:
            identity, name = row.get("uuid"), row.get("name")
            if isinstance(identity, str) and isinstance(name, str) and name:
                self._instance_names[identity] = name
        if self._picker is not None and self._picker.winfo_exists():
            self._picker.destroy()
        picker = self._new_window("选择实例", 650, 350)
        self._picker, self._picker_widgets = picker, []
        picker.columnconfigure(0, weight=1)
        picker.rowconfigure(0, weight=1)
        frame = ttk.Frame(picker, padding=(12, 12, 12, 0))
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("name", "machine", "gpu", "status"), show="headings", selectmode="extended")
        self._picker_tree = tree
        for col, title, width in (("name", "实例名称", 245), ("machine", "机器", 85), ("gpu", "空闲 / 需要", 100), ("status", "状态", 90)):
            tree.heading(col, text=title, anchor="w")
            tree.column(col, width=round(width * self._scale), minwidth=60, stretch=col == "name")
        tree.grid(row=0, column=0, sticky="nsew")
        sy = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        sy.grid(row=0, column=1, sticky="ns")
        sx = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        sx.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        names = {"shutdown": "已关机", "running": "运行中", "starting": "启动中", "stopping": "关机中"}
        for i, row in enumerate(rows):
            status = row.get("status")
            status_text = names.get(status, "待确认") if isinstance(status, str) else "待确认"
            values = (row.get("name") or row.get("uuid", ""), row.get("machine_id", ""), f"{row.get('gpu_idle_num', '—')} / {row.get('req_gpu_amount', '—')}", status_text)
            tree.insert("", "end", iid=str(i), values=tuple(_safe_label(v) for v in values))
        bottom = ttk.Frame(picker, padding=12)
        bottom.grid(row=1, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        picker_status = tk.StringVar(picker, value="可按 Ctrl / Command 多选。" if rows else "这个账号暂时没有实例。")
        ttk.Label(bottom, textvariable=picker_status, style="Muted.TLabel", width=1).grid(row=0, column=0, sticky="ew", padx=(0, 10))
        def add_selected(event=None):
            if not self._can_live_action():
                return
            if not tree.selection():
                picker_status.set("请先选择实例。")
                return
            targets = [dict(target) for target in self._targets]
            for item in tree.selection():
                row = rows[int(item)]
                value = row.get("uuid") or row.get("name")
                if not isinstance(value, str) or not value or len(targets) >= 100:
                    continue
                if any(t["kind"] == "instance" and t["value"] == value for t in targets):
                    continue
                targets.append({"kind": "instance", "value": value, "min_gpus": None})
            added = len(targets) - len(self._targets)
            if added and not self._commit_targets(targets):
                picker_status.set("添加未完成，请查看主窗口提示。")
                return
            if not added and len(self._targets) >= 100:
                picker_status.set("最多可监控 100 个目标。")
                return
            self._set_status(f"已追加 {added} 个目标，监控继续运行。" if added and self.controller.running else f"已添加 {added} 个目标。" if added else "所选实例已经在列表中。")
            self._close_aux("picker")
        add = ttk.Button(bottom, text="添加", command=add_selected, style="Accent.TButton")
        add.grid(row=0, column=1, padx=(0, 8))
        self._picker_widgets.append(add)
        ttk.Button(bottom, text="关闭", command=lambda: self._close_aux("picker")).grid(row=0, column=2)
        picker.protocol("WM_DELETE_WINDOW", lambda: self._close_aux("picker"))
        tree.bind("<Double-1>", add_selected)
        self._refresh_controls()

    def _set_status(self, message, error=False):
        self._status_full, self._status_error = _safe_label(message), error
        self._render_status()

    def _render_status(self):
        text = self._status_full
        width = max(1, self.status_label.winfo_width() - 3)
        if self._small_font.measure(text) > width:
            while text and self._small_font.measure(text + "…") > width:
                text = text[:-1]
            text += "…"
        self.status_var.set(text)
        self.status_label.configure(foreground="#B04B47" if self._status_error else MUTED)

    def _append_log(self, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        for line in str(message).splitlines() or [""]:
            self._log_lines.append(f"{stamp}  {_safe_label(line)}")
        self._render_log()

    def _render_log(self):
        if self.log_text is not None and self.log_text.winfo_exists():
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", "\n".join(self._log_lines))
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def _emit(self, name, value):
        self._events.put((name, value))

    def _drain_events(self):
        self._after_id = None
        for _ in range(100):
            try:
                name, value = self._events.get_nowait()
            except queue.Empty:
                break
            if name in ("tray_show", "tray_exit"):
                if value == self._tray_generation:
                    if name == "tray_exit":
                        self._request_exit()
                    elif not self._closing:
                        self._restore_window()
            elif name == "tray_error":
                generation, message = value
                if generation == self._tray_generation:
                    self._tray_failed(message)
            elif name == "session_exit":
                self._request_exit()
            elif name in ("busy", "running", "action_busy", "stopping"):
                if name == "running":
                    if value:
                        self._monitor_was_running = True
                    elif self._monitor_was_running and not self.controller.running:
                        self._monitor_was_running = False
                        self.vars["auto_start"].set(False)
                self._refresh_controls()
                if name == "busy" and not value and self._connection_state == "connecting":
                    self._connection_state = "unverified" if self.vars["token"].get() else "disconnected"
                    self._refresh_connection()
            elif name == "connection" and isinstance(value, dict):
                state = value.get("state")
                if state in ("connecting", "opened", "verified", "error"):
                    self._connection_state = state
                    self._refresh_connection()
                    if not self._closing:
                        message = _safe_label(value.get("message", ""))
                        if state == "error":
                            self._operation_error = message
                        if state == "error" or self._operation_error is None:
                            self._set_status(message, state == "error")
            elif name == "chrome_connected" and isinstance(value, dict):
                # Secret-bearing event: never format, log, or display its payload.
                token = value.get("token")
                if isinstance(token, str) and token:
                    self.vars["token"].set(token)
                    self._connection_state = "verified"
                    self._refresh_connection()
            elif name == "status":
                if not self._closing and self._operation_error is None:
                    message = str(value)
                    if message == "操作完成":
                        if self._active_action == "open_chrome":
                            message = "Chrome 已打开，登录后点击“检测登录”。"
                        elif self._active_action in ("detect_chrome", "connect_chrome") and self._connection_state == "verified":
                            message = "登录检测成功，已连接 AutoDL。"
                    self._set_status(message)
            elif name == "log":
                self._append_log(str(value))
            elif name == "instances":
                self._show_instances(value)
            elif name == "observations":
                self._observations.update({obs.target_key: obs for obs in value})
                self._render_targets()
            elif name == "auto_start" and isinstance(value, dict):
                self._auto_start_result = value
                message = _safe_label(value.get("message", ""))
                state = value.get("state")
                hints = {"waiting": "等待满足开机条件 · 仅一台", "pending": "正在开机，等待确认", "accepted": "正在开机，等待确认",
                         "unknown": "结果待核实 · 已暂停抢卡", "succeeded": "已抢到一台 · 监控继续",
                         "blocked": "抢卡已暂停 · 查看活动记录", "rejected": "抢卡已暂停 · 查看活动记录"}
                if not self._closing and self._operation_error is None:
                    self._set_status(hints.get(state, message), state in ("unknown", "blocked", "rejected"))
                self._render_targets()
            elif name == "error":
                message = _friendly_error(value)
                self._operation_error = message
                if self._active_action in ("connect_chrome", "open_chrome", "detect_chrome"):
                    self._connection_state = "error"
                    self._refresh_connection()
                self._set_status(message, True)
                self._append_log(message)
            elif name == "mail_sent":
                if not self._closing and self._operation_error is None:
                    self._set_status(str(value))
                self._append_log(str(value))
            if self._destroyed:
                return
        if self._closing and not self.controller.busy:
            self._finish_exit()
            return
        if not self._closing and self._hidden and self.root.state() == "withdrawn":
            if self._tray is None or not self._tray.alive:
                self._tray_failed("系统托盘已断开，窗口已恢复。")
        self._after_id = self.root.after(100, self._drain_events)

    def _stop(self):
        self._stop_requested = True
        if self.controller.running or self._monitor_was_running:
            self.vars["auto_start"].set(False)
        self.controller.stop()
        self._set_status("正在停止，等待当前操作结束…")
        self._refresh_controls()

    def _on_close(self):
        if self._closing or self._hidden:
            return
        self._restore_state = self.root.state()
        tray_ready = self._ensure_tray()
        self._hidden_windows = []
        for name in ("more", "manual", "picker"):
            window = getattr(self, "_" + name)
            if window is not None and window.winfo_exists() and window.state() != "withdrawn":
                self._hidden_windows.append((window, window.state()))
                window.withdraw()
        self._hidden = True
        try:
            if tray_ready:
                self.root.withdraw()
                if not self._tray.alive:
                    self._tray_failed("系统托盘已断开，窗口已恢复。")
            else:
                # Keep the system taskbar/Dock as a recovery path whenever a
                # native tray is unavailable, including macOS and Linux.
                self.root.iconify()
        except tk.TclError:
            self._restore_window()

    def _request_exit(self):
        if self._closing or self._destroyed:
            return
        self._closing = True
        self._pending_instances = None
        self._hidden_windows = []
        self.controller.stop()
        for name in ("more", "manual", "picker"):
            self._close_aux(name)
        if self.controller.busy:
            self._set_status("正在退出，当前操作结束后窗口会关闭…")
            self._refresh_controls()
            return
        self._finish_exit()

    def _finish_exit(self):
        if self._destroyed:
            return
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self._stop_tray()
        self._destroyed = True
        self.root.destroy()


def main() -> int:
    _enable_dpi_awareness()
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
