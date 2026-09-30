"""Tkinter 图形界面。

配色、字体、间距与 ttk 样式集中在 :mod:`xdao.theme`，圆角卡片、状态胶囊、
扁平文本域等自绘控件在 :mod:`xdao.widgets`；本文件只负责布局与业务流转，
不要再写死色值。

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
from .exporters._shared import OutputDirNotWritable, choose_writable_dir, ensure_writable
from .fetcher import parse_thread_id
from .notifications import Notifier
from . import theme
from .settings import AppSettings
from .theme import apply_theme, font, mono, resolve_fonts
from .watcher import WatchTarget, check_once, describe_targets, notify_result, watch_forever
from .widgets import Card, ModernProgress, SectionHeading, StatusPill


# 当前配色（切换主题时由 refresh_colors 重新绑定）。**代码里一律用 _PAL，
# 不要再从 theme 直接 import PALETTE** —— 那样拿到的是导入期的旧对象，
# 换肤后新画出来的控件会继续用旧色（这是实测踩过的坑：切到深色后日志框
# 还是浅色 #f7f9fc）。
_PAL = theme.PALETTE
_PAL_NAME = _PAL.name

# 颜色与字体的短别名（历史原因：对话框里到处都在用），取值一律来自 theme，
# 想换配色只改 xdao/theme.py，不要在界面文件里写死色值。
ACCENT = _PAL.accent
ACCENT_HOVER = _PAL.accent_hover
ACCENT_DISABLED = _PAL.accent_soft
BG = _PAL.bg
CARD = _PAL.surface
TEXT = _PAL.text
MUTED = _PAL.muted
BORDER = _PAL.border
OK_GREEN = _PAL.ok


def refresh_colors() -> None:
    """按 ``theme.PALETTE`` 重算上面那批颜色别名。

    模块级别名是导入期绑定的值，切换主题后不会自己变；重建界面之前
    必须先调它，否则新画出来的控件还是旧配色（这是实测踩过的坑：
    只调 ``apply_theme`` 时 ttk 控件变了、自绘卡片和文本域仍是浅色）。
    """
    global _PAL, _PAL_NAME
    global ACCENT, ACCENT_HOVER, ACCENT_DISABLED, BG, CARD, TEXT, MUTED, BORDER, OK_GREEN
    _PAL = theme.PALETTE
    _PAL_NAME = _PAL.name
    ACCENT = _PAL.accent
    ACCENT_HOVER = _PAL.accent_hover
    ACCENT_DISABLED = _PAL.accent_soft
    BG = _PAL.bg
    CARD = _PAL.surface
    TEXT = _PAL.text
    MUTED = _PAL.muted
    BORDER = _PAL.border
    OK_GREEN = _PAL.ok


def _sync_dialog_colors(widget: tk.Misc, old: object = None, new: object = None) -> None:
    """把 ``widget`` 这棵子树里的原生 tk 控件从旧配色迁移到当前配色。

    主题切换时 :class:`App` 会重建自己的控件，但**已经打开的对话框**
    （设置/监控/饼干列表）不在重建范围内，不迁移的话它们会顶着旧底色
    留在新主题里。迁移只改"颜色恰好等于旧配色某个值"的选项，别的
    选项（文字、命令、状态）一律不碰。

    ``old``/``new`` 必须由调用方在 ``refresh_colors()`` **之前**取好：
    那一步会把 ``_PAL`` 换成新配色，之后再取旧色就晚了（实测踩过：
    迁移映射变成"新→新"，对话框一点没变）。
    """
    old = old if old is not None else theme.PALETTE
    new = new if new is not None else theme.PALETTE
    mapping = theme.color_map(old, new)
    if not mapping:
        return

    def fix(widget: tk.Misc) -> None:
        try:
            keys = widget.keys()
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return
        for key in ("bg", "background", "fg", "foreground", "activebackground",
                    "activeforeground", "selectbackground", "selectforeground",
                    "highlightbackground", "highlightcolor", "insertbackground",
                    "disabledbackground", "disabledforeground", "selectcolor",
                    "readonlybackground", "troughcolor"):
            if key not in keys:
                continue
            try:
                value = str(widget.cget(key))
            except tk.TclError:  # pragma: no cover - 控件已销毁
                continue
            replacement = mapping.get(value)
            if replacement is not None:
                try:
                    widget.configure(**{key: replacement})
                except tk.TclError:  # pragma: no cover - 个别控件不接受
                    pass
        try:
            children = widget.winfo_children()
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return
        for child in children:
            fix(child)

    fix(widget)


def refresh_open_dialogs(root: tk.Misc, old: object = None, new: object = None) -> None:
    """把当前打开的所有 Toplevel（对话框）从 ``old`` 配色迁移到 ``new``。"""
    try:
        children = root.winfo_children()
    except tk.TclError:  # pragma: no cover - 根窗口已销毁
        return
    for child in children:
        if isinstance(child, tk.Toplevel):
            _sync_dialog_colors(child, old, new)


FONT_UI = theme.FONT_UI
SECTION_FONT = font(theme.SIZE_SUBHEAD, bold=True)
BODY_FONT = font(theme.SIZE_BODY)
SMALL_FONT = font(theme.SIZE_SMALL)
MONO_FONT = mono(theme.SIZE_SMALL)


def refresh_fonts(root: tk.Misc | None = None) -> None:
    """重算模块级字体别名，跟 ``theme`` 探测出的真实字体保持一致。

    导入期还没有根窗口，``theme`` 只能给出候选里的第一个；等界面起来后
    ``theme.resolve_fonts`` 才探到真正装了的族名。不重算的话，"控件实际
    用的字体"与 ``theme.FONT_MONO`` 就会分叉（CI 上表现为
    ``assert 'Consolas' == 'Cascadia Mono'``）。``setup_style()`` 会调它。
    """
    global FONT_UI, SECTION_FONT, BODY_FONT, SMALL_FONT, MONO_FONT
    theme.resolve_fonts(root)
    FONT_UI = theme.FONT_UI
    SECTION_FONT = font(theme.SIZE_SUBHEAD, bold=True)
    BODY_FONT = font(theme.SIZE_BODY)
    SMALL_FONT = font(theme.SIZE_SMALL)
    MONO_FONT = mono(theme.SIZE_SMALL)


def describe_login_failure(message: str) -> tuple[str, str]:
    """把登录失败的提示翻成「对话框标题 + 该做的事」。

    实测过：邮箱登录点了之后只看到一句「未找到可用的饼干」。真相是
    X 岛的跳转提示页（HTTP 200）被当成了饼干列表，真正的失败原因
    （登录没过 / 没有权限 / 账号里确实没有饼干）被盖掉了。
    这里把几种情况分开说，并且都给一个能马上做的动作。
    """
    text = (message or "").strip()
    if "验证码" in text and "错" in text:
        return "验证码不对", f"{text}\n\n验证码图片已经换成新的一张，重新填一次再登录。"
    if "密码" in text and ("错" in text or "不正确" in text):
        return "密码不对", f"{text}\n\n请确认密码；也可以点下面的「用浏览器登录」，让程序自己从浏览器里取一次。"
    if "账号" in text and ("不存在" in text or "错" in text):
        return "账号有问题", f"{text}\n\n请确认邮箱地址；也可以点下面的「用浏览器登录」。"
    if "没能进入用户系统" in text or "没能读取饼干列表" in text:
        return (
            "登录没有生效",
            f"{text}\n\n"
            "多半是这次登录没被服务端认下来（验证码过期、密码刚改过、账号被限制）。"
            "请点「登录」重新来一次，验证码务必用最新那张。\n"
            "如果反复失败，请点「用浏览器登录」让程序自己去取一次，"
            "或者点「直接粘贴饼干登录」手动粘贴。",
        )
    if "饼干列表是空的" in text:
        return (
            "这个账号还没有饼干",
            f"{text}\n\n"
            "在浏览器里登录 X 岛用户系统 →「饼干」→ 领取并应用一块饼干，"
            "然后回到程序重新登录。",
        )
    return "登录失败", text


def setup_style(root: tk.Tk) -> ttk.Style:
    """装上现代扁平配色 + 定下真实字体（实际工作都在 xdao.theme 里）。"""
    style = apply_theme(root)
    refresh_fonts(root)
    return style


def describe_export_failure(exc: BaseException) -> str:
    """把导出线程里的异常翻成一句能让用户动手的话。

    两类错误占绝大多数：
    - ``PermissionError``：反复出现「导出目录不可写」，只甩一句
      ``[Errno 13] Permission denied`` 帮不上忙，得说清是哪个目录、往哪换；
    - 接口回「必须登入领取饼干后才可以访问」：这是**服务端**的拒绝，
      本地日志却只有这一行，用户会以为是程序坏了，得告诉他去重新登录。
    """
    text = f"{type(exc).__name__}: {exc}"
    if "必须登入" in str(exc) or "领取饼干" in str(exc):
        return (
            f"{text}\n    X 岛接口拒绝了这次访问：受限版块/带权限的串必须带上有效的"
            "「饼干」（userhash）才给读。程序这边已经把登录状态发过去了，"
            "服务端仍然拒绝，通常是饼干已失效或账号掉线。\n"
            "    请点右上角「登录 / 设置饼干」重新登录一次（会自动重新应用饼干）；"
            "还不行就用登录窗口的「用浏览器登录」让程序自己去取一次，"
            "或者把浏览器里的 cookie 整段贴进「直接粘贴饼干登录」。"
        )
    if not isinstance(exc, PermissionError):
        return text
    target = getattr(exc, "filename", None) or ""
    where = f"目录 {Path(target).parent}" if target else "导出目录"
    return (
        f"{text}\n    Windows 不允许往{where}写文件。最常见的原因是安全软件的"
        "「受控文件夹访问」（Windows 安全中心 → 病毒和威胁防护 → 勒索软件防护），"
        "它默认只放行白名单程序写「桌面 / 文档 / 图片 / 视频」这几个位置；"
        "本程序这一版起会自动改用 %LOCALAPPDATA%\\xdao-export\\导出 这类不受保护的位置，"
        "也可以手动把导出目录换过去，或把本程序加入白名单。"
    )


class LoginDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Tk,
        client: XdaoClient,
        settings: AppSettings | None = None,
    ) -> None:
        super().__init__(master)
        self.configure(bg=BG)
        self.client = client
        # 「用浏览器登录」要知道把浏览器 profile 放哪、要不要走代理，都取自这份设置。
        # 不传就自己读一次配置：老的调用方不必跟着改签名。
        self.settings = settings if settings is not None else AppSettings.load()
        self.userhash: str | None = None
        self._result_queue: queue.Queue = queue.Queue()
        self._busy = False
        self.title("登录 X 岛")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.form = None

        outer = ttk.Frame(self, padding=(theme.gap(4), theme.gap(3)))
        outer.pack(fill="both", expand=True)

        card = Card(outer)
        card.pack(fill="x")
        SectionHeading(card.body, "登录 X 岛").pack(fill="x")

        form = ttk.Frame(card.body, style="Card.TFrame")
        form.pack(fill="x", pady=(theme.gap(2), 0))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="账号（邮箱）", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=theme.gap(1)
        )
        self.email_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.email_var).grid(
            row=0, column=1, sticky="ew", padx=(theme.gap(2), 0), pady=theme.gap(1)
        )

        ttk.Label(form, text="密码", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=theme.gap(1)
        )
        self.password_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.password_var, show="*").grid(
            row=1, column=1, sticky="ew", padx=(theme.gap(2), 0), pady=theme.gap(1)
        )

        ttk.Label(form, text="验证码", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=theme.gap(1)
        )
        verify_row = ttk.Frame(form, style="Card.TFrame")
        verify_row.grid(row=2, column=1, sticky="w", padx=(theme.gap(2), 0), pady=theme.gap(1))
        self.verify_var = tk.StringVar()
        ttk.Entry(verify_row, textvariable=self.verify_var, width=14).pack(side="left")
        self._captcha_image = None
        self.captcha_button = ttk.Button(
            verify_row, text="加载中…", style="Secondary.TButton", command=self._refresh_captcha
        )
        self.captcha_button.pack(side="left", padx=(theme.gap(2), 0))

        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            card.body,
            text="记住登录状态（本机保存 userhash）",
            variable=self.remember_var,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(theme.gap(1), 0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(theme.gap(3), 0))
        ttk.Button(
            buttons,
            text="直接粘贴饼干登录",
            style="Ghost.TButton",
            command=self._manual_userhash,
        ).pack(side="left")
        ttk.Button(buttons, text="取消", style="Secondary.TButton", command=self.destroy).pack(
            side="right", padx=(theme.gap(1), 0)
        )
        self.login_button = ttk.Button(buttons, text="登录", command=self._do_login)
        self.login_button.pack(side="right")
        # 最省事的一条路：开一个独立浏览器窗口去登录，程序在旁边把饼干取回来。
        # 放在「登录」左边，因为实测账号密码这条路经常被验证码/跳转卡住。
        self.browser_button = ttk.Button(
            buttons,
            text="用浏览器登录",
            style="Secondary.TButton",
            command=self._open_browser_login,
        )
        self.browser_button.pack(side="right", padx=(0, theme.gap(1)))
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
        title, body = describe_login_failure(payload)
        messagebox.showerror(title, body, parent=self)
        self._refresh_captcha()

    def _manual_userhash(self) -> None:
        """账号密码登录走不通时的兜底：把浏览器里的饼干整段粘进来即可。"""
        # 延迟导入：这个模块只管「从粘贴的文字里摘 userhash」这一件事，
        # 界面本身不依赖它，缺了也不该影响窗口启动。
        from .browser_login import looks_like_userhash, parse_userhash_input

        value = simpledialog.askstring(
            "粘贴饼干登录",
            "在浏览器里登录 X 岛用户系统，把 cookie 整段复制粘贴到下面就行"
            "（userhash=... 也在这段里），程序会自己把值摘出来。\n"
            "不需要打开开发者工具。",
            parent=self,
        )
        if not value or not value.strip():
            return
        userhash = parse_userhash_input(value)
        if not userhash or not looks_like_userhash(userhash):
            messagebox.showwarning(
                "没找到 userhash",
                "粘贴的内容里没找到 userhash，"
                "请确认复制的是浏览器里的整段 cookie（或至少包含 userhash=... 的那一部分）。",
                parent=self,
            )
            return
        try:
            self.client.set_userhash(userhash)
            self.userhash = userhash
            self.destroy()
        except Exception as exc:
            messagebox.showerror("设置失败", str(exc), parent=self)

    def _open_browser_login(self) -> None:
        """开「用浏览器登录」子窗口；成功后跟别的登录方式一样，把结果交给调用方。

        本对话框自己 ``grab_set()`` 过，而子窗口也要用鼠标：先把 grab 放开，
        等子窗口关掉再收回来（子窗口成功时会把本对话框一起关掉，那时就不用了）。
        """
        self.grab_release()
        dialog = BrowserLoginDialog(self, self.client, self.settings)
        try:
            self.wait_window(dialog)
        finally:
            try:
                if self.winfo_exists():
                    self.grab_set()
            except tk.TclError:  # pragma: no cover - 窗口已经不可用了
                pass
        if dialog.userhash:
            self.userhash = dialog.userhash
            self.destroy()


# ---------- 用浏览器登录 ----------

# 后台线程查饼干的间隔（秒）：太密会把 CDP 调用排满，太稀用户登录完要干等。
BROWSER_POLL_SECONDS = 1.5
# 没读到 userhash 时，隔这么久去饼干页领一次（用户登录成功那一刻正好用上）。
BROWSER_LEAF_SECONDS = 5.0
# 等用户登录的上限；到点给一句能照做的话，而不是一直转圈。
BROWSER_LOGIN_TIMEOUT = 300.0
# 等浏览器把调试端口写出来的上限（冷启动 + 首次建 profile 会偏慢）。
BROWSER_START_TIMEOUT = 30.0
# 主线程消费消息队列的间隔（毫秒），跟本文件其它对话框保持一致。
BROWSER_UI_POLL_MS = 150


def _load_browser_login():
    """惰性导入 :mod:`xdao.browser_login`。

    「用浏览器登录」才需要它（里面是一整套 CDP 客户端），平时不该拖累界面启动；
    惰性导入还能把「模块缺失」变成界面上一句人话，而不是让窗口直接打不开。
    """
    from . import browser_login

    return browser_login


class BrowserLoginDialog(tk.Toplevel):
    """「用浏览器登录」：开一个独立浏览器窗口，程序在旁边等着取饼干。

    为什么要有它：X 岛的账号密码登录经常卡在验证码和跳转上，而让人开 F12
    复制 cookie 对普通用户太不友好。这里用**独立 profile** 起 Edge/Chrome
    （不动用户自己浏览器的任何配置），通过 CDP 读饼干，拿到 userhash 就自动
    关掉浏览器。

    线程模型：浏览器、CDP 会话都归后台线程管，它只往 ``_queue`` 投消息；
    ``_poll`` 在主线程消费。本类是 Tk 组件，所以用自己的 ``after``。
    """

    def __init__(
        self,
        master: tk.Tk,
        client: XdaoClient,
        settings: AppSettings | None = None,
    ) -> None:
        super().__init__(master)
        self.configure(bg=BG)
        self.client = client
        self.settings = settings if settings is not None else AppSettings.load()
        # 成功后交给 LoginDialog；None 表示这次没成（用户取消或失败）。
        self.userhash: str | None = None

        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._browser = None  # backend.LoginBrowser，启动后才有
        self._session = None
        self._ui_job: str | None = None
        self._closing = False

        self.title("用浏览器登录 X 岛")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        # 关窗就是取消：必须先把线程、CDP、浏览器进程收干净再销毁窗口。
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        outer = ttk.Frame(self, padding=(theme.gap(4), theme.gap(3)))
        outer.pack(fill="both", expand=True)

        card = Card(outer)
        card.pack(fill="x")
        SectionHeading(card.body, "用浏览器登录").pack(fill="x")
        tk.Label(
            card.body,
            text=(
                "点开之后会弹出一个独立的浏览器窗口。在里面像平时一样登录 X 岛用户系统，"
                "该输密码就输密码、该点验证码就点验证码；登录成功后程序会自己把饼干取回来，"
                "浏览器窗口也会自动关掉。\n"
                "不需要按 F12，也不用复制任何东西；这个窗口用的是单独的浏览器配置，"
                "不会动你自己浏览器里的登录状态。"
            ),
            bg=CARD,
            fg=MUTED,
            font=SMALL_FONT,
            justify="left",
            anchor="w",
            wraplength=420,
        ).pack(fill="x", pady=(theme.gap(2), 0))

        self.status_var = tk.StringVar(value="正在准备…")
        ttk.Label(
            card.body,
            textvariable=self.status_var,
            style="CardMuted.TLabel",
            wraplength=420,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(theme.gap(2), 0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(theme.gap(3), 0))
        ttk.Button(
            buttons,
            text="直接粘贴饼干登录",
            style="Ghost.TButton",
            command=self._manual_userhash,
        ).pack(side="left")
        ttk.Button(buttons, text="取消", style="Secondary.TButton", command=self._on_cancel).pack(
            side="right", padx=(theme.gap(1), 0)
        )
        self.retry_button = ttk.Button(
            buttons, text="重新打开浏览器", style="Secondary.TButton", command=self._restart
        )
        self.retry_button.pack(side="right", padx=(theme.gap(1), 0))
        self.retry_button.config(state="disabled")

        # 窗口先画出来，再开浏览器：不然点下去要愣一下才有反应。
        self.after(20, self.start)

    # ---------- 主线程：状态与收尾 ----------

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def start(self) -> None:
        """打开浏览器并开始等饼干；已经在跑时重复调用没有副作用。"""
        if self._closing:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown()  # 上一轮的进程/会话可能还在（重开时）
        self._stop.clear()
        self._set_status("正在打开浏览器窗口，请稍等…")
        self.retry_button.config(state="disabled")
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        if self._ui_job is None:
            self._ui_job = self.after(BROWSER_UI_POLL_MS, self._poll)

    def _restart(self) -> None:
        """重开浏览器：先把上一轮收干净（含 join 线程），再起新的一轮。"""
        self._shutdown()
        self.start()

    def _poll(self) -> None:
        """主线程：消费后台消息（本类是 Tk 组件，用自己的 after）。"""
        self._ui_job = None
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "ok":
                try:
                    self.userhash = self.client.import_userhash(str(payload))
                except Exception as exc:
                    self._set_status(f"饼干取到了，但设置失败：{exc}")
                    self.retry_button.config(state="normal")
                    continue
                # 拿到就走了：收尾（停线程/关 CDP/杀浏览器）必须在 destroy 之前。
                self._shutdown()
                self.destroy()
                return
            if kind == "ready":
                self._set_status(str(payload))
                continue
            if kind == "note":  # 只是提醒一句（比如没能自动把窗口切到登录页），别当失败
                current = self.status_var.get()
                self._set_status(f"{current}（{payload}）" if current else str(payload))
                continue
            if kind == "browser_closed":
                self._set_status(
                    "浏览器窗口已经关掉了，还没取到饼干。点「重新打开浏览器」重开一个，"
                    "或者点「直接粘贴饼干登录」。"
                )
            else:  # 剩下的都是错误
                self._set_status(str(payload))
            self.retry_button.config(state="normal")
        if not self._closing:
            self._ui_job = self.after(BROWSER_UI_POLL_MS, self._poll)

    def _on_cancel(self) -> None:
        """取消 / 点关闭：线程、CDP、浏览器进程一样都不许留下再销毁窗口。"""
        self._closing = True
        self._shutdown()
        self.destroy()

    def _release(self) -> None:
        """停线程、关 CDP 会话、结束浏览器进程。可重复调用，不碰 Tk —— 后台线程也能调。

        顺序是故意的：先 set 停止位（后台线程从 wait 里立刻醒），再关 CDP
        （后台若正卡在读饼干上，关掉会话能让它马上抛出来），最后才等线程。
        """
        self._stop.set()
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:  # pragma: no cover - 会话可能已经断了
                pass
        browser, self._browser = self._browser, None
        if browser is not None:
            self._release_browser(browser)
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _release_browser(self, browser) -> None:
        """按**实例**结束浏览器进程，并把登记处清干净。可重复调用，不碰 Tk。

        为什么不能只靠 ``_release()`` 里的 ``self._browser``：用户取消（关窗 / 粘贴饼干）
        可能正好落在「已经登记、进程还没起来」的那一瞬 —— 主线程那次 ``_release()``
        既停不掉任何东西（``process`` 还是 None），又把 ``self._browser`` 清成了 None。
        后台线程随后才把进程真正拉起来，此时再回头看 ``self._browser`` 已经什么都没有了，
        进程就没人管了。所以启动/握手这些「起来之后才发现已取消」的分支必须拿
        **自己的局部引用**来收尾，而不是重新去读登记处。
        """
        if self._browser is browser:
            self._browser = None
        try:
            browser.stop()  # 内部 terminate → 等一会 → kill，不往外抛
        except Exception:  # pragma: no cover - 收尾路径不能把异常丢回界面
            pass

    def _shutdown(self) -> None:
        """主线程收尾：先放掉资源，再撤掉还没跑的那次轮询。"""
        self._release()
        if self._ui_job is not None:
            try:
                self.after_cancel(self._ui_job)
            except Exception:  # pragma: no cover - 窗口可能已经销毁
                pass
            self._ui_job = None

    # ---------- 兜底：手动粘贴 ----------

    def _manual_userhash(self) -> None:
        """不想开浏览器的话，也可以自己把饼干整段粘进来。"""
        try:
            backend = _load_browser_login()
        except Exception:
            backend = None
        value = simpledialog.askstring(
            "直接粘贴饼干登录",
            "在浏览器里登录 X 岛用户系统，把 cookie 整段复制粘贴到下面就行"
            "（userhash=... 也在这段里），程序会自己把值摘出来。",
            parent=self,
        )
        if not value or not value.strip():
            return
        userhash = ""
        if backend is not None:
            try:
                userhash = backend.parse_userhash_input(value)
                if userhash and not backend.looks_like_userhash(userhash):
                    userhash = ""
            except Exception:
                userhash = ""
        if not userhash:
            messagebox.showwarning(
                "没找到 userhash",
                "粘贴的内容里没找到 userhash，请确认复制的是浏览器里的整段 cookie。",
                parent=self,
            )
            return
        try:
            self.userhash = self.client.import_userhash(userhash)
        except Exception as exc:
            messagebox.showerror("设置失败", str(exc), parent=self)
            return
        self._shutdown()
        self.destroy()

    # ---------- 后台线程 ----------

    def _worker(self) -> None:
        """起浏览器、连 CDP、等 userhash。只碰 queue/进程/会话，绝不碰 Tk。"""
        try:
            backend = _load_browser_login()
        except Exception:
            self._queue.put(
                ("error", "这个版本里缺少「浏览器登录」组件，请改用「直接粘贴饼干登录」。")
            )
            return
        try:
            info = backend.find_browser()
        except Exception as exc:
            self._queue.put(("error", f"没能找到浏览器：{exc}"))
            return
        if info is None:
            self._queue.put(
                ("error", "没找到 Edge 或 Chrome。请先装一个，或者改用「直接粘贴饼干登录」。")
            )
            return
        if self._stop.is_set():  # 找浏览器的功夫里用户已经取消了，别再多开一个进程
            return
        try:
            profile = backend.user_data_dir(Path(self.settings.config_path).parent)
            # 先登记再 start()：用户可能在启动过程中点取消，主线程得能把它停掉。
            browser = backend.LoginBrowser(
                info, profile, self.settings.proxy or "", timeout=BROWSER_START_TIMEOUT
            )
            self._browser = browser
            browser.start()
            if self._stop.is_set():  # 取消正好落在启动过程中
                # 必须拿局部引用收尾：主线程那次 _release() 已经把 self._browser 清成 None，
                # 里面那个进程当时还没起来、它停不掉，再读登记处就等于放任成一个孤儿进程。
                self._release_browser(browser)
                return
            # 浏览器端点交给 CDPSession，它自己会换成页面标签再握手。
            session = backend.CDPSession(browser.browser_ws_url)
            self._session = session
            session.connect()
            if self._stop.is_set():  # 取消正好落在握手过程中
                self._release_browser(browser)
                return
            # 真机实测：刚起来的那个标签常常还停在 about:blank（起始地址还没落地），
            # 这时页面里的 fetch 会落在别的源上，领饼干那一步会白跑。
            # ensure_login_page 只在「确实不在站内」时才导航，用户已经登进去的页面不会被拽走。
            # 导航失败（比如页面正在被销毁、Page.navigate 报错）不该把整条登录流程掐掉：
            # 后面每一轮轮询都会重新读 URL，用户还是能正常登录。
            try:
                backend.ensure_login_page(session)
            except Exception as exc:  # noqa: BLE001 —— 导航只是尽力而为
                self._queue.put(("note", f"没能把浏览器窗口切到登录页（{exc}），请在窗口里手动打开。"))
        except Exception as exc:
            if not self._stop.is_set():  # 取消时后台报的错没人看，不必再刷界面
                self._queue.put(("error", f"打开浏览器失败：{exc}"))
            return
        self._queue.put(
            ("ready", "浏览器已经打开了：请在里面登录 X 岛用户系统。登录成功后这里会自动关掉。")
        )

        deadline = time.monotonic() + BROWSER_LOGIN_TIMEOUT
        next_leaf = time.monotonic() + BROWSER_LEAF_SECONDS
        while not self._stop.is_set():
            process = browser.process  # 用户自己把浏览器窗口关掉时要能察觉
            if process is not None and process.poll() is not None:
                self._queue.put(("browser_closed", None))
                return
            try:
                value = self._read_userhash(backend, session)
                if not value and time.monotonic() >= next_leaf:
                    next_leaf = time.monotonic() + BROWSER_LEAF_SECONDS
                    value = self._try_leaf_cookie(backend, session)
                if value:
                    self._queue.put(("ok", value))
                    return
            except Exception as exc:
                self._queue.put(("error", f"读取浏览器饼干失败：{exc}"))
                return
            if time.monotonic() >= deadline:
                break
            self._stop.wait(BROWSER_POLL_SECONDS)
        if not self._stop.is_set():
            self._queue.put(
                (
                    "error",
                    f"等了 {BROWSER_LOGIN_TIMEOUT / 60:.0f} 分钟还没看到登录成功。"
                    "请确认浏览器窗口里已经登录完成，再点「重新打开浏览器」试一次。",
                )
            )

    @staticmethod
    def _read_userhash(backend, session) -> str | None:
        """从 CDP 读到的饼干里挑出 userhash；没有就返回 None。"""
        for cookie in session.read_cookies():
            if not isinstance(cookie, dict) or cookie.get("name") != "userhash":
                continue
            value = str(cookie.get("value") or "").strip()
            if value and backend.looks_like_userhash(value):
                return value
        return None

    @staticmethod
    def _try_leaf_cookie(backend, session) -> str | None:
        """兜底：登录了却没看到 userhash，就去饼干页领一块新的。

        这一步失败是常态（人还没登录完、页面正在跳转），所以异常一律当「还没好」，
        不往界面上报错 —— 它本来就是兜底路径，主路径是上面的 read_cookies。
        """
        try:
            value = backend.apply_leaf_cookie(session)
        except Exception:
            return None
        value = str(value or "").strip()
        if value and backend.looks_like_userhash(value):
            return value
        return None


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

        outer = ttk.Frame(self, padding=(theme.gap(4), theme.gap(3)))
        outer.pack(fill="both", expand=True)

        # ① 网络
        net_card = Card(outer)
        net_card.pack(fill="x")
        SectionHeading(net_card.body, "网络").pack(fill="x")

        net = ttk.Frame(net_card.body, style="Card.TFrame")
        net.pack(fill="x", pady=(theme.gap(2), 0))
        net.columnconfigure(1, weight=1)

        ttk.Label(net, text="代理地址", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=theme.gap(1)
        )
        self.proxy_var = tk.StringVar(value=settings.proxy)
        ttk.Entry(net, textvariable=self.proxy_var).grid(
            row=0, column=1, sticky="ew", padx=(theme.gap(2), 0), pady=theme.gap(1)
        )
        ttk.Label(
            net,
            text="例如 http://127.0.0.1:7890；留空则读取系统环境变量，仍为空表示直连。",
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, theme.gap(1)))

        # 三个数字项排成一行，省一层高度（小屏上更友好）
        self.timeout_var = tk.StringVar(value=f"{settings.timeout:g}")
        self.retries_var = tk.StringVar(value=str(settings.retries))
        self.throttle_var = tk.StringVar(value=f"{settings.throttle:g}")
        numbers = ttk.Frame(net, style="Card.TFrame")
        numbers.grid(row=2, column=0, columnspan=2, sticky="w", pady=theme.gap(1))
        for column, (text, variable) in enumerate(
            (
                ("请求超时（秒）", self.timeout_var),
                ("失败重试次数", self.retries_var),
                ("请求间隔（秒）", self.throttle_var),
            )
        ):
            group = ttk.Frame(numbers, style="Card.TFrame")
            group.grid(row=0, column=column, sticky="w", padx=(0, theme.gap(3)))
            ttk.Label(group, text=text, style="Card.TLabel").pack(anchor="w")
            ttk.Entry(group, textvariable=variable, width=10).pack(
                anchor="w", pady=(theme.gap(0.5), 0)
            )
        ttk.Label(
            net,
            text="请求过密会被限流（429）；抓很长的串时把间隔调到 0.3～0.5 更稳。",
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w")

        # ② 缓存
        cache_card = Card(outer)
        cache_card.pack(fill="x", pady=(theme.gap(2), 0))
        SectionHeading(cache_card.body, "缓存").pack(fill="x")

        cache = ttk.Frame(cache_card.body, style="Card.TFrame")
        cache.pack(fill="x", pady=(theme.gap(2), 0))
        cache.columnconfigure(1, weight=1)

        self.use_cache_var = tk.BooleanVar(value=settings.use_cache)
        ttk.Checkbutton(
            cache,
            text="启用本地缓存（断点续传、跳过重复下载）",
            variable=self.use_cache_var,
            style="Card.TCheckbutton",
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(cache, text="缓存目录", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(theme.gap(1.5), 0)
        )
        cache_row = ttk.Frame(cache, style="Card.TFrame")
        cache_row.grid(
            row=1, column=1, sticky="ew", padx=(theme.gap(2), 0), pady=(theme.gap(1.5), 0)
        )
        self.cache_var = tk.StringVar(value=settings.cache_dir)
        ttk.Entry(cache_row, textvariable=self.cache_var).pack(side="left", fill="x", expand=True)
        ttk.Button(
            cache_row, text="…", style="Secondary.TButton", width=3, command=self._choose_cache
        ).pack(side="left", padx=(theme.gap(1), 0))
        ttk.Label(
            cache,
            text="留空表示放在导出目录下的 .cache，随导出目录一起迁移。",
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(theme.gap(1), 0))

        # ③ 导出
        export_card = Card(outer)
        export_card.pack(fill="x", pady=(theme.gap(2), 0))
        SectionHeading(export_card.body, "导出").pack(fill="x")

        export = ttk.Frame(export_card.body, style="Card.TFrame")
        export.pack(fill="x", pady=(theme.gap(2), 0))
        export.columnconfigure(1, weight=1)

        ttk.Label(export, text="文件名模板", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=theme.gap(1)
        )
        self.template_var = tk.StringVar(value=settings.filename_template)
        ttk.Entry(export, textvariable=self.template_var).grid(
            row=0, column=1, sticky="ew", padx=(theme.gap(2), 0), pady=theme.gap(1)
        )
        ttk.Label(
            export,
            text=(
                "占位符：{title} 标题、{id} 串号、{date} 导出日期、{po} PO 饼干、{count} 楼层数。\n"
                "例：[{id}] {title}。留空表示用标题，无标题时取第一句话。"
            ),
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, theme.gap(1)))

        ttk.Label(export, text="PDF 浏览器", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=theme.gap(1)
        )
        browser_row = ttk.Frame(export, style="Card.TFrame")
        browser_row.grid(row=2, column=1, sticky="ew", padx=(theme.gap(2), 0), pady=theme.gap(1))
        self.browser_var = tk.StringVar(value=settings.pdf_browser)
        ttk.Entry(browser_row, textvariable=self.browser_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            browser_row, text="…", style="Secondary.TButton", width=3, command=self._choose_browser
        ).pack(side="left", padx=(theme.gap(1), 0))
        ttk.Label(
            export,
            text="导出 PDF 时调用的浏览器（无头模式渲染）。留空表示自动查找 Chrome 或 Edge。",
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(theme.gap(3), 0))
        ttk.Button(buttons, text="取消", style="Secondary.TButton", command=self.destroy).pack(
            side="right", padx=(theme.gap(1), 0)
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

        outer = ttk.Frame(self, padding=(theme.gap(4), theme.gap(3)))
        outer.pack(fill="both", expand=True)

        card = Card(outer)
        card.pack(fill="both", expand=True)
        SectionHeading(card.body, "第一页出现的饼干").pack(fill="x")
        ttk.Label(
            card.body,
            text="勾选后点「应用」，多个饼干会以空格分隔写入筛选框。",
            style="CardMuted.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(theme.gap(1), 0))

        list_frame = ttk.Frame(card.body, style="Card.TFrame")
        list_frame.pack(fill="both", expand=True, pady=(theme.gap(2), 0))
        canvas = tk.Canvas(
            list_frame,
            bg=_PAL.surface,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=_PAL.surface)
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
                inner,
                text=label,
                variable=var,
                bg=_PAL.surface,
                activebackground=_PAL.surface,
                fg=TEXT,
                activeforeground=TEXT,
                selectcolor=_PAL.surface,
                highlightthickness=0,
                bd=0,
                anchor="w",
                font=SMALL_FONT,
            ).pack(anchor="w", padx=theme.gap(2), pady=theme.gap(0.5), fill="x")
            self.vars[cookie] = var

        buttons = ttk.Frame(card.body, style="Card.TFrame")
        buttons.pack(fill="x", pady=(theme.gap(2.5), 0))
        ttk.Button(buttons, text="取消", style="Secondary.TButton", command=self.destroy).pack(
            side="right", padx=(theme.gap(1), 0)
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
        ).pack(side="left", padx=(theme.gap(1), 0))

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

        outer = ttk.Frame(self, padding=(theme.gap(4), theme.gap(2)))
        outer.pack(fill="both", expand=True)

        card = Card(outer)
        card.pack(fill="both", expand=True)
        SectionHeading(card.body, "监控列表").pack(fill="x")
        ttk.Label(
            card.body,
            text="定时检查这些串：有新回复就自动导出；没有新回复只花一次请求。",
            style="CardMuted.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(theme.gap(1), 0))

        list_frame = ttk.Frame(card.body, style="Card.TFrame")
        list_frame.pack(fill="both", expand=True, pady=(theme.gap(2), 0))
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

        controls = ttk.Frame(card.body, style="Card.TFrame")
        controls.pack(fill="x", pady=(theme.gap(2), 0))
        ttk.Button(
            controls,
            text="添加输入框中的串",
            style="Secondary.TButton",
            command=self._add_from_input,
        ).pack(side="left")
        ttk.Button(
            controls, text="移除选中", style="Secondary.TButton", command=self._remove_selected
        ).pack(side="left", padx=(theme.gap(1.5), 0))
        ttk.Label(controls, text="检查间隔（秒）", style="CardMuted.TLabel").pack(
            side="left", padx=(theme.gap(3.5), theme.gap(1))
        )
        self.interval_var = tk.StringVar(value=str(int(self.app.settings.watch_interval)))
        ttk.Entry(controls, textvariable=self.interval_var, width=7).pack(side="left")
        self.verify_var = tk.BooleanVar(value=self.app.settings.verify_cached)
        ttk.Checkbutton(
            controls,
            text="校验老楼层改动（更准，每轮多一次请求）",
            variable=self.verify_var,
            style="Card.TCheckbutton",
        ).pack(side="left", padx=(theme.gap(2.5), 0))

        actions = ttk.Frame(card.body, style="Card.TFrame")
        actions.pack(fill="x", pady=(theme.gap(2), 0))
        self.toggle_button = ttk.Button(actions, text="开始监控", command=self._toggle)
        self.toggle_button.pack(side="left")
        self.status_var = tk.StringVar()
        ttk.Label(actions, textvariable=self.status_var, style="CardMuted.TLabel").pack(
            side="left", padx=(theme.gap(3), 0)
        )
        ttk.Button(
            actions, text="立即检查一次", style="Secondary.TButton", command=self._check_now
        ).pack(side="right")
        ttk.Button(actions, text="关闭", style="Secondary.TButton", command=self.destroy).pack(
            side="right", padx=(0, theme.gap(1.5))
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
        resolve_fonts(root)
        self.settings = AppSettings.load()
        # 配色要在画任何控件之前定下来：ttk 样式归 apply_theme，
        # 模块级的颜色别名归 refresh_colors（自绘卡片/文本域走的是后者）。
        self._theme_name = theme.palette(self.settings.theme_name).name
        self.style = apply_theme(root, theme.palette(self._theme_name))
        refresh_colors()
        # 字体别名同样要在第一笔画下去之前定下来：模块级 MONO_FONT 等是
        # 导入期算的（那时还没窗口，探不到真实字族），要等有了 root 才准。
        refresh_fonts(root)
        root.configure(bg=BG)
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
        # 本次导出真正用的目录（prepare_export_dir 的结果），收尾提示要用
        self._output_dir = ""
        # 用户在「更改…」里亲手选过导出目录吗？选过就尊重他的选择（只报错、不换地方），
        # 没选过（用配置里的默认值）才允许导出时自动改到能写的位置。
        # 放在进入主循环之前赋值，start() 永远晚于它执行。
        self._dir_pinned = False
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
        # 桌面通知在主线程（_poll_watch）里发，这样通知失败也不会影响监控线程
        self._watch_notifier: Notifier | None = None
        self._watch_dialog: WatchDialog | None = None
        # 切换主题时旧控件会被销毁，这里记住本次实例出来的控件，便于重建
        self._widget_roots: list[tk.Misc] = []

        root.title("X岛串导出")
        root.geometry("1060x760")
        root.minsize(940, 680)

        self._build_ui()
        self.refresh_watch_status()
        self.refresh_cache_info()
        self.log("就绪。填入串网址后点「开始导出」。")

    # ---------- 主题 ----------

    def _remember_theme_widget(self, widget: tk.Misc) -> None:
        self._widget_roots.append(widget)

    def _build_ui(self, log_text: str = "") -> None:
        """按当前配色把整个界面画一遍（切换主题时也会再调一次）。

        颜色别名在模块级别，控件在创建时就把颜色烘进去了，所以换肤不能
        只改 ``theme.PALETTE``：必须销毁旧控件、按新配色重画。用户填过的
        串网址、日志正文等状态存在 ``StringVar``/文本里，由调用方传进来。
        """
        outer = ttk.Frame(self.root, padding=(theme.gap(4), theme.gap(3)))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, minsize=theme.SETTINGS_COLUMN_WIDTH)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)
        self._remember_theme_widget(outer)

        self._build_header(outer)
        self._build_settings_column(outer)
        self._build_activity_card(outer)
        if log_text:
            # 日志框建出来就是 disabled（只读），直接 insert 什么也写不进去，
            # 所以先开门、写完再关上 —— 切换主题时旧日志才不会丢。
            self.log_text.config(state="normal")
            self.log_text.insert("1.0", log_text)
            self.log_text.config(state="disabled")
        self.sync_theme_selector()

    def switch_theme(self, name: str, *, persist: bool = True) -> bool:
        """换配色并立刻重画界面。返回是否真的换了（同一个主题返回 False）。

        导出/监控正在跑时**不要**调它：工作线程还在往旧控件投递消息，中途重建
        界面容易丢一条进度或弹一半的对话框（``_poll_export`` 还在按旧控件刷进度）。
        这条判断在 :meth:`_on_theme_selected` 里做（那里能提示用户）。
        """
        palette = theme.palette(name)
        if palette.name == self._theme_name:
            self.sync_theme_selector()
            return False

        self._theme_name = palette.name
        self.settings.theme_name = palette.name
        if persist:
            self.persist_prefs()

        keep_log = ""
        keep_urls = ""
        try:
            keep_log = self.log_text.get("1.0", "end-1c")
        except (AttributeError, tk.TclError):  # pragma: no cover - 没建起来过
            pass
        try:
            # 重建会把输入框清空，用户手打的串网址得留下来
            keep_urls = self.urls_text.get("1.0", "end-1c")
        except (AttributeError, tk.TclError):  # pragma: no cover - 没建起来过
            pass

        theme.set_palette(palette)
        self.style = apply_theme(self.root, palette)
        # 旧配色对象要在 refresh_colors() 之前拿到：那一步会把 _PAL 换成新的，
        # 之后再拿"旧色"就只能拿到新色，对话框迁移会整个失效。
        previous = _PAL
        refresh_colors()
        self.root.configure(bg=BG)
        # 已打开的对话框不在重建范围内，单独迁移它们的颜色
        refresh_open_dialogs(self.root, previous, palette)

        for widget in self._widget_roots:
            try:
                widget.destroy()
            except tk.TclError:  # pragma: no cover - 已被销毁
                pass
        self._widget_roots.clear()

        self._build_ui(keep_log)
        if keep_urls:
            self.urls_text.insert("1.0", keep_urls)
        self.log(f"界面配色已切换为{'深色' if palette.name == 'dark' else '浅色'}。")
        return True

    def sync_theme_selector(self) -> None:
        """把下拉框的显示值对齐当前主题（切换后要回写，否则显示不同步）。"""
        var = getattr(self, "_theme_var", None)
        if var is not None and var.get() != self._theme_name:
            var.set(self._theme_name)

    def _on_theme_selected(self, _event: object = None) -> None:
        var = getattr(self, "_theme_var", None)
        if var is None:
            return
        chosen = var.get()
        if chosen == self._theme_name:
            return
        if self._exporting or self._watching:
            messagebox.showinfo(
                "正在忙",
                "导出或监控正在跑，等它结束再换配色吧。\n"
                "（中途换配色会重建界面，进度和日志容易错位。）",
            )
            self.sync_theme_selector()
            return
        self.switch_theme(chosen)

    # ---------- 界面搭建 ----------

    def _build_header(self, parent: ttk.Frame) -> None:
        """顶栏：标题 + 一句话说明 + 状态胶囊 + 三个入口按钮。"""
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, theme.gap(3)))
        header.columnconfigure(0, weight=1)

        title_box = ttk.Frame(header)
        title_box.grid(row=0, column=0, sticky="w")
        ttk.Label(title_box, text="X岛串导出", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_box,
            text="HTML · PDF · TXT · Markdown · EPUB　|　断点续传 · 图片缓存 · 更新监控",
            style="Faint.TLabel",
        ).pack(anchor="w", pady=(theme.gap(0.5), 0))

        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, sticky="e")
        ttk.Button(
            actions, text="登录 / 设置饼干", style="Ghost.TButton", command=self.open_login
        ).pack(side="left")
        ttk.Button(actions, text="设置", style="Ghost.TButton", command=self.open_settings).pack(
            side="left", padx=(theme.gap(0.5), 0)
        )
        ttk.Button(
            actions, text="监控串更新", style="Secondary.TButton", command=self.open_watch
        ).pack(side="left", padx=(theme.gap(0.5), 0))

        # 配色开关：立即生效（重建界面），不忙的时候才允许切
        ttk.Label(actions, text="配色", style="Faint.TLabel").pack(
            side="left", padx=(theme.gap(1.5), theme.gap(0.5))
        )
        self._theme_var = tk.StringVar(value=self._theme_name)
        theme_box = ttk.Combobox(
            actions,
            textvariable=self._theme_var,
            values=[theme.palette(key).name for key in theme.PALETTES],
            state="readonly",
            width=5,
        )
        theme_box.pack(side="left")
        theme_box.bind("<<ComboboxSelected>>", self._on_theme_selected)

        status_row = ttk.Frame(header)
        status_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(theme.gap(2.5), 0))
        self.status_var = tk.StringVar(value="已登录" if self.settings.userhash else "未登录")
        self.status_pill = StatusPill(
            status_row,
            self.status_var.get(),
            tone="ok" if self.settings.userhash else "muted",
        )
        self.status_pill.pack(side="left")
        # 登录状态在弹窗里改，这里不重复造按钮，只留提示
        ttk.Label(
            status_row,
            text="登录后才能在受限版块里看帖；只看公开串可以不登录。",
            style="Faint.TLabel",
        ).pack(side="left", padx=(theme.gap(2), 0))

        self.watch_status_var = tk.StringVar(value="")
        self.watch_pill = StatusPill(status_row, "", tone="accent")
        ttk.Label(status_row, textvariable=self.watch_status_var, style="Faint.TLabel").pack(
            side="right"
        )

    def _build_settings_column(self, parent: ttk.Frame) -> None:
        """左栏：输入串 → 抓取选项 → 导出目录 → 开始按钮 → 进度。

        窗口太矮时左栏可以滚动（小屏笔记本上 1366x768 很常见），
        所以内容放在 :meth:`_scroll_area` 里，而不是直接挂在 ``parent`` 上。
        """
        column = ttk.Frame(parent, width=theme.SETTINGS_COLUMN_WIDTH)
        column.grid(row=1, column=0, sticky="nsew", padx=(0, theme.gap(2)))
        column.pack_propagate(False)  # 见 theme.SETTINGS_COLUMN_WIDTH 的注释
        column.columnconfigure(0, weight=1)
        column.rowconfigure(0, weight=1)

        inner = self._scroll_area(column)
        inner.columnconfigure(0, weight=1)

        # ① 输入串
        input_card = Card(inner)
        input_card.grid(row=0, column=0, sticky="ew")
        SectionHeading(input_card.body, "① 输入串网址", "每行一个，可一次粘贴多个串").pack(
            fill="x"
        )
        self.urls_text = tk.Text(
            input_card.body,
            height=3,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=BODY_FONT,
            padx=theme.gap(2),
            pady=theme.gap(1.5),
            bg=_PAL.surface_sunken,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=_PAL.accent_soft,
        )
        self.urls_text.pack(fill="x", pady=(theme.gap(1.25), 0))
        self._add_context_menu(self.urls_text)

        # 抓取范围
        scope_row = ttk.Frame(input_card.body, style="Card.TFrame")
        scope_row.pack(fill="x", pady=(theme.gap(1.5), 0))
        ttk.Label(scope_row, text="抓取范围", style="Card.TLabel").pack(side="left")
        self.scope_var = tk.StringVar(value=self.settings.scope or "all")
        ttk.Radiobutton(
            scope_row, text="所有人发言", variable=self.scope_var, value="all",
            style="Card.TRadiobutton",
        ).pack(side="left", padx=(theme.gap(2), theme.gap(1.5)))
        ttk.Radiobutton(
            scope_row, text="只抓 PO 发言", variable=self.scope_var, value="po",
            style="Card.TRadiobutton",
        ).pack(side="left")

        # 只看指定饼干
        cookie_row = ttk.Frame(input_card.body, style="Card.TFrame")
        cookie_row.pack(fill="x", pady=(theme.gap(1), 0))
        ttk.Label(cookie_row, text="只看指定饼干", style="Card.TLabel").pack(side="left")
        self.hashes_var = tk.StringVar(value=self.settings.include_hashes)
        ttk.Entry(cookie_row, textvariable=self.hashes_var).pack(
            side="left", fill="x", expand=True, padx=(theme.gap(2), theme.gap(1))
        )
        self.pick_cookie_button = ttk.Button(
            cookie_row,
            text="从串中挑选…",
            style="Secondary.TButton",
            command=self.pick_cookies,
        )
        self.pick_cookie_button.pack(side="left")
        self.hash_hint = ttk.Label(
            input_card.body,
            text="留空 = 不筛选；多个用空格或逗号分隔。",
            style="CardFaint.TLabel",
            justify="left",
        )
        self.hash_hint.pack(anchor="w", fill="x", pady=(theme.gap(0.5), 0))
        self.hash_hint.bind(
            "<Configure>",
            lambda e: e.widget.configure(wraplength=max(200, e.width - theme.gap(1))),
        )

        ttk.Separator(input_card.body, orient="horizontal").pack(
            fill="x", pady=(theme.gap(1), theme.gap(1))
        )

        # 导出格式 + 两个开关（并成一行，少占一层高度）
        format_row = ttk.Frame(input_card.body, style="Card.TFrame")
        format_row.pack(fill="x")
        ttk.Label(format_row, text="导出格式", style="Card.TLabel").pack(side="left")
        self._format_keys = list(EXPORTERS.keys())
        self.format_box = ttk.Combobox(
            format_row,
            state="readonly",
            width=18,
            values=[name for name, _, _ in EXPORTERS.values()],
        )
        self.format_box.pack(side="left", padx=(theme.gap(2), theme.gap(1)))
        self.format_box.current(
            self._format_keys.index(self.settings.format_key)
            if self.settings.format_key in self._format_keys
            else 0
        )
        self.format_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_image_mode())

        option_row = ttk.Frame(input_card.body, style="Card.TFrame")
        option_row.pack(fill="x", pady=(theme.gap(1), 0))
        self.use_cache_var = tk.BooleanVar(value=self.settings.use_cache)
        ttk.Checkbutton(
            option_row,
            text="本地缓存（断点续传）",
            variable=self.use_cache_var,
            style="Card.TCheckbutton",
        ).pack(side="left")
        self.notify_var = tk.BooleanVar(value=self.settings.notify)
        ttk.Checkbutton(
            option_row,
            text="监控时弹通知",
            variable=self.notify_var,
            style="Card.TCheckbutton",
        ).pack(side="left", padx=(theme.gap(2), 0))

        # EPUB 的图片处理方式（只在选 EPUB 时可用；非 EPUB 时整行收起来）
        self.image_mode_frame = ttk.Frame(input_card.body, style="Card.TFrame")
        ttk.Label(self.image_mode_frame, text="EPUB 图片", style="Card.TLabel").pack(side="left")
        self._image_mode_keys = ["embed", "url", "drop"]
        self.image_mode_box = ttk.Combobox(
            self.image_mode_frame,
            state="readonly",
            width=26,
            values=["内嵌到文件（体积大，离线可看）", "仅保留图片链接（体积小）", "丢弃图片"],
        )
        self.image_mode_box.pack(side="left", padx=(theme.gap(2), 0))
        self.image_mode_box.current(
            self._image_mode_keys.index(self.settings.image_mode)
            if self.settings.image_mode in self._image_mode_keys
            else 0
        )
        if self.current_format() == "epub":
            self.image_mode_frame.pack(fill="x", pady=(theme.gap(1), 0))

        # ② 导出目录
        folder_card = Card(inner)
        folder_card.grid(row=1, column=0, sticky="ew", pady=(theme.gap(2), 0))
        SectionHeading(folder_card.body, "② 导出目录", "成品与日志都写在这里").pack(fill="x")
        self.output_var = tk.StringVar(
            value=self.settings.output_dir or str(Path.home() / "Documents" / "X岛备份")
        )
        folder_row = ttk.Frame(folder_card.body, style="Card.TFrame")
        folder_row.pack(fill="x", pady=(theme.gap(1.25), 0))
        ttk.Entry(folder_row, textvariable=self.output_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            folder_row, text="更改…", style="Secondary.TButton", command=self.choose_folder
        ).pack(side="left", padx=(theme.gap(1), 0))

        cache_row = ttk.Frame(folder_card.body, style="Card.TFrame")
        cache_row.pack(fill="x", pady=(theme.gap(1), 0))
        self.cache_info_var = tk.StringVar(value="")
        ttk.Label(
            cache_row, textvariable=self.cache_info_var, style="CardFaint.TLabel"
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            cache_row, text="打开目录", style="Secondary.TButton", command=self.open_folder
        ).pack(side="right")
        ttk.Button(
            cache_row,
            text="清空缓存",
            style="Ghost.TButton",
            command=self.clear_cache,
        ).pack(side="right", padx=(0, theme.gap(1)))

        # 主操作按钮 + 进度（滚动区之外，永远看得见）
        footer = ttk.Frame(column)
        footer.grid(row=1, column=0, sticky="ew", pady=(theme.gap(2), 0))
        button_row = ttk.Frame(footer)
        button_row.pack(fill="x")
        self.start_button = ttk.Button(button_row, text="开始导出", command=self.start)
        self.start_button.pack(side="left", fill="x", expand=True)
        self.retry_button = ttk.Button(
            button_row,
            text="重试失败项",
            style="Secondary.TButton",
            command=self.retry_failed,
            state="disabled",
        )
        self.retry_button.pack(side="left", padx=(theme.gap(1), 0))

        self.progress_var = tk.StringVar(value="等待开始")
        ttk.Label(footer, textvariable=self.progress_var, style="Faint.TLabel").pack(
            anchor="w", pady=(theme.gap(2), theme.gap(1))
        )
        self.progress_bar = ModernProgress(footer)
        self.progress_bar.pack(fill="x")

    def _scroll_area(self, parent: ttk.Frame) -> ttk.Frame:
        """造一个"装不下就滚动"的纵向容器，返回可往里塞内容的内层 Frame。

        用法：``inner = self._scroll_area(column)``，然后往 ``inner`` 里 grid 内容。

        画布高度写死（不跟着内容走）是关键：``tk.Canvas`` 的"请求高度"会跟着
        内嵌窗口的实际高度跑，一旦跟着内容长高，父容器就会被撑成内容那么高，
        左边栏不再滚动、右边日志栏还会被挤成一条缝。高度固定后行为才可预期。
        """
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(
            holder,
            bg=BG,
            highlightthickness=0,
            borderwidth=0,
            takefocus=False,
            width=theme.SETTINGS_COLUMN_WIDTH,
            height=theme.SETTINGS_VIEWPORT_HEIGHT,
        )
        canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        window = canvas.create_window(0, 0, anchor="nw", window=inner)
        canvas.configure(yscrollcommand=scroll.set)
        self._scroll_needed = False

        def sync(_event=None) -> None:
            width = canvas.winfo_width()
            height = canvas.winfo_height()
            content = inner.winfo_reqheight()
            if width > 1:
                canvas.itemconfigure(window, width=width)
            if height > 1 and content < height:
                canvas.itemconfigure(window, height=height)
            canvas.configure(scrollregion=canvas.bbox("all"))
            needed = height > 1 and content > height + 1
            if needed != self._scroll_needed:
                self._scroll_needed = needed
                if needed:
                    scroll.pack(side="right", fill="y", padx=(theme.gap(1), 0))
                else:
                    scroll.pack_forget()

        def on_wheel(event: tk.Event) -> None:
            if self._scroll_needed:
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        inner.bind("<Configure>", sync)
        canvas.bind("<Configure>", sync)
        canvas.bind("<MouseWheel>", on_wheel)
        return inner

    def _build_activity_card(self, parent: ttk.Frame) -> None:
        """右栏：运行日志卡片，高度随窗口自适应。"""
        card = Card(parent)
        card.grid(row=1, column=1, sticky="nsew")
        card.pack_propagate(False)

        ttk.Label(card.body, text="运行日志", style="CardHeading.TLabel").pack(anchor="w")
        log_head = ttk.Frame(card.body, style="Card.TFrame")
        log_head.pack(fill="x", pady=(theme.gap(0.5), theme.gap(1.5)))
        ttk.Label(
            log_head, text="抓取与导出的每一步都会记在这里", style="CardFaint.TLabel"
        ).pack(side="left")
        ttk.Button(log_head, text="保存…", style="Ghost.TButton", command=self.save_log).pack(
            side="right"
        )
        ttk.Button(log_head, text="清空", style="Ghost.TButton", command=self.clear_log).pack(
            side="right", padx=(0, theme.gap(0.5))
        )

        self.log_text = tk.Text(
            card.body,
            height=18,
            state="disabled",
            background=_PAL.surface_sunken,
            fg=TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=MONO_FONT,
            padx=theme.gap(2),
            pady=theme.gap(1.5),
            selectbackground=_PAL.accent_soft,
        )
        self.log_text.pack(fill="both", expand=True)
        self._add_context_menu(self.log_text)
        card.bind("<Configure>", lambda _e: self._fit_log_height())

    def _fit_log_height(self) -> None:
        """日志框按卡片实际高度决定显示多少行（不跟着内容无限长高）。"""
        try:
            available = self.log_text.master.winfo_height() - 64
            linespace = int(self.log_text.tk.call("font", "metrics", MONO_FONT, "-linespace"))
        except (tk.TclError, TypeError, ValueError):
            return
        if available < 80 or linespace <= 0:
            return
        lines = max(10, min(40, available // linespace))
        if int(self.log_text.cget("height")) != lines:
            self.log_text.configure(height=lines)

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
        """图片模式只对 EPUB 有意义：选 EPUB 时露出来，其它格式直接收起来。"""
        enabled = self.current_format() == "epub"
        self.image_mode_box.configure(state="readonly" if enabled else "disabled")
        if not hasattr(self, "_scroll_needed"):
            return  # 界面还没搭完（构造过程中会调用一次）
        if enabled:
            self.image_mode_frame.pack(fill="x", pady=(theme.gap(1), 0))
        else:
            self.image_mode_frame.pack_forget()

    def parse_hashes(self) -> list[str]:
        return self.settings.parse_hashes(self.hashes_var.get())

    def persist_prefs(self) -> None:
        self.settings.output_dir = self.output_var.get().strip() or None
        self.settings.scope = self.scope_var.get()
        self.settings.format_key = self.current_format()
        self.settings.include_hashes = self.hashes_var.get().strip()
        self.settings.use_cache = bool(self.use_cache_var.get())
        self.settings.notify = bool(self.notify_var.get())
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
            dialog = LoginDialog(self.root, self.client, self.settings)
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
            self._dir_pinned = True
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

    def _resolved_output_dir(self) -> str:
        """本次导出实际用的目录（收尾提示用）。

        ``start()`` 定下目录后存在 ``self._output_dir``；这里兜底读界面上的
        输入框，任何情况下都返回一个字符串 —— 收尾函数绝不能因为拿不到目录
        而抛异常（看到的会是「界面出错」而不是导出结果）。
        """
        resolved = getattr(self, "_output_dir", "")
        if resolved:
            return str(resolved)
        try:
            return self.output_var.get().strip()
        except (AttributeError, tk.TclError):  # pragma: no cover - 界面还没建好
            return ""

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

    def prepare_export_dir(self) -> str | None:
        """定下这一趟的导出目录；拦下不可用的情况时返回 None。

        单独抽出来是为了能直接测（``start()`` 会真的开线程、还会把偏好写进配置）。
        """
        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请选择导出目录。")
            return None

        # 这个目录连"建出来"都做不到时才拦下（例如路径指向不存在又无权创建的盘）。
        # 只写不进探针文件不算数 —— 那正是 v0.5.0 把用户挡在门外的那次教训。
        try:
            ensure_writable(output_dir)
        except OutputDirNotWritable as exc:
            messagebox.showerror("导出目录不可用", str(exc))
            return None

        # 实测过：抓取全成功、写文件时权限拒绝，整趟白跑（Windows 的受控文件夹访问
        # 默认保护桌面/文档）。所以先挑一个真写得进去的目录 —— 挑不到原位就自动换。
        # 命令行/界面上显式写死的目录不换地方，只把问题说清楚。
        allow_fallback = not self._dir_pinned
        choice = choose_writable_dir(output_dir, kind="导出", allow_fallback=allow_fallback)
        output_dir = str(choice.path)
        if choice.fallback:
            # 兜底位置可能是第一次用，先建出来（探测只确认"写得进去"，不负责留下目录）。
            try:
                Path(output_dir).mkdir(parents=True, exist_ok=True)
            except OSError:  # pragma: no cover - 探测刚说过能写，走到这里基本是磁盘满
                pass
            # 只改界面上显示的目录，不动用户的设置：这一趟写兜底位置，下次仍按他设的试。
            self.output_var.set(output_dir)
        for note in choice.notes:
            self.log(note)
        if choice.notes and choice.fallback:
            messagebox.showwarning(
                "导出目录已自动改到能写的位置",
                "\n\n".join(choice.notes)
                + f"\n\n本次的成品都会放在：\n{output_dir}\n\n"
                "想固定用别的位置：把导出目录换成不受保护的地方（例如自建的文件夹），"
                "或在安全软件里把本程序加入白名单。",
            )
        return output_dir

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

        resolved = self.prepare_export_dir()
        if resolved is None:
            return
        # 导出目录存到实例上：_finish()（结束汇总/弹窗）跑在主线程，
        # 拿不到 start() 里的局部变量。曾经这里只写局部变量，导致
        # 一整趟导出结束后弹「NameError: name 'output_dir' is not defined」
        # （2026-09-30 实测：抓取失败后 _finish 一跑就崩）。
        self._output_dir = resolved
        output_dir = resolved

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
        self.log(f"导出目录：{output_dir}")

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
            # 抓下来但明确不完整的串（缺页/撞上页数上限）：产物已经写出去了，
            # 所以要单独收集，在结束时的汇总里提醒用户"这几份别当全的用"。
            incomplete: list[str] = []
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
                    if getattr(result, "retry_note", ""):
                        # 缺页/撞上页数上限这类"产物不完整"要在日志里留痕：
                        # 旧版本是静默少抓一大截，用户拿到半份成品还以为抓完了。
                        self._export_queue.put(
                            (
                                "log",
                                f"[{index}/{len(urls)}] 注意：{result.retry_note}",
                            )
                        )
                        incomplete.append(f"{thread.thread_id}：{result.retry_note}")
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
                        ("log", f"[{index}/{len(urls)}] 失败：{describe_export_failure(exc)}")
                    )

            self._export_queue.put(
                ("done", (succeeded, len(urls), failed, time.time() - started, incomplete))
            )

        def worker() -> None:
            # 这个线程里任何漏出来的异常都会让打包版弹出「Unhandled exception in script」
            # 对话框（见到过一次），所以在这里兜底，转成界面上的一条日志 + 结束事件。
            try:
                worker_body()
            except Exception as exc:  # noqa: BLE001 —— 兜住线程里的一切
                self._export_queue.put(
                    ("log", f"导出没能开始/继续：{type(exc).__name__}: {exc}")
                )
                self._export_queue.put(
                    ("done", (0, len(urls), list(urls), 0.0, []))
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

    def _finish(
        self,
        succeeded: int,
        total: int,
        failed: list[str],
        elapsed: float,
        incomplete: list[str] | None = None,
    ) -> None:
        self._exporting = False
        self._last_failed = list(failed)
        # 汇总弹窗里要报"文件在哪"。这个值由 start() 落在实例上，这里再兜一层：
        # 万一哪条路径漏了赋值，也只丢一句话，不能让整个收尾崩掉（曾经的 NameError
        # 就是在这里弹出来的，看到的是「界面出错」而不是导出结果）。
        output_dir = self._resolved_output_dir()
        self.progress_bar.stop()
        self.progress_bar.set_value(succeeded, total)
        self.start_button.config(state="normal")
        self.retry_button.config(state="normal" if failed else "disabled")
        self.progress_var.set(f"完成：成功 {succeeded} / {total} 个串，用时 {elapsed:.0f} 秒")
        self.log(f"全部处理完毕：成功 {succeeded} / {total} 个，用时 {elapsed:.1f} 秒。")
        if failed:
            self.log("失败清单：" + "、".join(failed))
        self.refresh_cache_info()
        notes = [line for line in (incomplete or []) if line]
        if notes:
            # 文件是写出去了，但内容是缺的 —— 只提示"失败"会漏掉这种情况，
            # 日志里的那一条也容易划过去，这里再强调一次。
            self.log(f"有 {len(notes)} 个串没能抓全：")
            for line in notes:
                self.log(f"    {line}")
        if failed:
            messagebox.showwarning(
                "部分失败",
                f"成功 {succeeded} / {total} 个串。\n\n失败 {len(failed)} 个，原因见日志区，"
                f"可点「重试失败项」重跑。\n\n成功的文件在：\n{output_dir}",
            )
        elif notes:
            messagebox.showwarning(
                "有串没抓全",
                f"成功导出 {succeeded} / {total} 个串，但其中 {len(notes)} 个只抓到了"
                "一部分（多半是网络中断或接口限流），成品里缺页。\n\n"
                + "\n".join(notes[:5])
                + ("\n…" if len(notes) > 5 else "")
                + "\n\n再导一次通常就能补齐：已经抓下来的页都存进缓存了，"
                "重跑只会补缺的那几页。\n\n文件在：\n"
                f"{output_dir}",
            )
        else:
            messagebox.showinfo(
                "完成", f"成功导出 {succeeded} / {total} 个串。\n\n文件在：\n{output_dir}"
            )

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

        notifier = None
        if self.notify_var.get():
            notifier = Notifier(min_interval=self.settings.notify_interval)
            if not notifier.available():
                self.log("提示：当前环境发不出桌面通知，更新只会写进这份日志。")
        self._watch_notifier = notifier

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
                    notifier=notifier,
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
        self._watch_notifier = None
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
            if notify_result(self._watch_notifier, item):
                self.log(f"    ↳ 已弹桌面通知（No.{target.label}）")
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
