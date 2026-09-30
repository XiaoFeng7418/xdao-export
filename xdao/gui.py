"""Tkinter 图形界面。

线程模型（沿用项目原有的约定，务必保持）：
``App`` 不是 Tk 组件，因此任何定时回调都必须写 ``self.root.after(...)``，
不能写 ``self.after(...)``，否则日志与进度不会刷新。
后台工作线程只往 ``queue.Queue`` 里投消息，主线程用 ``after`` 轮询消费。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .cache import CachedThreadFetcher, resolve_cache_dir
from .client import XdaoClient, XdaoError
from .exporters import EXPORTERS, ThreadData, create_exporter
from .exporters._shared import OutputDirNotWritable, ensure_writable
from .fetcher import parse_thread_id
from .settings import AppSettings
from .watcher import WatchTarget, check_once, describe_targets, watch_forever


ACCENT = "#3b82f6"
ACCENT_HOVER = "#2563eb"
ACCENT_DISABLED = "#9db8e8"
BG = "#f5f7fb"
CARD = "#ffffff"
TEXT = "#1f2430"
MUTED = "#6b7280"
BORDER = "#e2e8f0"
OK_GREEN = "#16a34a"

FONT_UI = "Microsoft YaHei UI"
SECTION_FONT = (FONT_UI, 11, "bold")
BODY_FONT = (FONT_UI, 10)
SMALL_FONT = (FONT_UI, 9)
MONO_FONT = ("Consolas", 9)


def setup_style(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=BG, foreground=TEXT, font=BODY_FONT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Card.TFrame", background=CARD, relief="flat")
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("CardSection.TLabel", background=CARD, foreground=TEXT, font=SECTION_FONT)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=SMALL_FONT)
    style.configure("Section.TLabel", background=BG, foreground=TEXT, font=SECTION_FONT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=SMALL_FONT)
    style.configure(
        "TButton",
        background=ACCENT,
        foreground="#ffffff",
        borderwidth=0,
        focusthickness=0,
        padding=(16, 9),
        relief="flat",
        font=(FONT_UI, 10, "bold"),
    )
    style.map(
        "TButton",
        background=[("active", ACCENT_HOVER), ("disabled", ACCENT_DISABLED)],
    )
    style.configure(
        "Secondary.TButton",
        background="#eef2f7",
        foreground=TEXT,
        borderwidth=0,
        focusthickness=0,
        padding=(11, 7),
        relief="flat",
        font=(FONT_UI, 9),
    )
    style.map("Secondary.TButton", background=[("active", "#e2e8f0")])
    style.configure(
        "TEntry",
        fieldbackground=CARD,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        padding=6,
    )
    style.configure("TCombobox", padding=4)
    style.configure("TCheckbutton", background=BG)
    style.configure("Card.TCheckbutton", background=CARD)
    style.configure("TRadiobutton", background=BG)
    style.configure(
        "Horizontal.TProgressbar",
        background=ACCENT,
        troughcolor="#eef1f6",
        bordercolor=BORDER,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        thickness=6,
    )
    return style


class ModernProgress(tk.Canvas):
    """圆角扁平进度条，支持确定/不确定两种模式。"""

    def __init__(self, master, height: int = 8, **kwargs) -> None:
        super().__init__(
            master,
            height=height,
            bg=BG,
            highlightthickness=0,
            borderwidth=0,
            **kwargs,
        )
        self._value = 0
        self._maximum = 100
        self._mode = "determinate"
        self._offset = 0.0
        self._animating = False
        self.bind("<Configure>", lambda _e: self._redraw())

    def _round_rect(self, x1, y1, x2, y2, r=4, **kw) -> None:
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        self.create_polygon(points, smooth=True, **kw)

    def _redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 2 or h <= 2:
            return
        r = h / 2
        self._round_rect(0, 0, w, h, r=r, fill="#eef1f6", outline="")
        if self._mode == "indeterminate":
            seg_w = max(30.0, w * 0.25)
            span = w + seg_w
            x = (self._offset * span) % span - seg_w
            self._round_rect(x, 0, x + seg_w, h, r=r, fill=ACCENT, outline="")
        else:
            ratio = min(1.0, self._value / self._maximum) if self._maximum else 0.0
            fill_w = w * ratio
            if fill_w >= h:
                self._round_rect(0, 0, fill_w, h, r=r, fill=ACCENT, outline="")

    def start(self, interval: int = 12) -> None:
        self._mode = "indeterminate"
        self._animating = True
        self._animate()

    def stop(self) -> None:
        self._animating = False
        self._mode = "determinate"
        self._redraw()

    def set_value(self, value: float, maximum: float = 100) -> None:
        self._mode = "determinate"
        self._value = value
        self._maximum = maximum
        self._redraw()

    def _animate(self) -> None:
        if not self._animating:
            return
        self._offset += 0.025
        self._redraw()
        self.after(30, self._animate)


class LoginDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, client: XdaoClient) -> None:
        super().__init__(master)
        self.configure(bg=BG)
        self.client = client
        self.userhash: str | None = None
        self._result_queue: queue.Queue = queue.Queue()
        self._busy = False
        self.title("登录 X 岛")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.form = None

        pad = {"padx": 12, "pady": 6}
        ttk.Label(self, text="账号（邮箱）").grid(row=0, column=0, sticky="w", **pad)
        self.email_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.email_var, width=34).grid(row=0, column=1, **pad)

        ttk.Label(self, text="密码").grid(row=1, column=0, sticky="w", **pad)
        self.password_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.password_var, width=34, show="*").grid(
            row=1, column=1, **pad
        )

        ttk.Label(self, text="验证码").grid(row=2, column=0, sticky="w", **pad)
        verify_row = ttk.Frame(self)
        verify_row.grid(row=2, column=1, sticky="w", **pad)
        self.verify_var = tk.StringVar()
        ttk.Entry(verify_row, textvariable=self.verify_var, width=14).pack(side="left")
        self._captcha_image = None
        self.captcha_button = ttk.Button(
            verify_row, text="加载中…", command=self._refresh_captcha
        )
        self.captcha_button.pack(side="left", padx=(8, 0))

        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="记住登录状态（本机保存 userhash）", variable=self.remember_var).grid(
            row=3, column=0, columnspan=2, sticky="w", **pad
        )

        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(
            buttons, text="取消", style="Secondary.TButton", command=self.destroy
        ).pack(side="left", padx=6)
        self.login_button = ttk.Button(buttons, text="登录", command=self._do_login)
        self.login_button.pack(side="left", padx=6)
        ttk.Button(
            buttons,
            text="改用饼干直接登录",
            style="Secondary.TButton",
            command=self._manual_userhash,
        ).pack(side="left", padx=6)
        self.after(50, self._load_form)

    def _refresh_captcha(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.captcha_button.config(state="disabled", text="加载中…")

        def work() -> None:
            try:
                form = self.client.fetch_login_form()
                self._result_queue.put(("form", form))
            except Exception as exc:
                self._result_queue.put(("form_error", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll_form)

    def _load_form(self) -> None:
        self._refresh_captcha()

    def _poll_form(self) -> None:
        try:
            kind, payload = self._result_queue.get_nowait()
        except queue.Empty:
            if self._busy:
                self.after(50, self._poll_form)
            return
        self._busy = False
        if kind == "form_error":
            self.captcha_button.config(state="normal", text="刷新")
            messagebox.showerror("加载验证码失败", payload, parent=self)
            return
        self.form = payload
        try:
            self._captcha_image = tk.PhotoImage(data=self.form.captcha_bytes)
            self.captcha_button.config(state="normal", image=self._captcha_image)
        except Exception:
            self.captcha_button.config(state="normal", text="刷新")
        self.verify_var.set("")

    def _do_login(self) -> None:
        email = self.email_var.get().strip()
        password = self.password_var.get()
        verify = self.verify_var.get().strip()
        if not email or not password or not verify:
            messagebox.showwarning("提示", "请填写账号、密码和验证码。", parent=self)
            return
        if self.form is None:
            messagebox.showwarning("提示", "验证码还没加载完成，请稍候。", parent=self)
            return
        self._busy = True
        self.login_button.config(state="disabled", text="登录中…")
        self.update_idletasks()

        def work() -> None:
            try:
                userhash = self.client.login(
                    email, password, verify, self.form.hash_value
                )
                self._result_queue.put(("ok", userhash))
            except Exception as exc:
                self._result_queue.put(("fail", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll_login)

    def _poll_login(self) -> None:
        try:
            kind, payload = self._result_queue.get_nowait()
        except queue.Empty:
            if self._busy:
                self.after(50, self._poll_login)
            return
        self._busy = False
        self.login_button.config(state="normal", text="登录")
        if kind == "ok":
            self.userhash = payload
            self.destroy()
            return
        messagebox.showerror("登录失败", payload, parent=self)
        self._refresh_captcha()

    def _manual_userhash(self) -> None:
        """账号密码登录失败的兜底：直接粘贴 userhash 饼干。"""
        value = simpledialog.askstring(
            "填入饼干",
            "请从浏览器开发者工具复制 userhash 的值（Cookie 中 userhash= 到 ; 之间的内容），粘贴到下面：",
            parent=self,
        )
        if value and value.strip():
            try:
                self.client.set_userhash(value.strip())
                self.userhash = value.strip()
                self.destroy()
            except Exception as exc:
                messagebox.showerror("设置失败", str(exc), parent=self)


class SettingsDialog(tk.Toplevel):
    """网络、缓存与导出设置。"""

    def __init__(self, master: tk.Tk, settings: AppSettings) -> None:
        super().__init__(master)
        self.configure(bg=BG)
        self.settings = settings
        self.saved = False
        self.title("设置")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        pad = {"padx": 10, "pady": 5}
        frame = ttk.Frame(self, padding=(16, 14))
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="网络", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )

        ttk.Label(frame, text="代理地址").grid(row=1, column=0, sticky="w", **pad)
        self.proxy_var = tk.StringVar(value=settings.proxy)
        ttk.Entry(frame, textvariable=self.proxy_var, width=34).grid(row=1, column=1, **pad)
        ttk.Label(
            frame,
            text="例如 http://127.0.0.1:7890；留空则读取系统环境变量，仍为空表示直连。",
            style="Muted.TLabel",
            wraplength=340,
        ).grid(row=2, column=1, sticky="w", padx=10)

        ttk.Label(frame, text="请求超时（秒）").grid(row=3, column=0, sticky="w", **pad)
        self.timeout_var = tk.StringVar(value=f"{settings.timeout:g}")
        ttk.Entry(frame, textvariable=self.timeout_var, width=10).grid(
            row=3, column=1, sticky="w", **pad
        )

        ttk.Label(frame, text="失败重试次数").grid(row=4, column=0, sticky="w", **pad)
        self.retries_var = tk.StringVar(value=str(settings.retries))
        ttk.Entry(frame, textvariable=self.retries_var, width=10).grid(
            row=4, column=1, sticky="w", **pad
        )

        ttk.Label(frame, text="请求间隔（秒）").grid(row=5, column=0, sticky="w", **pad)
        self.throttle_var = tk.StringVar(value=f"{settings.throttle:g}")
        ttk.Entry(frame, textvariable=self.throttle_var, width=10).grid(
            row=5, column=1, sticky="w", **pad
        )
        ttk.Label(
            frame,
            text="请求过密会被限流（429）；抓很长的串时把间隔调到 0.3～0.5 更稳。",
            style="Muted.TLabel",
            wraplength=340,
        ).grid(row=6, column=1, sticky="w", padx=10)

        ttk.Separator(frame, orient="horizontal").grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=10
        )
        ttk.Label(frame, text="缓存", style="Section.TLabel").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )

        self.use_cache_var = tk.BooleanVar(value=settings.use_cache)
        ttk.Checkbutton(
            frame,
            text="启用本地缓存（断点续传、跳过重复下载）",
            variable=self.use_cache_var,
        ).grid(row=9, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(frame, text="缓存目录").grid(row=10, column=0, sticky="w", **pad)
        cache_row = ttk.Frame(frame)
        cache_row.grid(row=10, column=1, sticky="ew", **pad)
        self.cache_var = tk.StringVar(value=settings.cache_dir)
        ttk.Entry(cache_row, textvariable=self.cache_var, width=26).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            cache_row, text="…", style="Secondary.TButton", width=3, command=self._choose_cache
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            frame,
            text="留空表示放在导出目录下的 .cache，随导出目录一起迁移。",
            style="Muted.TLabel",
            wraplength=340,
        ).grid(row=11, column=1, sticky="w", padx=10)

        ttk.Separator(frame, orient="horizontal").grid(
            row=12, column=0, columnspan=2, sticky="ew", pady=10
        )
        ttk.Label(frame, text="导出", style="Section.TLabel").grid(
            row=13, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        ttk.Label(frame, text="文件名模板").grid(row=14, column=0, sticky="w", **pad)
        self.template_var = tk.StringVar(value=settings.filename_template)
        ttk.Entry(frame, textvariable=self.template_var, width=34).grid(row=14, column=1, **pad)
        ttk.Label(
            frame,
            text=(
                "占位符：{title} 标题、{id} 串号、{date} 导出日期、{po} PO 饼干、{count} 楼层数。\n"
                "例：[{id}] {title}。留空表示用标题，无标题时取第一句话。"
            ),
            style="Muted.TLabel",
            wraplength=340,
            justify="left",
        ).grid(row=15, column=1, sticky="w", padx=10)

        ttk.Label(frame, text="PDF 浏览器").grid(row=16, column=0, sticky="w", **pad)
        browser_row = ttk.Frame(frame)
        browser_row.grid(row=16, column=1, sticky="ew", **pad)
        self.browser_var = tk.StringVar(value=settings.pdf_browser)
        ttk.Entry(browser_row, textvariable=self.browser_var, width=26).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            browser_row, text="…", style="Secondary.TButton", width=3, command=self._choose_browser
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            frame,
            text="导出 PDF 时调用的浏览器（无头模式渲染）。留空表示自动查找 Chrome 或 Edge。",
            style="Muted.TLabel",
            wraplength=340,
        ).grid(row=17, column=1, sticky="w", padx=10)

        buttons = ttk.Frame(self, padding=(16, 0, 16, 14))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", style="Secondary.TButton", command=self.destroy).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(buttons, text="保存", command=self._save).pack(side="right")

    def _choose_cache(self) -> None:
        chosen = filedialog.askdirectory(title="选择缓存目录", parent=self)
        if chosen:
            self.cache_var.set(chosen)

    def _choose_browser(self) -> None:
        chosen = filedialog.askopenfilename(
            title="选择浏览器可执行文件",
            parent=self,
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if chosen:
            self.browser_var.set(chosen)

    def _save(self) -> None:
        from .settings import DEFAULT_RETRIES, DEFAULT_THROTTLE, DEFAULT_TIMEOUT

        settings = self.settings
        settings.proxy = self.proxy_var.get().strip()
        settings.use_cache = bool(self.use_cache_var.get())
        settings.cache_dir = self.cache_var.get().strip()
        settings.filename_template = self.template_var.get().strip()
        settings.pdf_browser = self.browser_var.get().strip()

        def _num(raw: str, fallback: float, minimum: float) -> float:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return fallback
            return value if value >= minimum else fallback

        settings.timeout = _num(self.timeout_var.get(), DEFAULT_TIMEOUT, 1.0)
        settings.throttle = _num(self.throttle_var.get(), DEFAULT_THROTTLE, 0.0)
        try:
            settings.retries = max(0, min(10, int(float(self.retries_var.get()))))
        except (TypeError, ValueError):
            settings.retries = DEFAULT_RETRIES

        settings.save()
        self.saved = True
        self.destroy()


class CookiePicker(tk.Toplevel):
    """从当前串第一页里挑选要筛选的饼干。"""

    def __init__(
        self,
        master: tk.Tk,
        counter: dict[str, int],
        po_hash: str,
        target_var: tk.StringVar,
    ) -> None:
        super().__init__(master)
        self.configure(bg=BG)
        self.target_var = target_var
        self.title("挑选饼干")
        self.geometry("440x440")
        self.transient(master)
        self.grab_set()

        outer = ttk.Frame(self, padding=(16, 14))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="第一页出现的饼干", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="勾选后点「应用」，多个饼干会以空格分隔写入筛选框。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(
            list_frame, bg=CARD, highlightthickness=1, highlightbackground=BORDER
        )
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=CARD)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))

        existing = set(target_var.get().replace(",", " ").split())
        self.vars: dict[str, tk.BooleanVar] = {}
        for cookie, count in sorted(counter.items(), key=lambda kv: -kv[1]):
            label = f"{cookie}（{count} 楼）"
            if cookie == po_hash:
                label += "  ← PO"
            var = tk.BooleanVar(value=cookie in existing)
            tk.Checkbutton(
                inner, text=label, variable=var, bg=CARD, anchor="w", font=SMALL_FONT
            ).pack(anchor="w", padx=8, pady=2, fill="x")
            self.vars[cookie] = var

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="取消", style="Secondary.TButton", command=self.destroy).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(buttons, text="应用", command=self._apply).pack(side="right")
        ttk.Button(
            buttons, text="全选", style="Secondary.TButton", command=lambda: self._set_all(True)
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="全不选",
            style="Secondary.TButton",
            command=lambda: self._set_all(False),
        ).pack(side="left", padx=(6, 0))

    def _set_all(self, value: bool) -> None:
        for var in self.vars.values():
            var.set(value)

    def _apply(self) -> None:
        chosen = [cookie for cookie, var in self.vars.items() if var.get()]
        self.target_var.set(" ".join(chosen))
        self.destroy()


class WatchDialog(tk.Toplevel):
    """监控串更新：列表管理 + 开关。"""

    def __init__(self, app: "App") -> None:
        super().__init__(app.root)
        self.configure(bg=BG)
        self.app = app
        self.title("监控串更新")
        self.geometry("760x480")
        self.minsize(660, 420)
        self.transient(app.root)

        outer = ttk.Frame(self, padding=(16, 14))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="监控列表", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="定时检查这些串：有新回复就自动导出；没有新回复只花一次请求。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        columns = ("thread", "scope", "format", "state", "checked")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=9)
        for key, text, width in (
            ("thread", "串号", 110),
            ("scope", "范围", 90),
            ("format", "格式", 70),
            ("state", "状态", 260),
            ("checked", "最近检查", 100),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(
            controls,
            text="添加输入框中的串",
            style="Secondary.TButton",
            command=self._add_from_input,
        ).pack(side="left")
        ttk.Button(
            controls, text="移除选中", style="Secondary.TButton", command=self._remove_selected
        ).pack(side="left", padx=(6, 0))
        ttk.Label(controls, text="检查间隔（秒）", style="Muted.TLabel").pack(
            side="left", padx=(14, 4)
        )
        self.interval_var = tk.StringVar(value=str(int(self.app.settings.watch_interval)))
        ttk.Entry(controls, textvariable=self.interval_var, width=7).pack(side="left")
        self.verify_var = tk.BooleanVar(value=self.app.settings.verify_cached)
        ttk.Checkbutton(
            controls,
            text="校验老楼层改动（更准，每轮多一次请求）",
            variable=self.verify_var,
        ).pack(side="left", padx=(10, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self.toggle_button = ttk.Button(actions, text="开始监控", command=self._toggle)
        self.toggle_button.pack(side="left")
        self.status_var = tk.StringVar()
        ttk.Label(actions, textvariable=self.status_var, style="Muted.TLabel").pack(
            side="left", padx=(12, 0)
        )
        ttk.Button(
            actions, text="立即检查一次", style="Secondary.TButton", command=self._check_now
        ).pack(side="right")
        ttk.Button(actions, text="关闭", style="Secondary.TButton", command=self.destroy).pack(
            side="right", padx=(0, 6)
        )

        self._refresh()
        self._sync_toggle()
        self._poll()

    # ---------- 列表维护 ----------

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for target in self.app.watch_targets:
            _, ext, _ = EXPORTERS.get(target.format_key, EXPORTERS["html"])
            if target.include_hashes:
                scope = "指定饼干"
            elif target.scope == "po":
                scope = "仅 PO"
            else:
                scope = "全部"
            checked = (
                time.strftime("%H:%M:%S", time.localtime(target.last_check))
                if target.last_check
                else "—"
            )
            self.tree.insert(
                "",
                "end",
                iid=str(id(target)),
                values=(f"No.{target.label}", scope, ext.lstrip("."), target.state, checked),
            )

    def _add_from_input(self) -> None:
        urls = self.app._collect_urls()
        if not urls:
            messagebox.showwarning("提示", "请先在主窗口的输入框里填写串网址。", parent=self)
            return
        added = 0
        for url in urls:
            if parse_thread_id(url) is None:
                continue
            if any(t.url_or_id == url for t in self.app.watch_targets):
                continue
            self.app.watch_targets.append(
                WatchTarget(
                    url_or_id=url,
                    scope=self.app.scope_var.get(),
                    format_key=self.app.current_format(),
                    include_hashes=self.app.parse_hashes(),
                    image_mode=self.app.current_image_mode(),
                )
            )
            added += 1
        self.app.persist_watch_targets()
        self._refresh()
        if not added:
            messagebox.showinfo("提示", "没有新增（可能已存在，或网址无法识别）。", parent=self)

    def _remove_selected(self) -> None:
        selected = {str(iid) for iid in self.tree.selection()}
        if selected:
            self.app.watch_targets = [
                t for t in self.app.watch_targets if str(id(t)) not in selected
            ]
            self.app.persist_watch_targets()
            self._refresh()
        if not self.app.watch_targets and self.app._watching:
            self._stop()

    # ---------- 监控开关 ----------

    def _toggle(self) -> None:
        self._stop() if self.app._watching else self._start()

    def _start(self) -> None:
        if not self.app.watch_targets:
            messagebox.showwarning("提示", "请先添加要监控的串。", parent=self)
            return
        try:
            interval = max(15.0, float(self.interval_var.get()))
        except (TypeError, ValueError):
            interval = 300.0
        self.app.settings.watch_interval = interval
        self.app.settings.verify_cached = bool(self.verify_var.get())
        self.app.persist_watch_targets()
        self.app.start_watching(interval, bool(self.verify_var.get()))
        self._sync_toggle()

    def _stop(self) -> None:
        self.app.stop_watching()
        self._sync_toggle()

    def _check_now(self) -> None:
        if not self.app.watch_targets:
            messagebox.showwarning("提示", "请先添加要监控的串。", parent=self)
            return
        self.app.check_watch_once()
        self.status_var.set("已触发一次检查，结果见主窗口日志。")

    def _sync_toggle(self) -> None:
        if not self.winfo_exists():
            return
        self.toggle_button.config(text="停止监控" if self.app._watching else "开始监控")
        state = "监控进行中" if self.app._watching else "监控未启动"
        self.status_var.set(state)
        self.title(f"监控串更新 · {state}")

    def _poll(self) -> None:
        # 不消费共享队列：主窗口负责把结果写进日志，这里只刷新状态列。
        if not self.winfo_exists():
            return
        if self.app._watching:
            self.status_var.set(f"监控进行中（{len(self.app.watch_targets)} 个串）")
        self._refresh()
        self.after(1500, self._poll)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.style = setup_style(root)
        root.configure(bg=BG)
        self.settings = AppSettings.load()
        self.client = XdaoClient(
            timeout=self.settings.timeout,
            retries=self.settings.retries,
            proxy=self.settings.proxy or None,
            throttle=self.settings.throttle,
        )
        if self.settings.userhash:
            self.client.set_userhash(self.settings.userhash)

        self._export_queue: queue.Queue = queue.Queue()
        self._exporting = False
        self._last_failed: list[str] = []
        self._cookie_queue: queue.Queue = queue.Queue()

        # 监控状态
        self.watch_targets: list[WatchTarget] = [
            WatchTarget.from_dict(d)
            for d in self.settings.watch_targets
            if d.get("url_or_id")
        ]
        self.watch_queue: queue.Queue = queue.Queue()
        self._watching = False
        self._watch_stop: threading.Event | None = None
        self._watch_dialog: WatchDialog | None = None

        root.title("X岛串导出")
        root.geometry("840x840")
        root.minsize(740, 720)

        outer = ttk.Frame(root, padding=(18, 16))
        outer.pack(fill="both", expand=True)

        header = tk.Canvas(outer, height=72, bg=BG, highlightthickness=0)
        header.pack(fill="x")
        header.bind("<Configure>", lambda e: self._draw_header(header))

        # 状态行
        status_row = ttk.Frame(outer)
        status_row.pack(fill="x", pady=(10, 10))
        self.status_var = tk.StringVar(value="已登录" if self.settings.userhash else "未登录")
        ttk.Label(
            status_row,
            textvariable=self.status_var,
            foreground=OK_GREEN if self.settings.userhash else MUTED,
            font=(FONT_UI, 10, "bold"),
        ).pack(side="left")
        self.watch_status_var = tk.StringVar(value="")
        ttk.Label(status_row, textvariable=self.watch_status_var, style="Muted.TLabel").pack(
            side="left", padx=(12, 0)
        )
        ttk.Button(
            status_row, text="监控串更新", style="Secondary.TButton", command=self.open_watch
        ).pack(side="right", padx=(6, 0))
        ttk.Button(
            status_row, text="设置", style="Secondary.TButton", command=self.open_settings
        ).pack(side="right", padx=(6, 0))
        ttk.Button(
            status_row,
            text="登录 / 设置饼干",
            style="Secondary.TButton",
            command=self.open_login,
        ).pack(side="right")

        # 输入卡片
        url_shadow = tk.Frame(outer, bg="#e3e8f0")
        url_shadow.pack(fill="both", expand=True, pady=(0, 12))
        url_card = tk.Frame(url_shadow, bg=CARD, padx=14, pady=12)
        url_card.pack(fill="both", expand=True, padx=1, pady=1)
        ttk.Label(url_card, text="① 输入串网址", style="CardSection.TLabel").pack(anchor="w")
        ttk.Label(url_card, text="每行一个，可一次粘贴多个", style="CardMuted.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        self.urls_text = tk.Text(
            url_card,
            height=4,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=BODY_FONT,
            padx=10,
            pady=8,
            bg="#fbfcfe",
        )
        self.urls_text.pack(fill="both", expand=True)
        self._add_context_menu(self.urls_text)

        scope_frame = ttk.Frame(url_card, style="Card.TFrame")
        scope_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(scope_frame, text="抓取范围", style="Card.TLabel", font=SECTION_FONT).pack(side="left")
        self.scope_var = tk.StringVar(value=self.settings.scope or "all")
        ttk.Radiobutton(
            scope_frame, text="所有人发言", variable=self.scope_var, value="all"
        ).pack(side="left", padx=(12, 14))
        ttk.Radiobutton(
            scope_frame, text="只抓 PO 发言", variable=self.scope_var, value="po"
        ).pack(side="left")

        cookie_frame = ttk.Frame(url_card, style="Card.TFrame")
        cookie_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(cookie_frame, text="只看指定饼干", style="Card.TLabel", font=SECTION_FONT).pack(
            side="left"
        )
        self.hashes_var = tk.StringVar(value=self.settings.include_hashes)
        ttk.Entry(cookie_frame, textvariable=self.hashes_var).pack(
            side="left", fill="x", expand=True, padx=(12, 8)
        )
        self.pick_cookie_button = ttk.Button(
            cookie_frame,
            text="从串中挑选…",
            style="Secondary.TButton",
            command=self.pick_cookies,
        )
        self.pick_cookie_button.pack(side="left")
        ttk.Label(
            url_card,
            text="留空表示不筛选；多个饼干用空格或逗号分隔。填写后以该筛选为准，「只抓 PO」不再生效。",
            style="CardMuted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        format_frame = ttk.Frame(url_card, style="Card.TFrame")
        format_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(format_frame, text="导出格式", style="Card.TLabel", font=SECTION_FONT).pack(side="left")
        self._format_keys = list(EXPORTERS.keys())
        self.format_box = ttk.Combobox(
            format_frame,
            state="readonly",
            width=22,
            values=[name for name, _, _ in EXPORTERS.values()],
        )
        self.format_box.pack(side="left", padx=(12, 0))
        self.format_box.current(
            self._format_keys.index(self.settings.format_key)
            if self.settings.format_key in self._format_keys
            else 0
        )
        self.use_cache_var = tk.BooleanVar(value=self.settings.use_cache)
        ttk.Checkbutton(
            format_frame, text="使用本地缓存", variable=self.use_cache_var
        ).pack(side="left", padx=(14, 0))

        # EPUB 的图片处理方式（只在选 EPUB 时可用）
        self.image_mode_frame = ttk.Frame(url_card, style="Card.TFrame")
        self.image_mode_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(
            self.image_mode_frame, text="EPUB 图片", style="Card.TLabel", font=SECTION_FONT
        ).pack(side="left")
        self._image_mode_keys = ["embed", "url", "drop"]
        self.image_mode_box = ttk.Combobox(
            self.image_mode_frame,
            state="readonly",
            width=28,
            values=["内嵌到文件（体积大，离线可看）", "仅保留图片链接（体积小）", "丢弃图片"],
        )
        self.image_mode_box.pack(side="left", padx=(12, 0))
        self.image_mode_box.current(
            self._image_mode_keys.index(self.settings.image_mode)
            if self.settings.image_mode in self._image_mode_keys
            else 0
        )
        self.format_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_image_mode())
        self._sync_image_mode()

        # 导出目录卡片
        folder_shadow = tk.Frame(outer, bg="#e3e8f0")
        folder_shadow.pack(fill="x", pady=(0, 14))
        folder_card = tk.Frame(folder_shadow, bg=CARD, padx=14, pady=12)
        folder_card.pack(fill="x", padx=1, pady=1)
        ttk.Label(folder_card, text="② 导出目录", style="CardSection.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        folder_inner = ttk.Frame(folder_card, style="Card.TFrame")
        folder_inner.pack(fill="x")
        self.output_var = tk.StringVar(
            value=self.settings.output_dir or str(Path.home() / "Documents" / "X岛备份")
        )
        ttk.Entry(folder_inner, textvariable=self.output_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            folder_inner, text="更改…", style="Secondary.TButton", command=self.choose_folder
        ).pack(side="left", padx=(8, 0))

        cache_row = ttk.Frame(folder_card, style="Card.TFrame")
        cache_row.pack(fill="x", pady=(8, 0))
        self.cache_info_var = tk.StringVar(value="")
        ttk.Label(cache_row, textvariable=self.cache_info_var, style="CardMuted.TLabel").pack(
            side="left"
        )
        ttk.Button(
            cache_row, text="清空缓存", style="Secondary.TButton", command=self.clear_cache
        ).pack(side="right")

        # 主操作按钮
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="开始导出", command=self.start, width=16)
        self.start_button.pack(side="left")
        self.retry_button = ttk.Button(
            buttons,
            text="重试失败项",
            style="Secondary.TButton",
            command=self.retry_failed,
            state="disabled",
        )
        self.retry_button.pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="打开导出目录", style="Secondary.TButton", command=self.open_folder
        ).pack(side="left", padx=(8, 0))

        # 进度
        self.progress_var = tk.StringVar(value="等待开始")
        progress_head = ttk.Frame(outer)
        progress_head.pack(fill="x", pady=(14, 6))
        ttk.Label(progress_head, textvariable=self.progress_var, style="Muted.TLabel").pack(side="left")
        self.progress_bar = ModernProgress(outer, height=8)
        self.progress_bar.pack(fill="x")

        # 日志
        log_head = ttk.Frame(outer)
        log_head.pack(fill="x", pady=(14, 4))
        ttk.Label(log_head, text="运行日志", style="Section.TLabel").pack(side="left")
        ttk.Button(log_head, text="清空", style="Secondary.TButton", command=self.clear_log).pack(
            side="right"
        )
        ttk.Button(
            log_head, text="保存日志…", style="Secondary.TButton", command=self.save_log
        ).pack(side="right", padx=(0, 6))
        self.log_text = tk.Text(
            outer,
            height=8,
            state="disabled",
            background="#fbfcfe",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=MONO_FONT,
            padx=10,
            pady=8,
        )
        self.log_text.pack(fill="both", expand=True)
        self._add_context_menu(self.log_text)

        self.refresh_watch_status()
        self.refresh_cache_info()
        self.log("就绪。填入串网址后点「开始导出」。")

    # ---------- 界面辅助 ----------

    def _draw_header(self, canvas: tk.Canvas) -> None:
        canvas.delete("all")
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w <= 1 or h <= 1:
            return
        top = (238, 244, 255)
        bottom = (245, 247, 251)
        for i in range(h):
            t = i / max(1, h - 1)
            r = int(top[0] + (bottom[0] - top[0]) * t)
            g = int(top[1] + (bottom[1] - top[1]) * t)
            b = int(top[2] + (bottom[2] - top[2]) * t)
            canvas.create_line(0, i, w, i, fill=f"#{r:02x}{g:02x}{b:02x}")
        canvas.create_text(
            18,
            26,
            anchor="w",
            text="X岛串导出",
            fill="#1f2430",
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        canvas.create_text(
            18,
            50,
            anchor="w",
            text="完整备份任意一个串：HTML / TXT / Markdown / EPUB，支持断点续传与更新监控",
            fill="#6b7280",
            font=("Microsoft YaHei UI", 9),
        )

    def _add_context_menu(self, widget: tk.Text) -> None:
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="剪切", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="复制", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="粘贴", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: widget.event_generate("<<SelectAll>>"))

        def show(event: tk.Event) -> None:
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", show)

    # ---------- 配置读写 ----------

    def current_format(self) -> str:
        index = self.format_box.current()
        if 0 <= index < len(self._format_keys):
            return self._format_keys[index]
        return "html"

    def current_image_mode(self) -> str:
        index = self.image_mode_box.current()
        if 0 <= index < len(self._image_mode_keys):
            return self._image_mode_keys[index]
        return "embed"

    def _sync_image_mode(self) -> None:
        """图片模式只对 EPUB 有意义，其它格式下灰掉以免误导。"""
        enabled = self.current_format() == "epub"
        self.image_mode_box.configure(state="readonly" if enabled else "disabled")

    def parse_hashes(self) -> list[str]:
        return self.settings.parse_hashes(self.hashes_var.get())

    def persist_prefs(self) -> None:
        self.settings.output_dir = self.output_var.get().strip() or None
        self.settings.scope = self.scope_var.get()
        self.settings.format_key = self.current_format()
        self.settings.include_hashes = self.hashes_var.get().strip()
        self.settings.use_cache = bool(self.use_cache_var.get())
        self.settings.image_mode = self.current_image_mode()
        self.settings.save()

    def persist_watch_targets(self) -> None:
        self.settings.watch_targets = [t.to_dict() for t in self.watch_targets]
        self.settings.save()

    def apply_settings_to_client(self) -> None:
        self.client.apply_config(self.settings)

    # ---------- 交互 ----------

    def open_login(self) -> None:
        try:
            dialog = LoginDialog(self.root, self.client)
            self.root.wait_window(dialog)
            if dialog.userhash:
                self.settings.userhash = dialog.userhash
                self.settings.save()
                self.status_var.set("已登录")
                self.log("登录成功，饼干已保存。")
        except XdaoError as exc:
            messagebox.showerror("无法打开登录页", str(exc))
        except Exception as exc:
            messagebox.showerror("登录出错", str(exc))

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.root, self.settings)
        self.root.wait_window(dialog)
        if dialog.saved:
            self.apply_settings_to_client()
            self.refresh_cache_info()
            self.log(
                f"设置已保存：超时 {self.settings.timeout:g} 秒、重试 {self.settings.retries} 次、"
                f"间隔 {self.settings.throttle:g} 秒、代理 "
                f"{'已设置' if self.settings.proxy else '直连'}、缓存 "
                f"{'启用' if self.settings.use_cache else '关闭'}。"
            )

    def open_watch(self) -> None:
        if self._watch_dialog is not None and self._watch_dialog.winfo_exists():
            self._watch_dialog.lift()
            self._watch_dialog.focus_set()
            return
        self._watch_dialog = WatchDialog(self)

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择导出目录", initialdir=self.output_var.get() or None
        )
        if chosen:
            self.output_var.set(chosen)
            self.refresh_cache_info()

    def open_folder(self) -> None:
        folder = self.output_var.get().strip()
        if not folder:
            messagebox.showwarning("提示", "请先选择导出目录。")
            return
        Path(folder).mkdir(parents=True, exist_ok=True)
        try:
            import os

            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo("目录", folder)

    def clear_log(self) -> None:
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def save_log(self) -> None:
        content = self.log_text.get("1.0", "end").strip()
        if not content:
            messagebox.showinfo("提示", "日志是空的。")
            return
        path = filedialog.asksaveasfilename(
            title="保存日志",
            defaultextension=".log",
            initialfile="xdao-export.log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt")],
        )
        if not path:
            return
        try:
            Path(path).write_text(content + "\n", encoding="utf-8")
            self.log(f"日志已保存：{path}")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def log(self, message: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        self.root.update_idletasks()

    # ---------- 缓存 ----------

    def current_cache_dir(self) -> Path:
        """本次真正会用到的缓存目录。

        设置里填的目录如果写不进去（只读介质、权限受限等），抓取层会自动换个能写的
        地方，这里保持一致，免得界面显示的目录和实际用的是两个。
        """
        return resolve_cache_dir(self.settings.resolved_cache_dir(self.output_var.get().strip()))[0]

    def refresh_cache_info(self) -> None:
        requested = self.settings.resolved_cache_dir(self.output_var.get().strip())
        cache_dir, note = resolve_cache_dir(requested)
        threads = 0
        size = 0
        threads_dir = cache_dir / "threads"
        if threads_dir.exists():
            for path in threads_dir.glob("*.json"):
                threads += 1
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
        images_dir = cache_dir / "images"
        images = 0
        if images_dir.exists():
            for path in images_dir.rglob("*"):
                if path.is_file():
                    images += 1
                    try:
                        size += path.stat().st_size
                    except OSError:
                        pass
        text = f"缓存：{threads} 个串 · {images} 张图片 · {size / 1024 / 1024:.1f} MB · {cache_dir}"
        if note:
            text += f"（原目录 {requested} 写不进去，已自动改用这里）"
        self.cache_info_var.set(text)

    def clear_cache(self) -> None:
        cache_dir = self.current_cache_dir()
        if not cache_dir.exists():
            messagebox.showinfo("提示", "当前没有缓存。")
            return
        if not messagebox.askyesno(
            "清空缓存",
            f"将删除以下目录里的全部缓存：\n{cache_dir}\n\n"
            "已导出的文件不受影响，但下次导出需要重新下载。确定继续吗？",
        ):
            return
        import shutil

        try:
            shutil.rmtree(cache_dir)
            self.log(f"缓存已清空：{cache_dir}")
        except OSError as exc:
            messagebox.showerror("清空失败", str(exc))
        self.refresh_cache_info()

    # ---------- 饼干挑选 ----------

    def pick_cookies(self) -> None:
        urls = self._collect_urls()
        if not urls:
            messagebox.showwarning("提示", "请先在输入框里填写一个串网址。")
            return
        thread_id = parse_thread_id(urls[0])
        if thread_id is None:
            messagebox.showwarning("提示", "无法识别第一个网址里的串号。")
            return

        self.apply_settings_to_client()
        self.pick_cookie_button.config(state="disabled", text="读取中…")

        def work() -> None:
            try:
                payload = self.client.fetch_thread_page(thread_id, 1)
                if isinstance(payload, str):
                    raise XdaoError(payload)
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise XdaoError(str(payload.get("error") or "无法读取该串"))
                parsed = self.client.parse_thread_page(payload, 1)
                self._cookie_queue.put(("ok", parsed["posts"]))
            except Exception as exc:
                self._cookie_queue.put(("fail", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(80, self._poll_cookies)

    def _poll_cookies(self) -> None:
        try:
            kind, payload = self._cookie_queue.get_nowait()
        except queue.Empty:
            self.root.after(80, self._poll_cookies)
            return
        self.pick_cookie_button.config(state="normal", text="从串中挑选…")
        if kind == "fail":
            messagebox.showerror("读取失败", payload)
            return

        counter: dict[str, int] = {}
        po_hash = ""
        for post in payload:
            if post.is_po:
                po_hash = post.user_hash
            if post.user_hash:
                counter[post.user_hash] = counter.get(post.user_hash, 0) + 1
        if not counter:
            messagebox.showinfo("提示", "第一页里没有可选的饼干。")
            return
        CookiePicker(self.root, counter, po_hash, self.hashes_var)

    # ---------- 导出 ----------

    def _collect_urls(self) -> list[str]:
        return [
            line.strip()
            for line in self.urls_text.get("1.0", "end").splitlines()
            if line.strip()
        ]

    def start(self, urls: list[str] | None = None) -> None:
        if self._exporting:
            return
        if not self.settings.userhash:
            messagebox.showwarning("提示", "请先登录或设置饼干。")
            self.open_login()
            return

        urls = urls if urls is not None else self._collect_urls()
        if not urls:
            messagebox.showwarning("提示", "请至少输入一个串网址。")
            return

        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请选择导出目录。")
            return

        # 先确认这个目录写不写得进去：写不进去时立刻说清楚原因，
        # 而不是抓取跑到一半才失败（用户报过 343 秒后才报权限错误）。
        try:
            ensure_writable(output_dir)
        except OutputDirNotWritable as exc:
            messagebox.showerror("导出目录不可写", str(exc))
            return

        self.persist_prefs()
        self.apply_settings_to_client()

        self._exporting = True
        self.start_button.config(state="disabled")
        self.progress_bar.start(12)
        self.progress_var.set("开始导出…")
        self.log(
            f"开始导出：{len(urls)} 个串，格式 {self.current_format()}，"
            f"缓存 {'启用' if self.use_cache_var.get() else '关闭'}。"
        )

        def worker_body() -> None:
            scope = self.scope_var.get()
            format_key = self.current_format()
            include_hashes = self.parse_hashes()
            template = self.settings.filename_template or None
            use_cache = bool(self.use_cache_var.get())
            cache_dir = self.current_cache_dir()
            # 图片缓存跟着导出目录走，重复导出同一串时不再下载图片。
            self.client.image_cache_dir = (cache_dir / "images") if use_cache else None

            fetcher = CachedThreadFetcher(
                self.client,
                cache_dir=cache_dir,
                progress=lambda msg: self._export_queue.put(("log", msg)),
                use_cache=use_cache,
            )
            exporter = create_exporter(
                format_key,
                self.client,
                progress=lambda msg: self._export_queue.put(("log", msg)),
                filename_template=template,
                image_mode=self.current_image_mode(),
                browser_path=self.settings.pdf_browser or None,
            )

            succeeded = 0
            failed: list[str] = []
            started = time.time()
            for index, url in enumerate(urls, start=1):
                self._export_queue.put(
                    ("progress", (index - 1, len(urls), f"[{index}/{len(urls)}] {url}"))
                )
                try:
                    result = fetcher.fetch(url)
                    thread = ThreadData(
                        thread_id=result.meta.thread_id,
                        title=result.meta.title,
                        po_hash=result.meta.po_hash,
                        posts=list(result.posts),
                    )
                    self._export_queue.put(
                        (
                            "log",
                            f"[{index}/{len(urls)}] No.{thread.thread_id} "
                            f"{len(thread.posts)} 楼 · {result.reason}",
                        )
                    )
                    if getattr(result, "cache_warning", ""):
                        # 缓存出问题意味着这次抓取可能没留下断点续传的成果（换个目录继续、
                        # 或者干脆没写成），必须说清楚，否则用户会以为下次能续上。
                        self._export_queue.put(
                            (
                                "log",
                                f"[{index}/{len(urls)}] 提示：{result.cache_warning}",
                            )
                        )
                    path = exporter.save(
                        thread, scope, Path(output_dir), include_hashes=include_hashes
                    )
                    succeeded += 1
                    self._export_queue.put(("log", f"[{index}/{len(urls)}] 完成：{path.name}"))
                except XdaoError as exc:
                    failed.append(url)
                    self._export_queue.put(("log", f"[{index}/{len(urls)}] 失败：{exc}"))
                except Exception as exc:
                    failed.append(url)
                    self._export_queue.put(
                        ("log", f"[{index}/{len(urls)}] 失败：{type(exc).__name__}: {exc}")
                    )

            self._export_queue.put(
                ("done", (succeeded, len(urls), failed, time.time() - started))
            )

        def worker() -> None:
            # 这个线程里任何漏出来的异常都会让打包版弹出「Unhandled exception in script」
            # 对话框（用户看到过一次），所以在这里兜底，转成界面上的一条日志 + 结束事件。
            try:
                worker_body()
            except Exception as exc:  # noqa: BLE001 —— 兜住线程里的一切
                self._export_queue.put(
                    ("log", f"导出没能开始/继续：{type(exc).__name__}: {exc}")
                )
                self._export_queue.put(
                    ("done", (0, len(urls), list(urls), 0.0))
                )

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(50, self._poll_export)

    def retry_failed(self) -> None:
        if not self._last_failed:
            messagebox.showinfo("提示", "没有失败项需要重试。")
            return
        urls = list(self._last_failed)
        self.log(f"重试 {len(urls)} 个失败项。")
        self.start(urls)

    def _poll_export(self) -> None:
        while True:
            try:
                kind, payload = self._export_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                done, total, label = payload
                self.progress_bar.set_value(done, total)
                self.progress_var.set(f"{label}（{done}/{total}）")
            elif kind == "log":
                try:
                    self.log(payload)
                except Exception:
                    pass
            elif kind == "done":
                self._finish(*payload)
                return
        if self._exporting:
            self.root.after(50, self._poll_export)

    def _finish(self, succeeded: int, total: int, failed: list[str], elapsed: float) -> None:
        self._exporting = False
        self._last_failed = list(failed)
        self.progress_bar.stop()
        self.progress_bar.set_value(succeeded, total)
        self.start_button.config(state="normal")
        self.retry_button.config(state="normal" if failed else "disabled")
        self.progress_var.set(f"完成：成功 {succeeded} / {total} 个串，用时 {elapsed:.0f} 秒")
        self.log(f"全部处理完毕：成功 {succeeded} / {total} 个，用时 {elapsed:.1f} 秒。")
        if failed:
            self.log("失败清单：" + "、".join(failed))
        self.refresh_cache_info()
        if failed:
            messagebox.showwarning(
                "部分失败",
                f"成功 {succeeded} / {total} 个串。\n\n失败 {len(failed)} 个，原因见日志区，"
                "可点「重试失败项」重跑。",
            )
        else:
            messagebox.showinfo("完成", f"成功导出 {succeeded} / {total} 个串。")

    # ---------- 监控 ----------

    def refresh_watch_status(self) -> None:
        if self._watching:
            self.watch_status_var.set(f"· 监控中（{len(self.watch_targets)} 个串）")
        elif self.watch_targets:
            self.watch_status_var.set(f"· {describe_targets(self.watch_targets)}")
        else:
            self.watch_status_var.set("")

    def start_watching(self, interval: float, verify_cached: bool = False) -> None:
        if self._watching:
            return
        if not self.watch_targets:
            messagebox.showwarning("提示", "请先添加要监控的串。")
            return
        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请先选择导出目录。")
            return

        self.persist_prefs()
        self.persist_watch_targets()
        self.apply_settings_to_client()

        cache_dir = self.current_cache_dir()
        if self.use_cache_var.get():
            self.client.image_cache_dir = cache_dir / "images"
        targets = list(self.watch_targets)
        stop_event = threading.Event()
        self._watch_stop = stop_event
        self._watching = True

        def worker() -> None:
            self.watch_queue.put(
                f"监控已启动，每 {int(interval)} 秒检查一次（{len(targets)} 个串）。"
            )
            try:
                watch_forever(
                    self.client,
                    targets,
                    Path(output_dir),
                    interval,
                    stop_event=stop_event,
                    cache_dir=cache_dir,
                    on_result=lambda result: self.watch_queue.put(result),
                    verify_cached=verify_cached,
                )
            except Exception as exc:  # noqa: BLE001 —— 监控线程也不能把 traceback 弹给用户
                self.watch_queue.put(f"监控出错：{type(exc).__name__}: {exc}")
                self._watching = False
            finally:
                self.watch_queue.put("监控已停止。")

        threading.Thread(target=worker, daemon=True).start()
        self.log(f"监控已启动：每 {int(interval)} 秒检查 {len(targets)} 个串。")
        self.refresh_watch_status()
        self._poll_watch()

    def stop_watching(self) -> None:
        if not self._watching:
            return
        if self._watch_stop:
            self._watch_stop.set()
        self._watching = False
        self.log("已请求停止监控。")
        self.refresh_watch_status()

    def check_watch_once(self) -> None:
        """立即后台检查一轮（不影响正在运行的监控循环）。"""
        if not self.watch_targets:
            messagebox.showwarning("提示", "请先添加要监控的串。")
            return
        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请先选择导出目录。")
            return
        self.persist_prefs()
        self.apply_settings_to_client()

        cache_dir = self.current_cache_dir()
        if self.use_cache_var.get():
            self.client.image_cache_dir = cache_dir / "images"
        targets = list(self.watch_targets)
        verify = bool(self.settings.verify_cached)

        def worker() -> None:
            for target in targets:
                result = check_once(
                    self.client,
                    target,
                    Path(output_dir),
                    cache_dir=cache_dir,
                    verify_cached=verify,
                )
                self.watch_queue.put(result)

        self.log("正在检查监控列表（一次性）…")
        threading.Thread(target=worker, daemon=True).start()
        self._poll_watch()

    def _poll_watch(self) -> None:
        """把监控线程的消息写进日志（主窗口始终生效，不依赖监控窗口是否打开）。"""
        got = False
        while True:
            try:
                item = self.watch_queue.get_nowait()
            except queue.Empty:
                break
            got = True
            if isinstance(item, str):
                self.log(item)
                continue
            target = item.target
            if getattr(item, "error", ""):
                self.log(f"监控 No.{target.label}：出错 {item.error}")
            elif getattr(item, "exported", None):
                label = "首次导出" if item.first_run else f"新增 {item.new_posts} 楼"
                self.log(f"监控 No.{target.label}：{label} → {item.exported.name}")
            else:
                self.log(f"监控 No.{target.label}：{item.reason or '无更新'}")
        if got:
            self.refresh_cache_info()
            self.refresh_watch_status()
        if self._watching or got:
            self.root.after(1000, self._poll_watch)


def run() -> None:
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 —— 打包版（--windowed）没有 stderr，崩了就是一片空白
        _report_fatal("启动界面失败", exc)
        raise SystemExit(1) from None

    # Tk 回调里抛出的异常默认交给 report_callback_exception，而它默认往 stderr 打印；
    # 打包版没有 stderr，于是异常会一路穿到 PyInstaller 启动器，弹成
    # 「Unhandled exception in script」对话框。这里改成写日志 + 弹对话框。
    def on_tk_error(exc_type, exc_value, exc_tb) -> None:  # pragma: no cover - 需要真实事件循环
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        app.log(f"界面出错（已记录，程序继续运行）：\n{text}")
        try:
            messagebox.showerror("界面出错", f"{exc_type.__name__}: {exc_value}")
        except Exception:  # noqa: BLE001
            pass

    root.report_callback_exception = on_tk_error

    try:
        app = App(root)
    except Exception as exc:  # noqa: BLE001
        _report_fatal("启动界面失败", exc)
        raise SystemExit(1) from None

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


def _report_fatal(title: str, exc: BaseException) -> None:
    """界面起不来时的最后一道输出：尽量弹个对话框，弹不出来也别再抛异常。"""
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        import tkinter.messagebox as fallback_box

        fallback_box.showerror(
            title,
            f"{type(exc).__name__}: {exc}\n\n"
            "常见原因：配置文件损坏，或导出目录不可用。\n"
            "可以删掉 %APPDATA%\\xdao-export\\config.json 后重试。\n\n"
            f"详细信息：\n{detail[-1200:]}",
        )
    except Exception:  # noqa: BLE001
        pass
