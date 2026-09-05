"""Tkinter 图形界面。"""

from __future__ import annotations

import threading
import tkinter as tk
import queue
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .builder import HtmlBuilder, TxtBuilder
from .client import LoginError, XdaoClient, XdaoError
from .fetcher import ThreadFetcher
from .settings import AppSettings


ACCENT = "#3b82f6"
ACCENT_HOVER = "#2563eb"
ACCENT_DISABLED = "#9db8e8"
BG = "#f5f7fb"
CARD = "#ffffff"
TEXT = "#1f2430"
MUTED = "#6b7280"
BORDER = "#e2e8f0"

FONT_UI = "Microsoft YaHei UI"
TITLE_FONT = (FONT_UI, 18, "bold")
SUBTITLE_FONT = (FONT_UI, 9)
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
    style.map(
        "Secondary.TButton",
        background=[("active", "#e2e8f0")],
    )
    style.configure(
        "TEntry",
        fieldbackground=CARD,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        padding=6,
    )
    style.configure("TCheckbutton", background=BG)
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
        ).pack(
            side="left", padx=6
        )
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


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.style = setup_style(root)
        root.configure(bg=BG)
        self.settings = AppSettings.load()
        self.client = XdaoClient()
        self._export_queue: queue.Queue = queue.Queue()
        self._exporting = False
        if self.settings.userhash:
            self.client.set_userhash(self.settings.userhash)

        root.title("X岛串导出")
        root.geometry("760x720")
        root.minsize(660, 620)

        outer = ttk.Frame(root, padding=(18, 16))
        outer.pack(fill="both", expand=True)

        # 渐变标题头
        header = tk.Canvas(outer, height=72, bg=BG, highlightthickness=0)
        header.pack(fill="x")
        header.bind("<Configure>", lambda e: self._draw_header(header))
        self._header_canvas = header

        # 登录状态行
        status_row = ttk.Frame(outer)
        status_row.pack(fill="x", pady=(10, 14))
        self.status_var = tk.StringVar(
            value="已登录" if self.settings.userhash else "未登录"
        )
        status_color = "#16a34a" if self.settings.userhash else MUTED
        ttk.Label(
            status_row,
            textvariable=self.status_var,
            foreground=status_color,
            font=(FONT_UI, 10, "bold"),
        ).pack(side="left")
        ttk.Button(
            status_row,
            text="登录 / 设置饼干",
            style="Secondary.TButton",
            command=self.open_login,
        ).pack(side="right")

        # 网址输入卡片
        url_shadow = tk.Frame(outer, bg="#e3e8f0")
        url_shadow.pack(fill="both", expand=True, pady=(0, 12))
        url_card = tk.Frame(url_shadow, bg=CARD, padx=14, pady=12)
        url_card.pack(fill="both", expand=True, padx=1, pady=1)
        ttk.Label(url_card, text="① 输入串网址", style="CardSection.TLabel").pack(anchor="w")
        ttk.Label(
            url_card, text="每行一个，可一次粘贴多个", style="CardMuted.TLabel"
        ).pack(anchor="w", pady=(0, 8))
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

        # 抓取范围
        scope_frame = ttk.Frame(url_card, style="Card.TFrame")
        scope_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(scope_frame, text="抓取范围", style="Card.TLabel", font=SECTION_FONT).pack(side="left")
        self.scope_var = tk.StringVar(value="all")
        ttk.Radiobutton(
            scope_frame, text="所有人发言", variable=self.scope_var, value="all"
        ).pack(side="left", padx=(12, 14))
        ttk.Radiobutton(
            scope_frame, text="只抓 PO 发言", variable=self.scope_var, value="po"
        ).pack(side="left")

        # 导出格式
        format_frame = ttk.Frame(url_card, style="Card.TFrame")
        format_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(format_frame, text="导出格式", style="Card.TLabel", font=SECTION_FONT).pack(side="left")
        self.format_var = tk.StringVar(value="html")
        ttk.Radiobutton(
            format_frame, text="HTML（图片嵌入）", variable=self.format_var, value="html"
        ).pack(side="left", padx=(12, 14))
        ttk.Radiobutton(
            format_frame, text="TXT（纯文本）", variable=self.format_var, value="txt"
        ).pack(side="left")

        # 导出目录
        folder_shadow = tk.Frame(outer, bg="#e3e8f0")
        folder_shadow.pack(fill="x", pady=(0, 14))
        folder_card = tk.Frame(folder_shadow, bg=CARD, padx=14, pady=12)
        folder_card.pack(fill="x", padx=1, pady=1)
        ttk.Label(folder_card, text="② 导出目录", style="CardSection.TLabel").pack(anchor="w", pady=(0, 8))
        folder_inner = ttk.Frame(folder_card, style="Card.TFrame")
        folder_inner.pack(fill="x")
        self.output_var = tk.StringVar(
            value=self.settings.output_dir
            or str(Path.home() / "Documents" / "X岛备份")
        )
        ttk.Entry(folder_inner, textvariable=self.output_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            folder_inner,
            text="更改…",
            style="Secondary.TButton",
            command=self.choose_folder,
        ).pack(side="left", padx=(8, 0))

        # 主操作按钮
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="开始导出", command=self.start, width=18)
        self.start_button.pack(side="left")
        ttk.Button(
            buttons,
            text="打开导出目录",
            style="Secondary.TButton",
            command=self.open_folder,
        ).pack(side="left", padx=(8, 0))

        # 进度
        self.progress_var = tk.StringVar(value="等待开始")
        progress_head = ttk.Frame(outer)
        progress_head.pack(fill="x", pady=(14, 6))
        ttk.Label(progress_head, textvariable=self.progress_var, style="Muted.TLabel").pack(side="left")
        self.progress_bar = ModernProgress(outer, height=8)
        self.progress_bar.pack(fill="x")

        # 日志
        ttk.Label(outer, text="运行日志", style="Section.TLabel").pack(anchor="w", pady=(14, 4))
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
            color = f"#{r:02x}{g:02x}{b:02x}"
            canvas.create_line(0, i, w, i, fill=color)
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
            text="把任意一个串完整备份为 HTML 或 TXT 文件",
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

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择导出目录", initialdir=self.output_var.get() or None
        )
        if chosen:
            self.output_var.set(chosen)

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

    def log(self, message: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        self.root.update_idletasks()

    # ---------- 导出 ----------

    def start(self) -> None:
        if self._exporting:
            return
        if not self.settings.userhash:
            messagebox.showwarning("提示", "请先登录或设置饼干。")
            self.open_login()
            return

        urls = [
            line.strip()
            for line in self.urls_text.get("1.0", "end").splitlines()
            if line.strip()
        ]
        if not urls:
            messagebox.showwarning("提示", "请至少输入一个串网址。")
            return

        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请选择导出目录。")
            return

        self.settings.output_dir = output_dir
        self.settings.save()

        self._exporting = True
        self.start_button.config(state="disabled")
        self.progress_bar.start(12)
        self.progress_var.set("开始导出…")
        self.log("开始导出任务。")

        def worker() -> None:
            scope = self.scope_var.get()
            export_format = self.format_var.get()
            if export_format == "txt":
                builder = TxtBuilder(
                    self.client,
                    progress=lambda msg: self._export_queue.put(("log", msg)),
                )
            else:
                builder = HtmlBuilder(
                    self.client,
                    progress=lambda msg: self._export_queue.put(("log", msg)),
                )
            succeeded = 0
            for index, url in enumerate(urls, start=1):
                self._export_queue.put(("status", f"正在处理：{url}"))
                try:
                    self._export_queue.put(("log", f"开始：{url}"))
                    fetcher = ThreadFetcher(
                        self.client,
                        progress=lambda msg: self._export_queue.put(("log", msg)),
                    )
                    thread = fetcher.fetch(url)
                    path = builder.save(thread, scope, Path(output_dir))
                    succeeded += 1
                    self._export_queue.put(("log", f"完成：{path.name}"))
                except XdaoError as exc:
                    self._export_queue.put(("log", f"失败：{exc}"))
                except Exception as exc:
                    self._export_queue.put(("log", f"失败：{exc}"))
            self._export_queue.put(("done", (succeeded, len(urls))))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(50, self._poll_export)

    def _poll_export(self) -> None:
        while True:
            try:
                kind, payload = self._export_queue.get_nowait()
                if kind == "status":
                    self.progress_var.set(payload)
                elif kind == "log":
                    try:
                        self.log(payload)
                    except Exception:
                        pass
                elif kind == "done":
                    self._finish(payload[0], payload[1])
                    return
            except queue.Empty:
                break
        if self._exporting:
            self.root.after(50, self._poll_export)

    def _finish(self, succeeded: int, total: int) -> None:
        self._exporting = False
        self.progress_bar.stop()
        self.progress_bar.set_value(100, 100)
        self.start_button.config(state="normal")
        self.progress_var.set(f"完成：成功 {succeeded} / {total} 个串")
        self.log(f"全部处理完毕：成功 {succeeded} / {total} 个。")
        messagebox.showinfo("完成", f"成功导出 {succeeded} / {total} 个串。")


def run() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()
