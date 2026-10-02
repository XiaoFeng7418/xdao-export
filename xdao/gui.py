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

import os
import queue
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .cache import CachedThreadFetcher, resolve_cache_dir
from .client import XdaoClient, XdaoError
from .exporters import EXPORTERS, ThreadData, create_exporter
from .exporters._shared import OutputDirNotWritable, choose_writable_dir, ensure_writable
from .fetcher import parse_thread_id
from .notifications import Notifier
from . import pdf_opts, theme, watch_list
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


def wrap_to_width(label: ttk.Label, *, minimum: int = 200) -> None:
    """让一段说明文字跟着窗口宽度换行，而不是整句摊开等着被裁掉。

    说明文字不设 ``wraplength`` 时，控件会按整句的自然宽度去申请空间：
    窗口一窄，右边就直接被裁掉（真机截图里「环境自检」的介绍只显示到
    「「试浏览器」」）。绑到控件自己的 ``<Configure>`` 上，每拿到新宽度
    就重设一次换行宽度；``minimum`` 是窗口被拖到极窄时的兜底，免得
    一行只剩一两个字。

    注意：调用的地方要 ``pack(fill="x")``（或 grid ``sticky="ew"``），
    让控件的实际宽度由容器决定；否则"改 wraplength → 请求宽度变 → 宽度又变"
    会来回抖。
    """
    label.bind(
        "<Configure>",
        lambda event: event.widget.configure(
            wraplength=max(minimum, event.width - theme.gap(1))
        ),
    )


def fold_buttons_when_narrow(
    row: ttk.Frame,
    primary: list[ttk.Button],
    secondary: list[ttk.Button],
    *,
    gap_steps: float = 1.5,
) -> None:
    """一行放不下时，把次要按钮折到第二行，别让窗口把两头切掉。

    「环境自检」最小宽度 520，那排五个按钮加起来要 559 px：不折行的话
    pack 会把先放的按钮撑满、后面的被挤到窗口外面（真机截图里「重新自检」
    和「复制结果」都只露半截）。这里给容器加一行 ``bottom``，窗口够宽时
    两组按钮在同一行（``secondary`` 靠右），不够宽时后者整组落到第二行。
    """
    gap = theme.gap(gap_steps)
    top = ttk.Frame(row, style="Card.TFrame")
    bottom = ttk.Frame(row, style="Card.TFrame")
    top.pack(fill="x")
    state: dict[str, bool | None] = {"narrow": None}

    def place(narrow: bool) -> None:
        if state["narrow"] == narrow:
            return
        state["narrow"] = narrow
        for button in (*primary, *secondary):
            button.pack_forget()
        # 主按钮永远待在第一行（窄的时候第二行只放次要按钮，免得整排
        # 一起挪到下面、第一行空着）。
        for index, button in enumerate(primary):
            button.pack(in_=top, side="left", padx=(gap if index else 0, 0))
        target = bottom if narrow else top
        for index, button in enumerate(secondary):
            button.pack(
                in_=target, side="right", padx=(gap, 0) if index == 0 else (0, gap)
            )
        if narrow:
            bottom.pack(fill="x", pady=(gap, 0))
        else:
            bottom.pack_forget()

    def on_configure(event: "tk.Event[tk.Misc]") -> None:
        needed = gap * (len(primary) + len(secondary) - 1) + sum(
            button.winfo_reqwidth() for button in (*primary, *secondary)
        )
        place(event.width < needed)

    place(False)
    row.bind("<Configure>", on_configure)


# ---------- 直接粘贴饼干登录 ----------

# 「为什么不用你自己浏览器的数据」与「去哪儿复制」两段话。放模块级是为了能用例
# 直接断言它讲清了没有 —— 这两句是这一版真正要交付的东西。
PASTE_WHY = (
    "程序不会去翻你自己浏览器的数据 —— 插件、保存的密码、登录状态都不碰。"
    "所以这一步是「你替它抄一份」：抄的只是一个 X 岛的登录饼干，抄完就归程序自己管。"
)
PASTE_STEPS = (
    "在你平时用的那个浏览器里（已经登录过 X 岛的）：\n"
    "① 按 F12 打开开发者工具；\n"
    "② 选「应用程序 / Application」→ 左边「Cookie」→ 点 https://www.nmbxd1.com ；\n"
    "③ 找到 userhash 那一行，复制它的「值」（整行 userhash=… 也行）。\n"
    "把复制到的内容粘到下面。整段 cookie 也可以，程序会自己把 userhash 摘出来。"
)


class PasteCookieDialog(tk.Toplevel):
    """把浏览器里的 cookie（或 userhash 值）粘进来，交给登录流程。

    为什么要自己写一个窗口：``simpledialog.askstring`` 的提示文字不换行，而这里
    得说清三件事 —— 为什么不去读你自己浏览器的数据、去哪儿复制、粘什么算数。
    一句挤在一行里，用户看一半就放弃了。
    """

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.configure(bg=BG)
        #: 摘出来的 userhash；取消或没粘东西时是 None。
        self.userhash: str | None = None
        self.title("直接粘贴饼干登录")
        self.transient(master)
        self.minsize(560, 430)

        outer = ttk.Frame(self, padding=(theme.gap(4), theme.gap(3)))
        outer.pack(fill="both", expand=True)

        card = Card(outer)
        card.pack(fill="both", expand=True)
        SectionHeading(card.body, "直接粘贴饼干登录").pack(fill="x")

        # 两段说明都给保守的初始 wraplength：不给的话 Card 会按整句宽度把窗口
        # 撑到屏幕外；给了再让 wrap_to_width 跟着窗口走（见 tests/test_window.py）。
        why = tk.Label(
            card.body,
            text=PASTE_WHY,
            bg=CARD,
            fg=MUTED,
            font=SMALL_FONT,
            justify="left",
            anchor="w",
            wraplength=theme.gap(120),
        )
        why.pack(fill="x", pady=(theme.gap(2), 0))
        wrap_to_width(why, minimum=240)

        steps = tk.Label(
            card.body,
            text=PASTE_STEPS,
            bg=CARD,
            fg=TEXT,
            font=SMALL_FONT,
            justify="left",
            anchor="w",
            wraplength=theme.gap(120),
        )
        steps.pack(fill="x", pady=(theme.gap(2), 0))
        wrap_to_width(steps, minimum=240)

        self.text = tk.Text(
            card.body,
            height=6,
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
        self.text.pack(fill="both", expand=True, pady=(theme.gap(2), 0))
        # 这一步就是「贴进来」，右键菜单里没有「粘贴」说不过去。
        menu = tk.Menu(self.text, tearoff=0)
        menu.add_command(label="粘贴", command=lambda: self.text.event_generate("<<Paste>>"))
        menu.add_command(label="复制", command=lambda: self.text.event_generate("<<Copy>>"))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: self.text.event_generate("<<SelectAll>>"))

        def show_menu(event: tk.Event) -> None:
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        self.text.bind("<Button-3>", show_menu)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(theme.gap(3), 0))
        ttk.Button(buttons, text="取消", style="Secondary.TButton", command=self.destroy).pack(
            side="right", padx=(theme.gap(1), 0)
        )
        ttk.Button(buttons, text="确定", command=self._confirm).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        # 焦点有时要等窗口映射完才给的进去，两头都试一次。
        self.text.focus_set()
        self.after(50, self.text.focus_set)

    def _confirm(self) -> None:
        """把粘贴框里的文字摘成 userhash；摘不出来就原地提示，不关窗口。"""
        # 延迟导入：这个模块只管「从粘贴的文字里摘 userhash」这一件事，
        # 界面本身不依赖它，缺了也不该影响窗口启动。
        try:
            from .browser_login import looks_like_userhash, parse_userhash_input
        except Exception:  # pragma: no cover - 只有裁掉了浏览器组件的版本才会走到
            messagebox.showwarning(
                "这个版本里没有「浏览器登录」组件",
                "没法从粘贴的内容里摘 userhash，请改用其它登录方式。",
                parent=self,
            )
            return

        value = parse_userhash_input(self.text.get("1.0", "end"))
        if not value or not looks_like_userhash(value):
            messagebox.showwarning(
                "没找到 userhash",
                "粘贴的内容里没找到 userhash。\n"
                "请确认复制的是浏览器里的整段 cookie（或至少包含 userhash=… 的那一部分），"
                "或者照上面第 ③ 步只复制 userhash 那一行的「值」。",
                parent=self,
            )
            return
        self.userhash = value
        self.destroy()


def ask_pasted_cookie(master: tk.Misc) -> str | None:
    """弹出「直接粘贴饼干登录」，返回摘出来的 userhash；取消或没粘东西返回 None。"""
    dialog = PasteCookieDialog(master)
    master.wait_window(dialog)
    return dialog.userhash


class LoginDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Tk,
        client: XdaoClient,
        settings: AppSettings | None = None,
        app: "App | None" = None,
    ) -> None:
        super().__init__(master)
        self.configure(bg=BG)
        self.client = client
        # 「用浏览器登录」要知道把浏览器 profile 放哪、要不要走代理，都取自这份设置。
        # 不传就自己读一次配置：老的调用方不必跟着改签名。
        self.settings = settings if settings is not None else AppSettings.load()
        # 主窗口：只用来把「用浏览器登录」失败的原因写进运行日志 —— 那句错误原本只
        # 出现在子窗口的一行小字里，窗口一关就再也找不到了：出了问题回头翻日志，
        # 里面只有自检，没有任何登录相关记录。
        self.app = app
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
        # 本对话框自己 grab_set() 过，而子窗口也要用鼠标：先把 grab 放开，
        # 等子窗口关掉再收回来（跟「用浏览器登录」同一条路子）。
        self.grab_release()
        try:
            userhash = ask_pasted_cookie(self)
        finally:
            try:
                if self.winfo_exists():
                    self.grab_set()
            except tk.TclError:  # pragma: no cover - 窗口已经不可用了
                pass
        if not userhash:
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
        # 这条失败原因原本只写在子窗口的一行小字里（关掉窗口就没了）。用户要报告
        # 问题时贴的是运行日志，所以这里补一笔：日志里那句「浏览器登录没成：…」
        # 才是能拿去查的东西。
        failure = getattr(dialog, "failure", "")
        app = getattr(self, "app", None)
        if failure and app is not None:
            try:
                app.log(f"浏览器登录没成：{failure}")
            except Exception:  # pragma: no cover - 日志写不进去不该影响登录
                pass
        if dialog.userhash:
            self.userhash = dialog.userhash
            self.destroy()


# ---------- 用浏览器登录 ----------

# 后台线程查饼干的间隔（秒）：太密会把 CDP 调用排满，太稀用户登录完要干等。
BROWSER_POLL_SECONDS = 1.5
# 没读到 userhash 时，隔这么久去饼干页领一次（用户登录成功那一刻正好用上）。
BROWSER_LEAF_SECONDS = 5.0
# 「领饼干」最多**导航**几次用户眼前那个标签页（v0.13.17 起这一步要走站点自己的跳转，
# 只能靠导航）。
#
# 为什么要设上限：站点那边「应用」要是反复不成（账号还没领过饼干、写法又变了），
# 每 5 秒把用户的标签页弹到饼干页一次，比多等一会儿糟得多。用完这几次之后只重读
# 饼干罐，剩下的时间留给用户自己登。
#
# 2 → 6（v0.13.21）：一次「应用」现在要等站点自己的倒计时页跳完、再等饼干罐里出现
# userhash，单次成功率高得多；上限太低的话，碰上站点那一跳慢半拍，两次机会会在几
# 十秒里用完，然后就只剩「只读饼干罐」，界面一路等到超时（用户 m29500 的卡法）。
BROWSER_LEAF_NAV_LIMIT = 6
# 次数用完时挂在窗口上的那句话：降级这件事必须在界面上说出来，不能悄悄发生。
BROWSER_LEAF_CAP_HINT = (
    "已经把「自动领饼干」试满 {limit} 次，接下来只读饼干罐（不再动浏览器里的标签页）。"
)
# 导航过之后缓这么久再去下一次：站点「应用」是跳转式的，几秒一轮会把标签页弹成风箱。
BROWSER_LEAF_RETRY_SECONDS = 20.0
# 隔这么久把状态换成「已经等了 N 秒」。
#
# 为什么要有这一句：用户可能在**自己平时的浏览器**里登录，程序看不到，界面又一直
# 停在「请在里面登录…」上一动不动 —— 看起来就像卡死了。换成带秒数的句子，至少
# 让人知道程序在等、等的是哪个窗口里的登录。
BROWSER_PROGRESS_SECONDS = 15.0
# 等用户登录的上限；到点给一句能照做的话，而不是一直转圈。
BROWSER_LOGIN_TIMEOUT = 300.0
# 等浏览器把调试端口写出来的上限（冷启动 + 首次建 profile 会偏慢）。
BROWSER_START_TIMEOUT = 30.0
# 主线程消费消息队列的间隔（毫秒），跟本文件其它对话框保持一致。
BROWSER_UI_POLL_MS = 150
# 饼干读到了、可 X 岛说这个登录不认时往界面上写的话。
# 为什么要单独写一句：那种饼干多半是**上一次登录留在浏览器资料目录里的**，
# 用户看着登录窗口里自己明明登进去了，程序却报「登录成功」并关窗 ——
# 到时候导出全是失败，比当场说清楚难查得多。
BROWSER_STALE_COOKIE_MESSAGE = (
    "浏览器里那块 userhash 饼干 X 岛不认（多半是上一次留下的旧饼干，"
    "也可能这个账号还没在「饼干」页领过）。请在浏览器窗口里重新登录一次；"
    "要是还不行，就去 X 岛用户系统 →「饼干」→ 领取并应用一块饼干。"
)
# 验饼干这步撞上网络问题（不是「不认」）时说的话：请求本身没成，值得再试一次。
BROWSER_VERIFY_FAILED_MESSAGE = "没法确认这个登录还算不算数（{detail}）。稍等一下，程序会接着试。"


def _load_browser_login():
    """惰性导入 :mod:`xdao.browser_login`。

    「用浏览器登录」才需要它（里面是一整套 CDP 客户端），平时不该拖累界面启动；
    惰性导入还能把「模块缺失」变成界面上一句人话，而不是让窗口直接打不开。
    """
    from . import browser_login

    return browser_login


def _find_login_browser(settings: AppSettings | None = None) -> object:
    """按界面上那个「PDF 浏览器」决定用哪个浏览器登录，找不到返回 ``None``。

    留空时 :func:`find_browser` 自己会先认系统默认浏览器（v0.13.3 起），
    再按 Edge → Chrome 的顺序找；填了路径就用填的那个。
    """
    backend = _load_browser_login()
    explicit = (settings or AppSettings.load()).pdf_browser
    return backend.find_browser(explicit or None)


def _waiting_status(waited: int) -> str:
    """「还在等」那句话（``waited`` 是已经等了多少秒）。

    分开写成一个函数是为了让「等哪儿的登录」这句话只有一份：界面上的状态、
    超时提示、测试断言引的都是这里，改口径不会漏掉某处。
    """
    return (
        f"已经等了 {waited} 秒，还没在浏览器里看到登录。"
        "要在这个窗口打开的那个浏览器里登录，程序才看得到；"
        "登录成功后这里会自己关掉。"
    )


def verify_userhash_live(client, userhash: str) -> str | None:
    """问一句 X 岛：这块饼干现在还算数吗？

    返回 ``None`` 表示能用；返回一句话表示不能用（那句话直接写给用户看）。

    为什么非得问：浏览器里的 userhash 可能来自上一次登录（浏览器资料目录是留着的），
    也可能这个账号压根没在「饼干」页领过。这两种情况拿它去取串都取不到，
    可只看 cookie 是看不出来的 —— 必须让服务端表态。

    做法是拿这块饼干去请一次用户系统里的「饼干」页：没登录或饼干不认时，
    X 岛会把请求弹回登录页（真机实测：``_request_following_jumps`` 返回的
    ``final_url`` 落在 ``Member/User/Index/login.html``），认的时候才会停在饼干
    列表页。只读一次页面，不发任何写操作。
    """
    index_url = f"{client.SITE}/Member/User/Cookie/index.html"
    # 自己把饼干放进客户端再问：不留「调用方得先 import 一次」这种暗规矩 ——
    # 漏了那一步的话，问出去的是**另一个身份**，结论就反了（假饼干反而说能用）。
    client.import_userhash(userhash)
    try:
        raw, final_url = client._request_following_jumps(index_url, timeout=15.0)
    except Exception as exc:  # noqa: BLE001 —— 网络这类问题算「没验成」，下一轮再试
        return BROWSER_VERIFY_FAILED_MESSAGE.format(detail=str(exc)[:120])
    if "login" in final_url:
        return BROWSER_STALE_COOKIE_MESSAGE
    text = raw.decode("utf-8", "replace")
    if "login" in client.jump_page_url(text):
        return BROWSER_STALE_COOKIE_MESSAGE
    return None


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
        # 没成的话，这里是能拿去查原因的那句话（由 _poll 从后台线程的消息里抄下来）。
        # 外面（SettingsDialog._open_browser_login）会把它写进运行日志 —— 只留在
        # 这一行小字里的话，用户关掉窗口就再也找不到了。
        self.failure: str = ""

        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._browser = None  # backend.LoginBrowser，启动后才有
        self._session = None
        # 「这次用哪个浏览器」的那行人话（定下来之后才显示），见 _set_browser_note。
        self._browser_note = ""
        # 最近一条「程序刚才试到哪一步」的结论（也进 self.failure，见 _poll）。
        self._hint = ""
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
        # 这里弹出的其实是同一个 Edge，只是换了份干净的临时资料目录，所以看上去
        # 「插件、保存的密码全都没有」。而且 Chrome/Edge 136 起不允许在默认资料目录
        # 上开调试端口（官方为防「偷 Cookie」做的改动），想读饼干就必须换目录。
        # 所以这里要把「为什么是空的」「你要做什么」直接写清楚，别让人自己猜。
        # 分成三段：每段一件事，短句子按句号断开，免得折行把「X 岛」这种词劈开。
        # 分成几行写：Tk 的中文折行是按字符切的，长句容易把「X岛」这种词劈开，
        # 也容易让下一行以逗号开头 —— 每行控制在一句以内就干净了。
        self.intro = tk.Label(
            card.body,
            text=(
                "点开后弹出一个独立的浏览器窗口 —— 干净的一次性窗口：\n"
                "没有你平时装的插件，也没有保存的密码。\n"
                "用的是机器上的 Chrome / Edge（Chromium、Brave 也行）；\n"
                "Firefox 内核的浏览器不行，两者不是同一套内核。\n"
                "请在里面用 X岛账号登录一次（跟微软账号无关，别输微软密码）。\n"
                "程序只取一个 X岛的登录饼干；关掉窗口后临时资料就删掉了，\n"
                "你自己的浏览器一点也不受影响。\n"
                "不想重输：关掉它，点「直接粘贴饼干登录」，\n"
                "把你自己浏览器里的 userhash 抄过来就行。"
            ),
            bg=CARD,
            fg=MUTED,
            font=SMALL_FONT,
            justify="left",
            anchor="w",
            wraplength=420,
        )
        self.intro.pack(fill="x", pady=(theme.gap(2), 0))
        wrap_to_width(self.intro, minimum=260)

        # 「这次用哪个浏览器」：定下来之前这行是空的，定下来才填上（见 _set_browser_note）。
        # 把系统默认浏览器改成 Chrome、实际打开的却还是 Edge 时（改设置没落地、
        # 或者程序在用备用的那个），当场把名字写出来能省掉一轮来回。
        self.browser_note_var = tk.StringVar(value="")
        tk.Label(
            card.body,
            textvariable=self.browser_note_var,
            bg=CARD,
            fg=MUTED,
            font=SMALL_FONT,
            justify="left",
            anchor="w",
            wraplength=420,
        ).pack(fill="x", pady=(theme.gap(1), 0))

        self.status_var = tk.StringVar(value="正在准备…")
        ttk.Label(
            card.body,
            textvariable=self.status_var,
            style="CardMuted.TLabel",
            wraplength=420,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(theme.gap(2), 0))

        # 常驻的一行「程序刚才试到哪一步」（见 _set_hint）。
        #
        # 为什么要它：状态行每 15 秒被「已经等了 N 秒…」整句**替换**掉（刻意的，
        # 否则会越堆越长），于是「饼干没领到」这类结论一闪就没了 —— 用户截个图过来，
        # 上面只有秒数。这一行只在结论变化时被替换，不会被时间刷掉；没话说时它是空的，
        # 也不占地方（第一次有话说才 pack 上去）。
        self.hint_var = tk.StringVar(value="")
        self.hint_label = tk.Label(
            card.body,
            textvariable=self.hint_var,
            bg=CARD,
            fg=MUTED,
            font=SMALL_FONT,
            justify="left",
            anchor="w",
            wraplength=420,
        )
        wrap_to_width(self.hint_label, minimum=260)

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

    def _set_browser_note(self, text: str) -> None:
        """把「这次用哪个浏览器」写上界面（主线程调；后台线程只投队列）。"""
        self._browser_note = text
        try:
            self.browser_note_var.set(text)
        except tk.TclError:  # pragma: no cover - 窗口已经销毁
            pass

    def _set_hint(self, text: str) -> None:
        """把「程序刚才试到哪一步」写到常驻那一行（主线程调）。

        跟状态行分开是有原因的：状态行每 15 秒被「已经等了 N 秒…」整句替换掉，
        失败原因一闪就没了。用户截图过来只看到秒数，谁也判断不出卡在哪一步
        （v0.13.17 就是为这件事加的）。这一行只被**新的结论**替换，不会被时间刷掉。
        """
        self._hint = text
        try:
            self.hint_var.set(text)
        except tk.TclError:  # pragma: no cover - 窗口已经销毁
            return
        # 没话说时不占地方：第一次有结论了才把它摆上来（窗口高度跟着长一行）。
        try:
            if text and not self.hint_label.winfo_manager():
                self.hint_label.pack(fill="x", pady=(theme.gap(1), 0))
        except tk.TclError:  # pragma: no cover - 窗口已经销毁
            pass

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
            if kind == "browser":  # 这次用哪个浏览器（定下来之后才来）
                self._set_browser_note(str(payload))
                continue
            if kind == "note":  # 只是提醒一句（比如没能自动把窗口切到登录页），别当失败
                current = self.status_var.get()
                self._set_status(f"{current}（{payload}）" if current else str(payload))
                continue
            if kind == "status":  # 换一句状态（「已经等了 N 秒…」）：是替换，不是追加
                self._set_status(str(payload))
                continue
            if kind == "hint":  # 「程序刚才试到哪一步」：常驻那一行，见 _set_hint
                self._set_hint(str(payload))
                continue
            if kind == "browser_closed":
                self.failure = "浏览器窗口已经关掉了，还没取到饼干。"
                self._set_status(
                    "浏览器窗口已经关掉了，还没取到饼干。点「重新打开浏览器」重开一个，"
                    "或者点「直接粘贴饼干登录」。"
                )
            else:  # 剩下的都是错误
                self.failure = str(payload)
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
        # 本窗口 grab_set 过，子窗口要用鼠标：先放开，关掉再收回来。
        self.grab_release()
        try:
            userhash = ask_pasted_cookie(self)
        finally:
            try:
                if self.winfo_exists():
                    self.grab_set()
            except tk.TclError:  # pragma: no cover - 窗口已经不可用了
                pass
        if not userhash:
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
            # 「这次用哪个浏览器」由设置里那个路径决定（留空时才自动挑），
            # 和 PDF 导出用的是同一个设置 —— 两边不一致最让人糊涂。
            info = _find_login_browser(self.settings)
        except Exception as exc:
            self._queue.put(("error", f"没能找到浏览器：{exc}"))
            return
        if info is None:
            self._queue.put(
                ("error", "没找到 Edge 或 Chrome。Windows 自带的 Edge 一般就有；"
                          "Firefox 走不了这条路（内核不同）。也可以改用「直接粘贴饼干登录」。")
            )
            return
        self._queue.put(("browser", f"这次用 {info.name} 打开（{info.path}）。"))
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
            # 配置目录里的浏览器资料要是用不了，库会自己换一个临时目录再试；
            # 默认那个浏览器起不来，库还会换一个浏览器再试。这两件事都得让用户看见，
            # 不然他会以为登录态还落在老地方、或者纳闷「我明明用的是 Chrome」。
            for note in (
                getattr(browser, "profile_note", ""),
                getattr(browser, "browser_note", ""),
            ):
                if note:
                    self._queue.put(("note", note))
            if self._stop.is_set():  # 取消正好落在启动过程中
                # 必须拿局部引用收尾：主线程那次 _release() 已经把 self._browser 清成 None，
                # 里面那个进程当时还没起来、它停不掉，再读登记处就等于放任成一个孤儿进程。
                self._release_browser(browser)
                return
            # 浏览器端点交给 CDPSession，它自己会换成页面标签再握手。
            # 建会话时把「本站点」一起告诉它：connect() 靠这个挑对页面标签
            # （Edge 自己会开 sync-confirmation 之类的内部页，不能抓错）。
            # 「浏览器还在不在」也一并交下去：读调试接口会在几秒预算内重试
            # （端口文件出现和调试服务开始收连接之间有时差），重试期间要是
            # 进程已经死了，就当场报死因，不让用户干等。
            session = backend._new_session(
                browser.browser_ws_url, failure_hint=browser.devtools_failure_hint
            )
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

        started = time.monotonic()
        deadline = started + BROWSER_LOGIN_TIMEOUT
        next_leaf = started + BROWSER_LEAF_SECONDS
        next_progress = started + BROWSER_PROGRESS_SECONDS
        verified: str | None = None  # 已经验过、当场就认的饼干：别每一轮都去问一遍
        said_dead = False  # 「这块饼干不认」只说一次，别每 1.5 秒刷一遍
        leaf_attempts = 0  # 已经**真去动过**用户标签页几次（见 BROWSER_LEAF_NAV_LIMIT）
        leaf_hint = ""  # 最近一次领饼干的结论：写进常驻那一行
        leaf_hints: list[str] = []  # 领饼干试过的几步（按发生顺序、去重）：拼进超时那句话
        cap_said = False  # 「次数用完、只读饼干罐了」只说一次，说完常驻那一行就留着它
        while not self._stop.is_set():
            process = browser.process  # 用户自己把浏览器窗口关掉时要能察觉
            if process is not None and process.poll() is not None:
                self._queue.put(("browser_closed", None))
                return
            try:
                value = self._read_userhash(backend, session)
                leaf_now = time.monotonic()
                if not value and leaf_now >= next_leaf:
                    navigate = leaf_attempts < BROWSER_LEAF_NAV_LIMIT
                    if not navigate and leaf_attempts and not cap_said:
                        # 降级这件事必须在界面上说出来（v0.13.21）：不然用户看到的只是
                        # 「程序忽然不再去应用饼干了」，跟卡死没两样。
                        cap_said = True
                        cap_hint = BROWSER_LEAF_CAP_HINT.format(
                            limit=BROWSER_LEAF_NAV_LIMIT
                        )
                        if cap_hint not in leaf_hints:
                            leaf_hints.append(cap_hint)
                        self._queue.put(("hint", cap_hint))
                        leaf_hint = cap_hint
                    value, detail, navigated = self._try_leaf_cookie(
                        backend,
                        session,
                        navigate=navigate,
                        # 已经真去应用过之后（v0.13.21），页面停在登录页不再按「用户还在
                        # 登录页打字」处理：那一趟导出页导航会把没种上 userhash 的标签页
                        # 弹到登录页，照旧早退就再也不会去领饼干，只会一直重复那句
                        # 「这个窗口里还没登录（页面停在登录页）」（用户 m29500 卡住的那支）。
                        waiting_for_login=leaf_attempts == 0,
                    )
                    # 只有**真动过**他的标签页才扣次数（v0.13.20）：用户还在登录页打字时，
                    # 这一步什么都不碰就返回了（「还没登录」），要是照旧扣，两次机会会在开头
                    # 十几秒里用光 —— 等他登进去，程序已经只会重读饼干罐，而 userhash 只有
                    # 真去「应用」才会被种下，于是界面一路等到超时（0.13.18 真机上正是如此）。
                    if navigate and navigated:
                        leaf_attempts += 1
                    # 导航过就缓一缓：站点那边「应用」是跳转式的，几秒一轮会把用户的
                    # 标签页弹成风箱；只读饼干罐的那几次不打扰他，照旧 5 秒一轮。
                    next_leaf = leaf_now + (
                        BROWSER_LEAF_RETRY_SECONDS if navigated else BROWSER_LEAF_SECONDS
                    )
                    if detail and detail != leaf_hint:
                        leaf_hint = detail
                        if detail not in leaf_hints:
                            # 有序留痕：只留最后一条的话，收尾那句「还是没看到 userhash」
                            # 会把前面有用的「还没登录」盖掉，截图上看不出卡在哪。
                            leaf_hints.append(detail)
                        if not cap_said:
                            # 次数用完之后常驻那一行就留着那句说明（比每 5 秒重复一遍
                            # 「饼干罐里还是没有 userhash」有用），但这一步照旧进历史。
                            self._queue.put(("hint", detail))
                    if not value:
                        # v0.13.22：浏览器那条路领不到时，拿**同一罐饼干**走 HTTP 再试一次。
                        # 站点给 userhash 的落点是导出页的响应体（`XdaoClient.apply_cookie`
                        # 从 v0.6.1 起就是这么读的）；让浏览器自己去跳那张跳转页只会停在
                        # 「饼干切换成功!」上原地重载 —— 用户 m29953/m29954 卡的就是这里。
                        http_value, http_detail = self._try_leaf_cookie_http(
                            backend,
                            session,
                            # 「用户可能还在打验证码」只在这一轮没动过页面、也从来没应用过时
                            # 成立（否则就是标签页被弹回登录页，而罐里其实还登录着）。真按
                            # 「在打字」处理时也只跳过**匿名**的罐子：里面已经有真会话就直接
                            # 走 HTTP —— 它不碰用户的页面，也就不会把人从表单上拽走。
                            may_skip_for_typing=not navigated and leaf_attempts == 0,
                        )
                        if http_value:
                            value = http_value
                            next_leaf = leaf_now + BROWSER_LEAF_SECONDS
                        elif http_detail and http_detail != leaf_hint:
                            # 它的原话要进「试过的几步」：这一段会进运行日志，是用户截图里
                            # 最接近真相的一句。每次都挪到最后 —— 收尾只显示最后三条
                            # （`leaf_hints[-3:]`），而这句话比「罐里还是没有」有用得多。
                            leaf_hint = http_detail
                            if http_detail in leaf_hints:
                                leaf_hints.remove(http_detail)
                            leaf_hints.append(http_detail)
                            if not cap_said:
                                self._queue.put(("hint", http_detail))
                if value and value != verified:
                    # 看到 userhash **不等于**登录成了：浏览器资料目录是留下来的，
                    # 上一回登录的旧饼干还躺在里面，会话早就过期了。不验一下就会
                    # 「界面说登录成功、导出却全是未登录」——就是这么来的。
                    # verify_userhash_live 自己会把这块饼干装进客户端再问服务端。
                    note = verify_userhash_live(self.client, value)
                    if note is None:
                        verified = value
                        if value != self.client.import_userhash(value):
                            # 理论上到不了；真到了说明客户端把值改了，宁可再等一轮，
                            # 也不能把一个来路不明的值当成功。
                            verified = None
                            continue
                        self._queue.put(("ok", value))
                        return
                    if note and not said_dead:
                        said_dead = True
                        self._queue.put(("note", note))
            except Exception as exc:
                self._queue.put(("error", f"读取浏览器饼干失败：{exc}"))
                return
            now = time.monotonic()
            if now >= next_progress:
                next_progress = now + BROWSER_PROGRESS_SECONDS
                self._queue.put(("status", _waiting_status(int(now - started))))
            if now >= deadline:
                break
            self._stop.wait(BROWSER_POLL_SECONDS)
        if not self._stop.is_set():
            # 把领饼干试过的几步按顺序拼进这句话：它会进运行日志（窗口一关就找不到了），
            # 是「到底卡在哪一步」唯一的书面记录。只留最后一条不够用 —— 「饼干罐里还是没有
            # userhash」这种最没信息量的收尾会盖掉前面「用户还没登录」那条（v0.13.20）。
            steps = leaf_hints[-3:]
            if not steps:
                tail = ""
            elif len(steps) == 1:
                tail = f"（程序试过的一步：{steps[0]}）"
            else:
                numbered = " ".join(
                    f"{'①②③'[index]} {text}" for index, text in enumerate(steps)
                )
                tail = f"（程序试过的几步：{numbered}）"
            self._queue.put(
                (
                    "error",
                    f"等了 {BROWSER_LOGIN_TIMEOUT / 60:.0f} 分钟还没看到登录成功。"
                    "要在这个窗口打开的浏览器里登录，程序才看得到 —— "
                    "在自己平时用的浏览器里登录不行；也可以点「直接粘贴饼干登录」。"
                    f"{tail}",
                )
            )

    @staticmethod
    def _read_userhash(backend, session) -> str | None:
        """从 CDP 读到的饼干里挑出 userhash；没有就返回 None。

        只负责「挑出来」，**不代表这个登录还能用** —— 这块饼干来自浏览器的
        资料目录，可能是上一次留下的。能不能用由 :func:`verify_userhash_live` 问服务端。

        问的是**好几个地址**（站点根、饼干页、当前页）：CDP 的 ``Network.getCookies``
        只回「会发给这个地址」的饼干，只问一个地址会漏，而漏掉的代价是界面一直等到
        超时（v0.13.17）。
        """
        urls = backend.cookie_urls_for(session)
        return backend.userhash_from_cookies(session.read_cookies(urls))

    @staticmethod
    def _try_leaf_cookie(
        backend,
        session,
        *,
        navigate: bool = True,
        waiting_for_login: bool = True,
    ) -> tuple[str | None, str, bool]:
        """兜底：登录了却没看到 userhash，就去饼干页领一块新的。

        返回 ``(值, 一句人话, 是否导航过)``。这一步失败是常态（人还没登录完、页面
        正在跳转），所以异常一律当「还没好」，不往界面上报错 —— 它本来就是兜底路径。

        为什么要那句人话：这条路以前是**静默**的（异常吞掉、返回 None），真机上表现
        为「明明登录好了，程序一路等到超时」，用户截图上没有任何线索（v0.13.17）。

        ``waiting_for_login`` 直接转给 :func:`browser_login.fetch_leaf_cookie`：已经
        真去应用过之后再看到登录页，不是「用户正在打字」，而是「标签页被弹回去了」。
        """
        try:
            result = backend.fetch_leaf_cookie(
                session, navigate=navigate, waiting_for_login=waiting_for_login
            )
        except Exception as exc:  # noqa: BLE001 —— 兜底路径，失败不算流程错误
            return None, f"去「饼干」页领饼干时出错：{exc}", False
        value = str(getattr(result, "value", "") or "").strip()
        detail = str(getattr(result, "detail", "") or "")
        navigated = bool(getattr(result, "navigated", False))
        if value and backend.looks_like_userhash(value):
            return value, detail, navigated
        return None, detail, navigated

    @staticmethod
    def _try_leaf_cookie_http(
        backend, session, *, may_skip_for_typing: bool = False
    ) -> tuple[str | None, str]:
        """用整罐饼干走 HTTP 那条路领饼干（v0.13.22）：不碰用户眼前那个标签页。

        返回 ``(值, 一句人话)``。浏览器里的会话饼干整罐交给
        :func:`browser_login.apply_leaf_cookie_over_http` 去跑 ``XdaoClient`` 那套从
        v0.6.1 起就在线上跑通的协议（认「跳转提示」页、跟着跳、从**导出页的正文**里
        抠 userhash）。为什么不继续让浏览器自己去跳：站点给值的落点正是导出页，而
        浏览器那条路上它会被弹回登录页，跳转页还会自己原地重载 —— 用户看到的就是
        「停在『饼干切换成功!』一直跳」（m29953/m29954）。

        ``may_skip_for_typing``：「用户可能还在输验证码」的场合，罐里只有一个匿名
        会话号（站点未登录时只给一个 ``PHPSESSID``）就先不试 —— 白连站点，而且它那句
        「没权限访问」会把更有用的「这个窗口里还没登录」顶掉。罐里已经有真会话
        （登录之后才多出来的那些饼干）就照试：这条路不碰页面，不会把人从表单上拽走。
        """
        try:
            cookies = backend.read_site_cookies(session)
        except Exception as exc:  # noqa: BLE001 —— 兜底路径，失败不算流程错误
            return None, f"读浏览器里的饼干时出错：{exc}"
        if not cookies:
            return None, ""
        if may_skip_for_typing and not _jar_holds_a_session(cookies):
            return None, ""
        try:
            result = backend.apply_leaf_cookie_over_http(cookies)
        except Exception as exc:  # noqa: BLE001 —— 同上
            return None, f"走 HTTP 领饼干时出错：{exc}"
        value = str(getattr(result, "value", "") or "").strip()
        detail = str(getattr(result, "detail", "") or "")
        if value and backend.looks_like_userhash(value):
            return value, detail
        return None, detail


def _jar_holds_a_session(cookies) -> bool:
    """罐里除了匿名会话号还有别的吗（＝浏览器里大概真登录着）。

    判据刻意宽：只要有一个名字不是 ``PHPSESSID``、值也非空的饼干就算。它只用来决定
    「要不要在用户可能还打着验证码时去连一次站点」—— 宁可多试一次，别漏掉真登录的
    那一趟（用户 m29953 就是标签页被弹回登录页、罐里其实还登录着）。
    """
    for item in cookies or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if name and name != "phpsessid" and value:
            return True
    return False


_PDF_SCALE_FALLBACK = f"{pdf_opts.SCALE_DEFAULT:g}"


def _choice_key(labels: dict, keys: list, label: str) -> str:
    """把下拉框里选中的中文标签翻回取值键；认不出就取第一项（＝默认）。"""
    for key in keys:
        if labels.get(key) == label:
            return key
    return keys[0]


def _choice_text(labels: dict, key, fallback: str) -> str:
    """配置里的键翻成下拉框要显示的中文标签（配置被手改坏时退回默认项）。"""
    return labels.get(str(key or "").strip().lower(), fallback)


def _scale_text(value) -> str:
    """缩放显示成 ``1``/``0.8`` 这种短写法（配置里存的是字符串，避免浮点误差）。"""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return _PDF_SCALE_FALLBACK


def _pdf_scale_text(raw: str, fallback: str) -> str:
    """缩放：认得出来且落在允许区间就存下来，否则保留原值。

    校验借 pdf_opts 的 ``require_valid_pdf_options`` 走一遍，rules 只有一份。
    """
    text = (raw or "").strip()
    try:
        options = pdf_opts.require_valid_pdf_options(margin="", scale=text)
    except pdf_opts.PdfOptionsError:
        return fallback
    return f"{options.scale:g}"


def _pdf_margin_mm_text(raw: str, fallback: str) -> str:
    """自定义页边距：留空＝不设（用左边预设）；越界或填错则保留原值。"""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        pdf_opts.require_valid_pdf_options(margin="", margin_mm=text)
    except pdf_opts.PdfOptionsError:
        return fallback
    return text


def _pdf_page_ranges_text(raw: str, fallback: str) -> str:
    """页码范围：留空＝全部页；写错则保留原值（不让一次误输入清掉好设置）。"""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        options = pdf_opts.require_valid_pdf_options(margin="", page_ranges=text)
    except pdf_opts.PdfOptionsError:
        return fallback
    return options.page_ranges


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
        # 说明文字用「初始 wraplength + 跟着格子宽度换行」两层：
        # 初始值让卡片知道要多宽（不设的话会按整句宽度把窗口撑开），
        # wrap_to_width 再按实际格子宽度收窄，免得 320 比格子还宽、右侧被切。
        proxy_hint = ttk.Label(
            net,
            text="例如 http://127.0.0.1:7890；留空则读取系统环境变量，仍为空表示直连。",
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        )
        proxy_hint.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(0, theme.gap(1))
        )
        wrap_to_width(proxy_hint, minimum=220)

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
        throttle_hint = ttk.Label(
            net,
            text="请求过密会被限流（429）；抓很长的串时把间隔调到 0.3～0.5 更稳。",
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        )
        throttle_hint.grid(row=3, column=0, columnspan=2, sticky="ew")
        wrap_to_width(throttle_hint, minimum=220)

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
        cache_hint = ttk.Label(
            cache,
            text="留空表示放在导出目录下的 .cache，随导出目录一起迁移。",
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        )
        cache_hint.grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(theme.gap(1), 0)
        )
        wrap_to_width(cache_hint, minimum=220)

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
        template_hint = ttk.Label(
            export,
            text=(
                "占位符：{title} 标题、{id} 串号、{date} 导出日期、{po} PO 饼干、{count} 楼层数。\n"
                "例：[{id}] {title}。留空表示用标题，无标题时取第一句话。"
            ),
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        )
        template_hint.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(0, theme.gap(1))
        )
        wrap_to_width(template_hint, minimum=220)

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
        browser_hint = ttk.Label(
            export,
            text="导出 PDF 和「用浏览器登录」时调用的浏览器（PDF 用无头模式渲染）。"
            "留空表示跟随系统默认浏览器，认不出来再找 Chrome 或 Edge。",
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        )
        browser_hint.grid(row=3, column=0, columnspan=2, sticky="ew")
        wrap_to_width(browser_hint, minimum=220)

        # ④ PDF 页面：默认「跟随网页样式」，导出效果与浏览器打印完全一致 ——
        # 老配置（没有这几个键）升级后行为不变，靠的就是这个默认值。
        pdf_row = ttk.Frame(export, style="Card.TFrame")
        pdf_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(theme.gap(1.5), 0))
        self._paper_keys = list(pdf_opts.PAPER_LABELS)
        self._orientation_keys = list(pdf_opts.ORIENTATION_LABELS)
        self._margin_keys = list(pdf_opts.MARGIN_PRESETS)

        self.pdf_paper_var = tk.StringVar(
            value=_choice_text(
                pdf_opts.PAPER_LABELS, settings.pdf_paper, pdf_opts.PAPER_LABELS["default"]
            )
        )
        self.pdf_orientation_var = tk.StringVar(
            value=_choice_text(
                pdf_opts.ORIENTATION_LABELS,
                settings.pdf_orientation,
                pdf_opts.ORIENTATION_LABELS["portrait"],
            )
        )
        self.pdf_margin_var = tk.StringVar(
            value=_choice_text(
                pdf_opts.MARGIN_PRESETS, settings.pdf_margin, pdf_opts.MARGIN_PRESETS["default"]
            )
        )
        for column, (text, variable, keys, labels, width) in enumerate(
            (
                ("纸张", self.pdf_paper_var, self._paper_keys, pdf_opts.PAPER_LABELS, 22),
                (
                    "方向",
                    self.pdf_orientation_var,
                    self._orientation_keys,
                    pdf_opts.ORIENTATION_LABELS,
                    8,
                ),
                ("页边距", self.pdf_margin_var, self._margin_keys, pdf_opts.MARGIN_PRESETS, 22),
            )
        ):
            group = ttk.Frame(pdf_row, style="Card.TFrame")
            group.grid(row=0, column=column, sticky="w", padx=(0, theme.gap(3)))
            ttk.Label(group, text=text, style="Card.TLabel").pack(anchor="w")
            ttk.Combobox(
                group,
                textvariable=variable,
                values=[labels[key] for key in keys],
                state="readonly",
                width=width,
            ).pack(anchor="w", pady=(theme.gap(0.5), 0))

        self.pdf_margin_mm_var = tk.StringVar(value=str(settings.pdf_margin_mm or ""))
        mm_group = ttk.Frame(pdf_row, style="Card.TFrame")
        mm_group.grid(row=0, column=3, sticky="w")
        ttk.Label(mm_group, text="自定义边距（毫米）", style="Card.TLabel").pack(anchor="w")
        ttk.Entry(mm_group, textvariable=self.pdf_margin_mm_var, width=10).pack(
            anchor="w", pady=(theme.gap(0.5), 0)
        )

        page_row = ttk.Frame(export, style="Card.TFrame")
        page_row.grid(row=5, column=0, columnspan=2, sticky="w", pady=(theme.gap(1.5), 0))
        scale_group = ttk.Frame(page_row, style="Card.TFrame")
        scale_group.grid(row=0, column=0, sticky="w", padx=(0, theme.gap(3)))
        ttk.Label(
            scale_group,
            text=f"缩放（{pdf_opts.SCALE_MIN:g}～{pdf_opts.SCALE_MAX:g}）",
            style="Card.TLabel",
        ).pack(anchor="w")
        self.pdf_scale_var = tk.StringVar(value=_scale_text(settings.pdf_scale))
        ttk.Entry(scale_group, textvariable=self.pdf_scale_var, width=10).pack(
            anchor="w", pady=(theme.gap(0.5), 0)
        )

        pages_group = ttk.Frame(page_row, style="Card.TFrame")
        pages_group.grid(row=0, column=1, sticky="w")
        ttk.Label(pages_group, text="页码范围", style="Card.TLabel").pack(anchor="w")
        self.pdf_pages_var = tk.StringVar(value=str(settings.pdf_page_ranges or ""))
        ttk.Entry(pages_group, textvariable=self.pdf_pages_var, width=18).pack(
            anchor="w", pady=(theme.gap(0.5), 0)
        )

        self.pdf_background_var = tk.BooleanVar(value=bool(settings.pdf_background))
        ttk.Checkbutton(
            export,
            text="打印背景（网页底色与图片背景会印出来）",
            variable=self.pdf_background_var,
            style="Card.TCheckbutton",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(theme.gap(1.5), 0))
        pdf_hint = ttk.Label(
            export,
            text=(
                "「跟随网页样式」＝与浏览器打印效果一致；自定义边距留空时用左边的预设，"
                "填了以它为准；页码范围留空表示全部页。这些设置只在导出 PDF 时生效。"
            ),
            style="CardMuted.TLabel",
            wraplength=theme.gap(80),
            justify="left",
        )
        pdf_hint.grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(theme.gap(1), 0)
        )
        wrap_to_width(pdf_hint, minimum=220)

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
        # PDF 页面：写回的都是 pdf_opts 认得的取值键；坏值保留原值（不把好设置抹掉）。
        settings.pdf_paper = _choice_key(
            pdf_opts.PAPER_LABELS, self._paper_keys, self.pdf_paper_var.get()
        )
        settings.pdf_orientation = _choice_key(
            pdf_opts.ORIENTATION_LABELS, self._orientation_keys, self.pdf_orientation_var.get()
        )
        settings.pdf_margin = _choice_key(
            pdf_opts.MARGIN_PRESETS, self._margin_keys, self.pdf_margin_var.get()
        )
        settings.pdf_margin_mm = _pdf_margin_mm_text(
            self.pdf_margin_mm_var.get(), settings.pdf_margin_mm
        )
        settings.pdf_scale = _pdf_scale_text(self.pdf_scale_var.get(), settings.pdf_scale)
        settings.pdf_page_ranges = _pdf_page_ranges_text(
            self.pdf_pages_var.get(), settings.pdf_page_ranges
        )
        settings.pdf_background = bool(self.pdf_background_var.get())

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
        # 高度要装下「列表 + 一行添加/间隔/校验 + 一行导入导出 + 一行开始监控」。
        # 2026-10-01 加了导入/导出那一行之后必须同步加高，否则底下的按钮被裁掉
        # （截图才看得出来：控件都在，就是看不见）。
        self.geometry("760x520")
        self.minsize(660, 460)
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

        # 列表的备份 / 还原单独占一行：760 宽的一行塞不下「添加 / 移除 / 间隔 /
        # 校验 / 导入 / 导出」六个控件 —— 挤在右边会被整条裁掉，看起来像没有这两个按钮
        # （2026-10-01 真机截图才发现，光看用例是绿的）。
        list_io = ttk.Frame(card.body, style="Card.TFrame")
        list_io.pack(fill="x", pady=(theme.gap(1.5), 0))
        ttk.Label(
            list_io,
            text="列表可以备份成文件，换台机器或重装之后导回来。",
            style="CardMuted.TLabel",
        ).pack(side="left")
        ttk.Button(
            list_io, text="导出列表", style="Secondary.TButton", command=self._export_list
        ).pack(side="right")
        ttk.Button(
            list_io, text="导入列表", style="Secondary.TButton", command=self._import_list
        ).pack(side="right", padx=(0, theme.gap(1.5)))

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

    # ---------- 列表的导入 / 导出 ----------

    def _export_list(self) -> None:
        if not self.app.watch_targets:
            messagebox.showinfo("提示", "监控列表是空的，没有可导出的内容。", parent=self)
            return
        chosen = filedialog.asksaveasfilename(
            title="导出监控列表",
            parent=self,
            defaultextension=watch_list.FILE_SUFFIX,
            initialfile=watch_list.suggested_filename(),
            filetypes=[("监控列表", f"*{watch_list.FILE_SUFFIX}"), ("所有文件", "*.*")],
        )
        if not chosen:
            return
        try:
            written = watch_list.export_targets(self.app.watch_targets, chosen)
        except watch_list.WatchListError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        self.status_var.set(f"已导出 {len(self.app.watch_targets)} 个监控串。")
        messagebox.showinfo(
            "导出完成",
            f"已导出 {len(self.app.watch_targets)} 个监控串：\n{written}",
            parent=self,
        )

    def _import_list(self) -> None:
        chosen = filedialog.askopenfilename(
            title="导入监控列表",
            parent=self,
            filetypes=[("监控列表", f"*{watch_list.FILE_SUFFIX}"), ("所有文件", "*.*")],
        )
        if not chosen:
            return
        try:
            result = watch_list.import_targets(self.app.watch_targets, chosen)
        except watch_list.WatchListError as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        if result.added:
            # 追加、不是替换：用户手上这份列表是他自己攒的，导入只做「并进来」。
            self.app.watch_targets.extend(result.added)
            self.app.persist_watch_targets()
            self._refresh()
        self.status_var.set(f"导入完成：{result.summary}。")
        lines = [f"文件里有 {result.total} 条，{result.summary}。"]
        if result.skipped:
            shown = result.skipped[:10]
            lines.append("")
            lines.append("跳过的条目：")
            lines.extend(f"· {who}：{reason}" for reason, who in shown)
            if len(result.skipped) > len(shown):
                lines.append(f"…另有 {len(result.skipped) - len(shown)} 条。")
        if not result.added and not result.skipped:
            lines.append("")
            lines.append("（文件里一条都没有。）")
        messagebox.showinfo("导入完成", "\n".join(lines), parent=self)

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


class SelftestDialog(tk.Toplevel):
    """环境自检：把「这台机器上哪里不对劲」摊开给用户看。

    先跑本机那几项（不联网、不改任何东西），用户点了才去测接口 —— 断网时
    本机结论照样有用，不该被联网那一步拖住。
    """

    def __init__(self, app: "App") -> None:
        super().__init__(app.root)
        self.configure(bg=BG)
        self.app = app
        # 浏览器那一项在后台线程里跑，结果经由这个队列回到主线程
        # （Tk 不能在别的线程里动）。
        self._browser_queue: queue.Queue = queue.Queue()
        self._browser_busy = False
        self.title("环境自检")
        # 卡片用 stretch=True 撑满窗口，文本区跟着长；这里给个初始大小即可，
        # 用户拉大拉小都能用。620 高时报告一趟看得完。
        self.geometry("660x620")
        # 最小高度要装得下窄窗口时折成两行的按钮排（真机量过：520 宽时
        # 报告区顶部到 349，两行按钮要 74，再加卡片下边距 —— 400 高会把
        # 「复制结果」「关闭」切掉，460 刚好留出余量）。
        self.minsize(520, 460)
        self.transient(app.root)

        outer = ttk.Frame(self, padding=(theme.gap(4), theme.gap(2)))
        outer.pack(fill="both", expand=True)
        # stretch=True：让卡片里的文本区长到窗口底部，而不是只占「请求高度」
        card = Card(outer, stretch=True)
        card.pack(fill="both", expand=True)

        SectionHeading(card.body, "环境自检").pack(fill="x")
        # 先给一个保守的 wraplength：说明文字要是按整句宽度申请空间，
        # 卡片会跟着变宽，窗口反而被顶到屏幕外（真机上就是右侧被裁掉）。
        self.intro = ttk.Label(
            card.body,
            text="查本机：配置与缓存目录、导出目录、浏览器、导出格式。"
            "只读不动，不会改你的设置。"
            "「试浏览器」会真的启动一次浏览器（临时资料目录，试完就关，"
            "不碰你自己的登录状态），用来回答「用浏览器登录为什么点不动」。",
            style="CardMuted.TLabel",
            justify="left",
            wraplength=460,
        )
        self.intro.pack(anchor="w", fill="x", pady=(theme.gap(1), 0))
        wrap_to_width(self.intro, minimum=260)

        box = ttk.Frame(card.body, style="Card.TFrame")
        box.pack(fill="both", expand=True, pady=(theme.gap(2), 0))
        scrollbar = ttk.Scrollbar(box, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.text = tk.Text(
            box,
            wrap="word",
            height=16,
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
            yscrollcommand=scrollbar.set,
        )
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.text.yview)
        self._add_context_menu(self.text)

        self.status_var = tk.StringVar(value="")
        # 状态那行也会写长句子（「已复制到剪贴板，可以直接贴给别人看。」、
        # 试浏览器的进度），同样跟着窗口宽度换行。
        self.status_label = ttk.Label(
            card.body,
            textvariable=self.status_var,
            style="CardMuted.TLabel",
            justify="left",
            wraplength=460,
        )
        self.status_label.pack(anchor="w", fill="x", pady=(theme.gap(1.5), 0))
        wrap_to_width(self.status_label, minimum=260)

        row = ttk.Frame(card.body, style="Card.TFrame")
        row.pack(fill="x", pady=(theme.gap(2), 0))
        # 五个按钮一行要 559 px，而这个窗口最小宽度是 520：交给
        # fold_buttons_when_narrow 决定是排一行还是把后两个折到第二行。
        restart_button = ttk.Button(
            row, text="重新自检", style="Secondary.TButton", command=self._run_local
        )
        # 联网检查单独一个按钮：它会真的去请求接口，慢的时候要等好几秒
        self.net_button = ttk.Button(row, text="检查联网", command=self._run_network)
        # 浏览器能不能起来是另一回事：真的把浏览器启一次，慢的时候十几秒
        self.browser_button = ttk.Button(
            row, text="试浏览器", command=self._run_browser_check
        )
        copy_button = ttk.Button(
            row, text="复制结果", style="Ghost.TButton", command=self._copy
        )
        close_button = ttk.Button(
            row, text="关闭", style="Secondary.TButton", command=self.destroy
        )
        fold_buttons_when_narrow(
            row,
            [restart_button, self.net_button, self.browser_button],
            [copy_button, close_button],
        )

        self._run_local()

    # ---------- 动作 ----------

    def _append(self, text: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text.rstrip("\n") + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def _clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _run_local(self) -> None:
        from . import preflight

        self._clear()
        for line in preflight.environment_lines():
            self._append(line)
        self._append("")
        report = preflight.run_local_checks()
        self._append(report.render())
        # 重填之后回到开头：不清的话会停在上一轮的滚动位置，
        # 真机截图里就出现过「第一行被顶上去看不见」（2026-10-01）。
        self.text.see("1.0")
        self.app.log(f"环境自检：{report.summary()}")

    def _run_network(self) -> None:
        from . import preflight

        self.status_var.set("正在检查联网…")
        self.net_button.state(["disabled"])
        self.update_idletasks()
        try:
            checks = preflight.run_network_checks()
        except Exception as exc:  # noqa: BLE001 —— 自检自己出问题也要说人话
            self._append(f"[不行] 联网检查本身出错了：{type(exc).__name__}: {exc}")
        else:
            self._append("")
            for check in checks:
                self._append(check.line())
        finally:
            self.status_var.set("")
            self.net_button.state(["!disabled"])
        self.app.log("环境自检：联网那两项已经测过。")

    def _run_browser_check(self) -> None:
        """真的把浏览器启一次，回答「到底起不起得来」。

        「环境自检」里的浏览器那一项只看得到「装没装」；装了却一起来就被安全
        软件拦下时，用户点「用浏览器登录」只会看到失败。这一项把那句话补上：
        启动、等调试端口、问一句，然后立刻关掉（临时资料目录，不碰用户的登录
        状态）。费时以秒计，所以放后台线程，主线程只轮询结果。
        """
        if self._browser_busy:
            return
        from . import browser_check

        self._clear()
        self._append("正在试浏览器：要真的启动一次，慢的时候十几秒。")
        self._append("")
        self._browser_busy = True
        self.browser_button.state(["disabled"])
        self.status_var.set("正在试浏览器…")
        # 设置里指定了浏览器就试那个（和「用浏览器登录」同一套挑选逻辑）；
        # 取不到设置（测试里的替身）就留空，交给自动挑选。
        settings = getattr(self.app, "settings", None)
        explicit = getattr(settings, "pdf_browser", "") or ""

        def work() -> None:
            def progress(message: str) -> None:
                self._browser_queue.put(("progress", message))

            try:
                report = browser_check.check_browsers(explicit or "", progress=progress)
            except Exception as exc:  # noqa: BLE001 —— 探测自己出错也要说人话
                self._browser_queue.put(
                    ("error", f"{type(exc).__name__}: {exc}")
                )
            else:
                self._browser_queue.put(("done", report))

        threading.Thread(target=work, daemon=True).start()
        self._schedule_browser_poll()

    def _schedule_browser_poll(self) -> None:
        """排下一轮轮询；窗口已经关掉就安静收摊（后台线程仍在跑，随进程结束）。"""
        try:
            self.after(BROWSER_UI_POLL_MS, self._poll_browser_check)
        except tk.TclError:  # pragma: no cover - 窗口在探测途中被关掉
            self._browser_busy = False

    def _poll_browser_check(self) -> None:
        if not self._browser_busy:
            return
        try:
            kind, payload = self._browser_queue.get_nowait()
        except queue.Empty:
            self._schedule_browser_poll()
            return
        if kind == "progress":
            # 进度只更新右下角那行状态，正文留给结论
            self.status_var.set(str(payload))
            self._schedule_browser_poll()
            return
        self._browser_busy = False
        self.browser_button.state(["!disabled"])
        self.status_var.set("")
        if kind == "error":
            self._append(f"[不行] 试浏览器这步自己出错了：{payload}")
            self.app.log("环境自检：试浏览器那步出错了。")
            return
        report = payload
        self._append(report.render())
        self.text.see("end")
        if report.ok:
            good = report.first_ok()
            name = good.name if good is not None else "浏览器"
            self.app.log(f"环境自检：浏览器可以用（{name}）——「用浏览器登录」这条路是通的。")
        else:
            self.app.log("环境自检：本机的浏览器都起不来，「用浏览器登录」暂时用不了。")

    def _copy(self) -> None:
        text = self.text.get("1.0", "end").strip()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("已复制到剪贴板，可以直接贴给别人看。")

    def _add_context_menu(self, widget: tk.Text) -> None:
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="复制", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: widget.event_generate("<<SelectAll>>"))
        widget.bind("<Button-3>", lambda event: menu.tk_popup(event.x_root, event.y_root))


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
        self._selftest_dialog: SelftestDialog | None = None
        # 新版本检查的结果（后台线程查完，主线程读）
        self._update_result: object | None = None
        self._update_button: ttk.Button | None = None
        self._checking_update = False
        self._update_queue: queue.Queue = queue.Queue()
        # 一键升级期间的状态（升级包下载、自检、换文件都走同一条队列回报）
        self._upgrading = False
        self._upgrade_spawned = False
        self._upgrade_queue: queue.Queue = queue.Queue()
        # 切换主题时旧控件会被销毁，这里记住本次实例出来的控件，便于重建
        self._widget_roots: list[tk.Misc] = []

        root.title("X岛串导出")
        root.geometry("1060x760")
        root.minsize(940, 680)

        self._build_ui()
        self.refresh_watch_status()
        self.refresh_cache_info()
        self.log("就绪。填入串网址后点「开始导出」。")
        # 启动后悄悄问一次「有没有新版本」：查到了只写一行日志，不弹窗打扰。
        self.root.after(1200, lambda: self.check_update(silent=True))
        # 上次升级换下来的旧目录（<名字>.old-<时间戳>）留一天就够了，顺手清掉。
        # 放在后台线程里：列目录、删目录都不该拖慢启动。
        threading.Thread(target=self._cleanup_old_backups, daemon=True).start()
        # 上一版（以及被强杀的那几次）可能把临时资料目录留在 %TEMP% 里：那次登录
        # 窗口用的资料目录一次就是几十上百 MB，攒起来很可观。启动后在后台扫掉旧的，
        # 还有浏览器开着的、或者刚建出来的，一律不碰（判据见 browser_login 里那个函数）。
        threading.Thread(target=self._sweep_temp_profiles, daemon=True).start()
        # 刚升级完的那一次，帮手自己住在暂存目录里、删不掉自己，于是留了个记号；
        # 等老进程（连同帮手）彻底走人之后再收，所以晚几秒、也在后台线程里做。
        self.root.after(3000, lambda: threading.Thread(
            target=self._cleanup_staging_leftovers, daemon=True).start())

    def _cleanup_old_backups(self) -> None:
        try:
            from . import updater

            updater.cleanup_backups()
        except Exception:  # noqa: BLE001 —— 清不干净也不该影响启动
            pass

    def _sweep_temp_profiles(self) -> None:
        """扫掉以前留在 %TEMP% 里的临时资料目录（登录窗口、自检各一份）。

        程序被强杀时没人收尾，那些目录会一直留着；正常路径由
        ``LoginBrowser.cleanup_temp_profile()`` 当场删掉。放后台线程里做：
        要列目录，还要挨个问一次调试端口。
        """
        try:
            from . import browser_login

            browser_login.sweep_stale_temp_profiles()
        except Exception:  # noqa: BLE001 —— 清不干净也不该影响启动
            pass

    def _cleanup_staging_leftovers(self) -> None:
        try:
            from . import updater

            # 帮手这会儿可能还没走（它要等老进程、还要启动我们），所以隔几秒再试几次。
            updater.cleanup_staging_leftovers_at_startup()
        except Exception:  # noqa: BLE001 —— 同上，删不掉就算了
            pass

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
        # 按钮先 pack：右边那块位置先占住。这一行在窄窗口里放不下「整句缓存信息
        # + 两个按钮」，要是先放标签，它会吃掉整行，把两个按钮挤到卡片外面
        # （真机 940 宽时就是这样，「打开目录」「清空缓存」都看不见、点不到）。
        cache_buttons = ttk.Frame(cache_row, style="Card.TFrame")
        cache_buttons.pack(side="right")
        ttk.Button(
            cache_buttons, text="打开目录", style="Secondary.TButton", command=self.open_folder
        ).pack(side="right")
        ttk.Button(
            cache_buttons,
            text="清空缓存",
            style="Ghost.TButton",
            command=self.clear_cache,
        ).pack(side="right", padx=(0, theme.gap(1)))
        # 剩下的宽度才是标签的：给它一个保守的初始换行宽度，再跟着实际宽度走，
        # 缓存目录再长也能整个看到（换行，而不是被切掉）。
        self.cache_label = ttk.Label(
            cache_row,
            textvariable=self.cache_info_var,
            style="CardFaint.TLabel",
            justify="left",
            wraplength=theme.gap(50),
        )
        self.cache_label.pack(side="left", fill="x", expand=True)
        wrap_to_width(self.cache_label, minimum=160)

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
        # 说明文字自己占一行、铺满卡片宽度：跟按钮挤在一行时，940 宽的
        # 窗口里留给它的只有 85 px（量到过），再怎么写都会缺字。
        log_hint = ttk.Label(
            card.body,
            text="抓取与导出的每一步都会记在这里",
            style="CardFaint.TLabel",
            justify="left",
            wraplength=theme.gap(45),
        )
        log_hint.pack(anchor="w", fill="x", pady=(theme.gap(0.5), 0))
        wrap_to_width(log_hint, minimum=160)
        # 按钮单独一行、靠右：位置先占住，谁也不会被挤成半个。
        log_head = ttk.Frame(card.body, style="Card.TFrame")
        log_head.pack(fill="x", pady=(theme.gap(0.5), theme.gap(1.5)))
        log_buttons = ttk.Frame(log_head, style="Card.TFrame")
        log_buttons.pack(side="right")
        ttk.Button(
            log_buttons, text="保存…", style="Ghost.TButton", command=self.save_log
        ).pack(side="right")
        ttk.Button(
            log_buttons, text="清空", style="Ghost.TButton", command=self.clear_log
        ).pack(side="right", padx=(0, theme.gap(0.5)))
        ttk.Button(
            log_buttons, text="自检", style="Ghost.TButton", command=self.open_selftest
        ).pack(side="right", padx=(0, theme.gap(0.5)))
        self._update_button = ttk.Button(
            log_buttons, text="检查更新", style="Ghost.TButton", command=self.check_update
        )
        self._update_button.pack(side="right", padx=(0, theme.gap(0.5)))

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
            dialog = LoginDialog(self.root, self.client, self.settings, app=self)
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

    def open_selftest(self) -> None:
        if self._selftest_dialog is not None and self._selftest_dialog.winfo_exists():
            self._selftest_dialog.lift()
            self._selftest_dialog.focus_set()
            return
        self._selftest_dialog = SelftestDialog(self)

    # ---------- 新版本检查 ----------

    def check_update(self, silent: bool = False) -> None:
        """问一次 GitHub 有没有新版本。

        查完把结果写进运行日志：有新版本会带一个 ★，失败只说一句。
        点按钮那次如果早就知道有新版本，会直接问「要不要打开下载页」；
        启动时那次（``silent=True``）只写日志，不弹窗打扰。
        """
        if self._checking_update or self._upgrading:
            return
        result = self._update_result
        if result is not None and not silent and getattr(result, "newer", False):
            # 已经知道有新版本了，再点就直接问「去不去下载页」
            self._offer_download(result)
            return
        self._checking_update = True
        if self._update_button is not None:
            self._update_button.config(text="检查中…")
        # 结果用队列交回主线程：Tk 的对象只能在主线程里碰，工作线程直接调
        # after() 会撞上 RuntimeError: main thread is not in main loop。
        self._update_queue = queue.Queue()
        threading.Thread(
            target=self._update_worker, args=(self._update_queue,), daemon=True
        ).start()
        self.root.after(120, self._poll_update)

    def _update_worker(self, box: queue.Queue) -> None:
        try:
            from . import update_check

            # force=True：点了按钮就是要现查，别拿一天的缓存糊弄人
            result = update_check.check_for_update(force=True)
        except Exception:  # noqa: BLE001 —— 查版本失败绝不能影响主流程
            result = None
        box.put(result)

    def _poll_update(self) -> None:
        """在主线程里看一眼后台查完没有（和导出、监控用的是同一套做法）。"""
        box = getattr(self, "_update_queue", None)
        if box is None:
            return
        try:
            result = box.get_nowait()
        except queue.Empty:
            self.root.after(120, self._poll_update)
            return
        self._update_done(result)

    def _update_done(self, result: object) -> None:
        self._checking_update = False
        if result is not None:
            self._update_result = result
        if self._update_button is not None and self._update_button.winfo_exists():
            newer = bool(result is not None and getattr(result, "newer", False))
            if not self._upgrading:
                self._update_button.config(text="有新版本" if newer else "检查更新")
        if result is None:
            return
        line = result.line() if hasattr(result, "line") else ""
        if getattr(result, "newer", False):
            self.log(f"★ {line}")
        elif getattr(result, "ok", False):
            self.log(line)
        elif not getattr(result, "from_cache", False):
            # 查不到（没网、代理不通）只说一次，不反复念
            self.log(line)

    def _offer_download(self, result: object) -> None:
        """发现有新版本之后：能原地升级就问一句，否则退回「打开下载页」。

        能不能原地升级取决于环境（打包版、目录可写……），这些都由
        :func:`xdao.updater.plan_upgrade` 判断，判断结果会直接说给用户听。
        """
        from . import updater

        url = getattr(result, "url", "")
        text = getattr(result, "line", lambda: "")()
        plan = updater.plan_upgrade()
        if plan.possible and getattr(result, "asset_url", ""):
            size = getattr(result, "asset_size", 0)
            size_text = f"，{size / 1024 / 1024:.1f} MB" if size else ""
            if messagebox.askyesno(
                "有新版本",
                f"{text}\n\n现在下载并安装吗？{size_text}\n"
                "会先把新版跑一遍自检，通过了才替换；旧版本仍留在原地，"
                "万一新版起不来可以改回来。",
            ):
                self._start_upgrade(result)
                return
        elif plan.reason:
            self.log(plan.reason)
        if not url:
            self.log(text)
            return
        if messagebox.askyesno("有新版本", f"{text}\n\n现在打开下载页吗？"):
            self._open_download_page(url)

    def _open_download_page(self, url: str) -> None:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001 —— 打不开浏览器就把网址写进日志
            self.log(f"下载页：{url}")

    # ---------- 一键升级 ----------

    def _start_upgrade(self, result: object) -> None:
        """下载新版 → 自检 → 换文件 → 重启，四步都在后台线程里做。

        每步结果用 ``_upgrade_queue`` 交回主线程：Tk 只能在主线程碰，
        这条规矩和导出、监控、查版本那边一模一样。
        """
        if self._upgrading:
            return
        self._upgrading = True
        if self._update_button is not None:
            self._update_button.config(text="升级中…")
        self.log("开始下载新版本…（下载和自检期间界面照常能用）")
        self._upgrade_queue = queue.Queue()
        threading.Thread(
            target=self._upgrade_worker, args=(result, self._upgrade_queue), daemon=True
        ).start()
        self.root.after(120, self._poll_upgrade)

    def _upgrade_worker(self, result: object, job: queue.Queue) -> None:
        from . import updater

        def progress(written: int, total: int) -> None:
            # 有总长度就报百分比，没有就只报已下多少（服务器没给 Content-Length）
            job.put(("progress", (written, total) if total else (written,)))

        staging = updater.default_staging_root()
        target = ""
        try:
            plan = updater.plan_upgrade()
            if not plan.possible:
                job.put(("finish", (False, plan.reason or "这个环境不能原地升级。", "")))
                return
            asset_url = getattr(result, "asset_url", "")
            if not asset_url:
                job.put(("finish", (False, "这个版本的发布里没有免安装包，只好手工下载。", "")))
                return
            archive = staging / "update.zip"
            updater.download_asset(
                asset_url, archive, proxy=updater.detect_proxy(), progress=progress
            )
            job.put(("note", "下载完成，正在解压…"))
            payload = updater.extract_payload(archive, staging / "payload")
            # zip 已经解开了，先把这十几 MB 删掉，别在用户 %TEMP% 里占着
            updater.remove_quietly(archive)
            job.put(("note", "正在给新版本做自检（先跑一遍再换，免得换上去打不开）…"))
            passed, detail = updater.run_selftest(updater.payload_launcher(payload))
            job.put(("note", detail))
            if not passed:
                job.put(("finish", (False, f"{detail}，这次不换，现场保持原样。", "")))
                return
            if result.latest is not None:
                target = result.latest.display_version
            decision: queue.Queue = queue.Queue()
            job.put(("confirm", (target, decision)))
            if not decision.get():
                job.put(("finish", (False, "已取消升级，现场保持原样。", "")))
                return
            job.put(("note", "正在替换文件…"))
            updater.spawn_helper(plan, payload)
            self._upgrade_spawned = True
        except Exception as exc:  # noqa: BLE001 —— 升级失败绝不能把界面带走
            job.put(("finish", (False, f"{type(exc).__name__}: {exc}", "")))
            return
        finally:
            if not self._upgrade_spawned:
                # 没交给帮手就把暂存丢掉，别在用户 %TEMP% 里留个几十 MB 的空壳；
                # 交给帮手的那一次不能删 —— 帮手正要用里面的新版本。
                updater.discard_staging(staging)
        job.put(
            (
                "finish",
                (
                    True,
                    f"新版本 {target} 已就位，程序马上自己重启。\n"
                    "旧版本留在原目录（名字里带 .old-），下次启动会自动清掉。",
                    "exit",
                ),
            )
        )

    def _poll_upgrade(self) -> None:
        box = getattr(self, "_upgrade_queue", None)
        if box is None:
            return
        while True:
            try:
                kind, payload = box.get_nowait()
            except queue.Empty:
                self.root.after(120, self._poll_upgrade)
                return
            if not self._upgrade_progress(kind, payload):
                return

    def _upgrade_progress(self, kind: str, payload: object) -> bool:
        """处理一条升级进度；返回 False 表示不再继续轮询。"""
        if kind == "note":
            self.log(str(payload))
            return True
        if kind == "progress":
            self._upgrade_progress_text(payload)
            return True
        if kind == "confirm":
            target, decision = payload  # type: ignore[misc]
            agree = messagebox.askyesno(
                "立刻升级",
                f"新版本自检通过，现在就用它替换当前程序吗？\n\n"
                f"即将换成：{target}\n"
                "换好之后程序会自己重启。",
            )
            if not agree:
                self.log("已取消升级。")
            decision.put(bool(agree))
            return True
        if kind == "finish":
            ok, message, action = payload  # type: ignore[misc]
            self._upgrade_finish(bool(ok), str(message), str(action))
            return False
        return True

    def _upgrade_progress_text(self, payload: object) -> None:
        """按钮上的进度：知道总长度就写百分比，不知道就写已经下了多少 KB。"""
        try:
            numbers = [int(item) for item in payload]  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return
        if not numbers:
            return
        if len(numbers) >= 2 and numbers[1]:
            written, total = numbers[0], numbers[1]
            percent = min(100, int(written * 100 / total))
            self._update_button_text(f"升级中 {percent}%")
        else:
            self._update_button_text(f"升级中 {numbers[0] // 1024} KB")

    def _update_button_text(self, text: str) -> None:
        if self._update_button is None:
            return
        try:
            if not self._update_button.winfo_exists():
                return
            self._update_button.config(text=text)
        except tk.TclError:
            pass

    def _upgrade_finish(self, ok: bool, message: str, action: str) -> None:
        self._upgrading = False
        self.log(message)
        if not ok:
            self._update_button_text("有新版本")
            messagebox.showerror("升级没成功", message)
            return
        self._update_button_text("升级完成")
        if action == "exit":
            # 帮手在等我们退出：它要拿到「现场目录已经没人占用」才动手换。
            #
            # 这里**不能弹消息框**：模态框会一直等着用户点确定，而帮手
            # 只在 WAIT_FOR_EXIT_SECONDS(60s) 内等老进程消失，点慢一拍这次
            # 升级就白换了（用户还看见「升级完成」）。上面那行 log 已经报过
            # 新版就位，直接让位即可。
            self._end_self_for_upgrade()

    def _end_self_for_upgrade(self) -> None:
        """升级交出去了，把自己彻底结束掉，好让帮手能换目录。

        **不能只 `root.quit()`**：实测（`_scratch\\probe_quit_exits.py`）在
        真机上 `quit()` 之后进程还在，哪怕当时只剩一个主线程 —— 帮手
        `wait_for_exit` 等满 60 秒就放弃，用户看到「升级完成」但目录其实没换。
        Tk 的退出本来就只打算结束事件循环，所以这里直接结束进程。

        此刻该落的盘都已经落了（下载、解压、自检、帮手都做完了），
        剩下要做的只有「让开位置」，没有需要慢慢收尾的东西。
        """
        self.log("退出程序，把位置让给新版本…")
        try:
            self.root.quit()
        finally:
            os._exit(0)

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
        if self.current_format() == "pdf":
            # 只在真的改过纸张/边距/背景/页码时说一声，全默认不必占一行。
            pdf_options = pdf_opts.from_settings(self.settings)
            if pdf_options.needs_cdp or not pdf_options.background:
                self.log(f"PDF 设置：{pdf_options.describe()}")

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
                pdf_options=pdf_opts.from_settings(self.settings),
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
                    browser_path=self.settings.pdf_browser or None,
                    pdf_options=pdf_opts.from_settings(self.settings),
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
                    browser_path=self.settings.pdf_browser or None,
                    pdf_options=pdf_opts.from_settings(self.settings),
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
