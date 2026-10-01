"""「用浏览器登录」对话框的测试。

真正的 ``xdao.browser_login`` 照常参与：只换掉两处会碰真实世界的边界 ——
起进程（``subprocess.Popen``）和连 CDP（``CDPSession``）。所以被测的是真的
``LoginBrowser``（真的会删过期端口文件、真的会解析 ``DevToolsActivePort``、
真的会 terminate 进程）和真的对话框逻辑，唯独不会真弹浏览器、也不碰网络。

界面部分要真 Tk 窗口，所以照 ``tests/test_window.py`` 的写法：拿不到显示环境就
skip，测试用的配置一定指向产物目录，绝不碰用户真实的 ``config.json``。
"""

from __future__ import annotations

import ast
import json
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

from xdao import browser_login, gui
from xdao.client import XdaoClient
from xdao.gui import verify_userhash_live as real_verify_userhash_live
from xdao.settings import AppSettings

# 假的调试端口与 userhash：只在本进程里用，不指向任何真实东西。
FAKE_PORT = 32123
FAKE_WS_PATH = "/devtools/browser/fake-tab"
FAKE_USERHASH = "f0e1d2c3b4a5"
PASTED_COOKIE = f"uid=9527; userhash={FAKE_USERHASH}; sessionid=deadbeef"


# ---------- 替身：进程与 CDP 会话 ----------


class _FakeProcess:
    """冒充浏览器进程：构造时就把调试端口写进 profile，和真浏览器一样。"""

    def __init__(self, args, *, exit_immediately: bool = False, **kwargs) -> None:
        self.args = [str(item) for item in args]
        self.popen_kwargs = kwargs
        self.returncode: int | None = 1 if exit_immediately else None
        self.terminated = False
        self.killed = False
        self.profile: Path | None = None
        if not exit_immediately:
            self._write_devtools_port()

    def _write_devtools_port(self) -> None:
        for item in self.args:
            if not item.startswith("--user-data-dir="):
                continue
            self.profile = Path(item.split("=", 1)[1])
            self.profile.mkdir(parents=True, exist_ok=True)
            (self.profile / "DevToolsActivePort").write_text(
                f"{FAKE_PORT}\n{FAKE_WS_PATH}\n", encoding="utf-8"
            )
            return

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def exit(self, code: int = 1) -> None:
        """模拟用户自己把浏览器窗口关掉。"""
        self.returncode = code


class _FakeSubprocess:
    """替身模块：只露出 ``browser_login`` 用到的那几样，不动全局 subprocess。"""

    DEVNULL = subprocess.DEVNULL
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, *, exit_immediately: bool = False, popen_release=None) -> None:
        self.processes: list[_FakeProcess] = []
        self.taskkills: list[list[str]] = []
        self.popen_entered = threading.Event()
        self._exit_immediately = exit_immediately
        self._popen_release = popen_release

    def Popen(self, args, **kwargs):  # noqa: N802 —— 冒充 subprocess.Popen
        # 用例可以让进程「在门口等一会」：这样取消一定发生在 process 还是 None 的那一瞬，
        # 才真的撞上「启动完才发现已经取消」那条收尾路径。
        self.popen_entered.set()
        if self._popen_release is not None:
            self._popen_release.wait(timeout=30.0)
        process = _FakeProcess(args, exit_immediately=self._exit_immediately, **kwargs)
        self.processes.append(process)
        return process

    def run(self, args, **kwargs):  # noqa: N802 —— 假装 taskkill 收进程树
        self.taskkills.append([str(item) for item in args])
        return subprocess.CompletedProcess(args, 0)


class _FakeSession:
    """冒充 CDP 会话：脚本化读到的饼干与 evaluate 的返回值。

    按 ``CDPSession`` 的**完整契约**来冒充：它是上下文管理器（``with`` 进来就连、
    出去就关），还能 ``call`` / ``current_url``。库将来若在 ``LoginBrowser.start()``
    里自己开一条会话（历史上确实有过这么一段），替身也不会漏接；判定「哪条是
    对话框自己的」看地址是不是 ``ws://``。
    """

    instances: list["_FakeSession"] = []

    def __init__(
        self,
        ws_url: str,
        timeout: float = 15.0,
        site_urls=None,
        http_json=None,
        failure_hint=None,
    ) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        # 界面层建会话时会带上「本站点」前缀与「怎么读 /json/list」，替身照单全收。
        self.site_urls = list(site_urls or [])
        self.http_json = http_json
        # 「浏览器还在不在」也一起交下来：读调试接口要重试时用它当场报死因。
        self.failure_hint = failure_hint
        self.connected = False
        self.closed = False
        self.cookies: list[dict] = []
        self.evaluate_value = ""
        self.evaluate_calls: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.current_url_value = ""
        self.read_error: Exception | None = None
        # v0.13.17：领饼干那条路从「页面里的 fetch」改成了「导航 + 读页面状态」，
        # 替身得能演这两种动作 —— 不然用例只能钉住「取到了值」，钉不住「怎么取的」。
        self.pages: dict[str, dict] = {}
        self.navigations: list[str] = []
        self.cookie_reads: list[list[str] | None] = []
        self.export_text = ""
        _FakeSession.instances.append(self)

    def __enter__(self) -> "_FakeSession":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.close()
        return False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        self.calls.append((method, params or {}))
        if method == "Page.navigate":
            # 真导航就是换一份文档：地址变了，随后 evaluate 读到的是那一页的状态。
            self.current_url_value = str((params or {}).get("url") or "")
            self.navigations.append(self.current_url_value)
        return {}

    def current_url(self) -> str:
        return self.current_url_value

    def read_cookies(self, urls: list[str] | None = None) -> list[dict]:
        self.cookie_reads.append(list(urls) if urls else None)
        if self.read_error is not None:
            raise self.read_error
        return list(self.cookies)

    def evaluate(self, expression: str, await_promise: bool = False) -> str:
        self.evaluate_calls.append(expression)
        # 读页面状态那条脚本按「当前地址」取登记好的状态；其余脚本给默认值，
        # 只有刻意用 evaluate_value 的用例才会走到最后一行。
        if expression == browser_login.build_find_apply_script():
            page = self.pages.get(
                self.current_url_value, {"url": self.current_url_value, "kind": "other"}
            )
            return json.dumps(page)
        if expression == "document.readyState":
            return "complete"
        if expression == "location.href":
            return self.current_url_value
        if expression.startswith("document.body"):
            return self.export_text
        return self.evaluate_value


# ---------- 夹具 ----------


def _make_root(timeout: float = 5.0) -> tk.Tk:
    """建一个 Tk 根窗口；没有可用显示环境时跳过（照 tests/test_window.py）。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            root = tk.Tk()
            break
        except tk.TclError as exc:  # pragma: no cover - 只在无显示环境的机器上
            if time.monotonic() >= deadline:
                pytest.skip(f"没有可用的显示环境：{exc}")
            time.sleep(0.2)
    # 挪到屏幕外：测试窗口不该在用户眼前跳。
    root.geometry("+3000+3000")
    root.update()
    return root


@pytest.fixture(scope="module")
def root_window():
    root = _make_root()
    root.title("browser-login-test")
    try:
        yield root
    finally:
        try:
            root.destroy()
        except tk.TclError:  # pragma: no cover - 测试自己已经关掉了
            pass


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """挡住真实配置：本模块的用例一律不读也不写 ``%APPDATA%`` 里的 config.json。"""
    monkeypatch.setattr(AppSettings, "load", classmethod(lambda cls: cls()))
    monkeypatch.setattr(AppSettings, "save", lambda self: None)


@pytest.fixture(autouse=True)
def fast_browser_polling(monkeypatch):
    """把对话框的轮询间隔调快，免得用例真的等几秒。"""
    monkeypatch.setattr(gui, "BROWSER_POLL_SECONDS", 0.05)
    monkeypatch.setattr(gui, "BROWSER_LEAF_SECONDS", 0.15)
    # 「导航过一次就缓一缓」的间隔也调快：不然后面那句「不再导航」的用例要真空等 20 秒。
    monkeypatch.setattr(gui, "BROWSER_LEAF_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(gui, "BROWSER_UI_POLL_MS", 20)


@pytest.fixture(autouse=True)
def clean_fake_sessions():
    _FakeSession.instances.clear()
    yield
    _FakeSession.instances.clear()


@pytest.fixture(autouse=True)
def assume_live_cookies(monkeypatch):
    """默认让「这块饼干还算数」这一步直接通过。

    它要真去问 X 岛（一次 HTTPS），用例不该靠网络；这里想钉的是「浏览器起来 →
    读到 userhash → 存进客户端 → 收尾」这条链路，不是服务端认不认。专门验这一点的
    用例自己把它换成假的返回值。
    """
    monkeypatch.setattr(gui, "verify_userhash_live", lambda client, value: None)


@pytest.fixture
def install_browser_shim(monkeypatch, artifacts_dir):
    """装上替身，返回 shim；``exit_immediately=True`` 表示浏览器刚起就退出。

    ``other_browser=None`` 时只让「一个浏览器」参与重试（替身只造得出一个），
    免得机器上真装有 Edge/Chrome 的用例多起几个假进程、行为随机器而变；
    传一个可执行文件名（比如 ``"chrome.exe"``）就能多造一个候选，
    用来钉住「当前那个起不来就换下一个」这条路。
    """

    def install(
        *, exit_immediately: bool = False, popen_release=None, other_browser: str | None = None
    ) -> _FakeSubprocess:
        shim = _FakeSubprocess(exit_immediately=exit_immediately, popen_release=popen_release)
        monkeypatch.setattr(browser_login, "subprocess", shim)
        monkeypatch.setattr(browser_login, "CDPSession", _FakeSession)
        # find_browser 走真实现，只是把候选换成一个真实存在的假可执行文件。
        # explicit 原样传下去：界面层现在会把设置里那个「PDF 浏览器」交给它。
        exe = Path(artifacts_dir) / "msedge.exe"
        exe.write_bytes(b"")
        real_find = browser_login.find_browser
        monkeypatch.setattr(
            browser_login,
            "find_browser",
            lambda explicit=None, env=None: real_find(explicit=explicit or str(exe)),
        )
        fake = [browser_login.BrowserInfo(name="Edge", path=str(exe))]
        if other_browser:
            second = Path(artifacts_dir) / other_browser
            second.write_bytes(b"")
            fake.append(
                browser_login.BrowserInfo(
                    name=browser_login._guess_name(second), path=str(second)
                )
            )
        # 和真实现一样把传进来的那个排最前：界面可能给一个不在候选表里的路径
        # （设置里手动指定的浏览器），此时「当前这个」必须是它，否则用例会
        # 误以为库自己换了浏览器。
        monkeypatch.setattr(
            browser_login,
            "browser_candidates",
            lambda info=None, env=None: ([info] if info is not None else []) + fake,
        )
        return shim

    return install


@pytest.fixture
def browser_shim(install_browser_shim) -> _FakeSubprocess:
    return install_browser_shim()


@pytest.fixture
def open_dialog(root_window, artifacts_dir):
    """造一个对话框并保证收尾：就算断言失败也不留在跑的线程/进程/窗口。

    ``client`` 可以换：对话框一起来后台线程就开始跑了，等造完再去改
    ``dialog.client`` 已经太晚（那正是「验饼干」这一步要用的东西）。想看
    「验饼干到底问了谁」，就把替身在这里交进去。
    """
    client = XdaoClient()
    settings = AppSettings(_path=Path(artifacts_dir) / "config.json")
    created: list[gui.BrowserLoginDialog] = []

    def factory(client_for_dialog=None) -> gui.BrowserLoginDialog:
        dialog = gui.BrowserLoginDialog(
            root_window, client_for_dialog if client_for_dialog is not None else client, settings
        )
        dialog.start()  # 不等 __init__ 里那次 after：用例直接开跑
        created.append(dialog)
        return dialog

    factory.client = client  # type: ignore[attr-defined]
    factory.settings = settings  # type: ignore[attr-defined]
    # 先在产物目录里放一个假浏览器、写进设置，再开对话框：界面层会拿它当「手动指定」。
    def set_explicit_browser(name: str = "chrome.exe") -> Path:
        exe = Path(artifacts_dir) / name
        exe.write_bytes(b"")
        settings.pdf_browser = str(exe)
        return exe

    factory.set_explicit_browser = set_explicit_browser  # type: ignore[attr-defined]
    yield factory
    for dialog in created:
        try:
            dialog._release()
        except Exception:  # pragma: no cover - 收尾不能反过来弄挂用例
            pass
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except tk.TclError:  # pragma: no cover
            pass
    try:
        root_window.update()
    except tk.TclError:  # pragma: no cover
        pass


# ---------- 小工具 ----------


def _pump(root: tk.Tk, seconds: float = 0.1) -> None:
    """让主线程跑几轮事件循环（后台消息靠 after 轮询消费）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)


def _wait_for(root: tk.Tk, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _dialog_sessions() -> list[_FakeSession]:
    """已经连上的、属于对话框自己的那条会话：连的是浏览器端点（``ws://``）。

    这样筛是为了不被库的内部会话误伤：``LoginBrowser.start()`` 曾经自己开过一条
    ``http://…/json/list`` 的会话去把标签页拽到登录页（``_ensure_login_page``），
    那条属于库的实现细节；将来再有类似动作，也仍然只算 ``ws://`` 这条。
    """
    return [
        item
        for item in _FakeSession.instances
        if item.connected and item.ws_url.startswith("ws://")
    ]


# ---------- 用例 ----------


def test_start_opens_the_browser_and_returns_the_userhash(
    root_window, browser_shim, open_dialog
):
    """主路径：浏览器起来 → 读到 userhash → 存进客户端 → 关掉窗口和浏览器。"""
    dialog = open_dialog()
    seen: list[_FakeSession] = []

    def ready() -> bool:
        for session in _dialog_sessions():
            session.cookies = [{"name": "userhash", "value": FAKE_USERHASH}]
            if session not in seen:
                seen.append(session)
        return dialog.userhash is not None

    assert _wait_for(root_window, ready), "没能从浏览器读到 userhash"
    assert dialog.userhash == FAKE_USERHASH
    # 拿到的值必须真的落进客户端（不然界面显示成功、请求却是匿名的）。
    assert _wait_for(root_window, lambda: not dialog.winfo_exists())

    session = seen[0]
    # 连的必须是浏览器端点：CDPSession 会自己换成页面标签，传 /json/list 会握不上手。
    assert session.ws_url.startswith(f"ws://127.0.0.1:{FAKE_PORT}/devtools/browser")
    assert session.closed
    assert browser_shim.processes, "浏览器进程没有被启动过"
    assert all(process.returncode is not None for process in browser_shim.processes)
    assert browser_shim.processes[-1].terminated


def test_stale_cookie_from_the_browser_is_not_reported_as_a_success(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """浏览器资料目录里躺着的旧饼干不许当成功。

    现象是「登录窗口里明明登进去了，程序却没登录上」：程序只要在浏览器里看到
    ``userhash`` 这块 cookie 就宣布成功并关窗，而那块饼干可能是上一次留下的、
    服务端早就不认了 —— 关窗之后导出全是未登录。这一版改成先问服务端认不认。
    """
    asked: list[str] = []

    def stale(client, value: str) -> str | None:
        asked.append(value)
        return gui.BROWSER_STALE_COOKIE_MESSAGE

    monkeypatch.setattr(gui, "verify_userhash_live", stale)
    dialog = open_dialog()

    def saw_note() -> bool:
        for session in _dialog_sessions():
            session.cookies = [{"name": "userhash", "value": FAKE_USERHASH}]
        return "X 岛不认" in dialog.status_var.get()

    assert _wait_for(root_window, saw_note), f"界面上没说清饼干不认：{dialog.status_var.get()!r}"
    # 关键：窗口不许关，也不许把这块饼干当成登录结果。
    assert dialog.winfo_exists(), "饼干不认却把窗口关了"
    assert dialog.userhash is None, "饼干不认却把 userhash 交出去了"
    assert asked and asked[0] == FAKE_USERHASH, f"压根没问过服务端：{asked!r}"


class _AskClient:
    """冒充 ``XdaoClient``：只看它被问了哪个地址、收到什么饼干。"""

    SITE = "https://www.nmbxd1.com"

    def __init__(self) -> None:
        self.imported: list[str] = []
        self.urls: list[str] = []

    def import_userhash(self, value: str) -> str:
        self.imported.append(value)
        return value

    def _request_following_jumps(self, url, **kwargs):
        self.urls.append(url)
        return b"<html><body>cookie list</body></html>", url

    @staticmethod
    def jump_page_url(html: str) -> str:
        return ""


def test_verify_userhash_live_asks_the_cookie_page_with_that_cookie():
    """真问一次服务端：拿这块饼干去请「饼干」页，看它有没有被弹回登录页。

    ``assume_live_cookies`` 那个 fixture 会把这一步短路掉（免得碰网络），但它一短路，
    「到底问没问、问的是谁」就没人钉了 —— 走哪条路、弹回登录页算不算不认，正是最容易
    悄悄错掉的地方。所以这条用例自己把客户端换成假的，让真实现跑一遍。
    """
    fake = _AskClient()
    # 真函数用 import 拿到的那个名字（模块级常量），**不要**在用例里读
    # `gui.verify_userhash_live`：autouse 的 `assume_live_cookies` 在本用例开工前
    # 就已经把模块属性换成替身了，那时读到的是替身（这一点吃过两次亏）。
    live = real_verify_userhash_live

    assert live(fake, FAKE_USERHASH) is None, "饼干没问题时不该报警"
    assert fake.imported == [FAKE_USERHASH], "验之前得先把这块饼干装进客户端"
    assert fake.urls == [f"{_AskClient.SITE}/Member/User/Cookie/index.html"], fake.urls

    # 掉进登录页 = 饼干不认。
    class _Stale(_AskClient):
        def _request_following_jumps(self, url, **kwargs):
            self.urls.append(url)
            return b"<html></html>", f"{self.SITE}/Member/User/Index/login.html"

    stale = _Stale()
    assert live(stale, FAKE_USERHASH) == gui.BROWSER_STALE_COOKIE_MESSAGE

    # 网络出问题 ≠ 饼干不认：得说清是「没验成」，不能让用户以为要重新登录。
    class _Broken(_AskClient):
        def _request_following_jumps(self, url, **kwargs):
            raise RuntimeError("网断了")

    broken = _Broken()
    note = live(broken, FAKE_USERHASH)
    assert note is not None and note.startswith("没法确认"), note
    assert "网断了" in note, note


def test_the_dialog_asks_the_server_before_calling_it_a_success():
    """对话框那条路上确实接了「验一下」，而且用的是拿到的那个值。

    上面一条钉的是「怎么验」，这条钉的是「接线」：只看源码，产品代码里
    ``_worker`` 必须把这个函数叫起来 —— 接错了线，上面串再多断言也白搭。
    """
    tree = ast.parse(Path(gui.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "verify_userhash_live"
    ]
    assert len(calls) == 1, f"产品代码里应该有且只有一处调用，实际 {len(calls)} 处"
    assert [ast.unparse(arg) for arg in calls[0].args] == ["self.client", "value"], ast.unparse(
        calls[0]
    )


def _note_failure(dialog: gui.BrowserLoginDialog) -> str:
    """没看到「这次用哪个浏览器」时，把后台队里的话也捞出来当断言说明。"""
    leftover: list[str] = []
    while True:
        try:
            kind, payload = dialog._queue.get_nowait()
        except queue.Empty:
            break
        leftover.append(f"{kind}={payload!r}")
    return f"界面上没写出「这次用哪个浏览器」；窗口上的字={dialog.browser_note_var.get()!r}，队里剩下={leftover}"


def test_browser_note_tells_the_user_which_browser_will_open(
    root_window, browser_shim, open_dialog
):
    """窗口上要当场写清「这次用哪个浏览器」—— 用户改了系统默认却仍打到 Edge，多半是这里没看见。"""
    dialog = open_dialog()
    # 等窗口上真的出现那句话再断言。只等「浏览器起来了」是不够的：后台线程把
    # 「这次用谁」投进队列之后，还要主线程那轮 after 轮询把它搬到窗口上。
    assert _wait_for(
        root_window,
        lambda: "Edge" in dialog.browser_note_var.get()
        and "msedge.exe" in dialog.browser_note_var.get(),
    ), _note_failure(dialog)
    note = dialog.browser_note_var.get()
    assert "Edge" in note, note
    assert "msedge.exe" in note, note


def test_explicit_browser_in_the_settings_is_the_one_that_gets_used(
    root_window, install_browser_shim, open_dialog
):
    """设置里手动指定了浏览器就用它 —— 这条以前只对 PDF 导出生效，浏览器登录还在自己挑。"""
    shim = install_browser_shim()
    chosen = open_dialog.set_explicit_browser()  # type: ignore[attr-defined]

    dialog = open_dialog()
    # 同上：等窗口上真的写出「这次用谁」，而不是只等会话连上。
    assert _wait_for(
        root_window,
        lambda: "这次用" in dialog.browser_note_var.get()
        and " 打开" in dialog.browser_note_var.get(),
    ), _note_failure(dialog)
    assert dialog._browser is not None
    assert Path(dialog._browser.info.path) == chosen
    assert f"这次用 {dialog._browser.info.name} 打开" in dialog.browser_note_var.get()
    assert shim.processes, "浏览器进程没有被启动过"


def test_closing_the_window_stops_the_thread_the_session_and_the_browser(
    root_window, browser_shim, open_dialog
):
    """关窗＝取消：线程、CDP 会话、浏览器进程一个都不许留下（验收硬要求）。"""
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    thread = dialog._thread
    process = browser_shim.processes[-1]
    session = _dialog_sessions()[0]
    assert thread is not None and thread.is_alive()

    handler = dialog.protocol("WM_DELETE_WINDOW")
    assert handler, "关窗没有接到取消回调上"
    dialog.tk.call(handler)  # 等于用户点了窗口右上角的关闭按钮

    assert not dialog.winfo_exists()
    assert session.closed, "CDP 会话没关"
    assert all(item.closed for item in _FakeSession.instances), "还有 CDP 会话没关"
    assert process.terminated, "浏览器进程没被结束"
    assert not thread.is_alive(), "后台线程还在跑"
    assert dialog._ui_job is None


def test_pasting_a_cookie_while_the_browser_starts_leaves_no_orphan_process(
    root_window, install_browser_shim, open_dialog, monkeypatch
):
    """兜底粘贴和「正在启动」撞在一起时，也不能留下没人管的浏览器进程。

    这一版把交错**钉死**而不是碰运气：让假 ``Popen`` 在创建进程之前先停住，于是取消
    必然发生在 ``LoginBrowser.process`` 还是 None 的那一瞬 —— 主线程的收尾此时停不掉
    任何东西，只有后台线程「启动完才发现已取消」那条守卫能收掉后来才起来的进程。
    用例还会断言收尾动作确实发生在**后台线程**上：否则进程没起来时 ``all([])`` 恒真，
    这条用例就退化成空过了（旧版正是如此）。
    """
    release = threading.Event()
    shim = install_browser_shim(popen_release=release)
    stops: list[int] = []
    real_stop = browser_login.LoginBrowser.stop
    monkeypatch.setattr(
        browser_login.LoginBrowser,
        "stop",
        lambda self: (stops.append(threading.get_ident()), real_stop(self))[1],
    )
    # 2026-10-01 起粘贴走自家窗口（gui.ask_pasted_cookie），它交出来的已经是
    # 摘好的 userhash（整段 cookie 的解析在 PasteCookieDialog 里，见 test_window.py）；
    # 这条用例只关心「取消时别留孤儿进程」，这里照样用真解析器把整段 cookie 摘一遍。
    monkeypatch.setattr(
        gui, "ask_pasted_cookie", lambda master: browser_login.parse_userhash_input(PASTED_COOKIE)
    )
    dialog = open_dialog()

    assert shim.popen_entered.wait(timeout=10.0), "后台线程没走到起进程那一步"
    main_thread = threading.get_ident()
    # 必须在取消**之前**抓住这条后台线程：_manual_userhash() 末尾的 _shutdown() → _release()
    # 会把 dialog._thread 清成 None，取消之后再读就只能读到 None，join 会整个被跳过。
    thread = dialog._thread
    assert thread is not None, "对话框没有后台线程"

    dialog._manual_userhash()  # 取消：进程还没被创建，收尾停不掉任何东西

    assert dialog.userhash == FAKE_USERHASH
    assert dialog._stop.is_set()
    assert not shim.processes, "取消时进程还不该被拉起来"
    assert _wait_for(root_window, lambda: not dialog.winfo_exists())

    release.set()  # 放行：进程现在才起来，只能靠后台线程的守卫收掉

    assert _wait_for(root_window, lambda: bool(shim.processes), timeout=10.0)
    # 收尾要等的是**后台线程**，不是「轮询到某个条件成立」：机器忙的时候（全套连跑、
    # 刚跑过真机浏览器）Tk 那套 update() 轮询会把后台线程挤到一边，15 秒都可能轮不到它。
    # join 是直接睡在条件变量上，不占 GIL，负载下也不会假红；join 一返回就说明 _worker
    # 已经收完尾，后面那两条断言从此是确定的，不再靠「后台线程跑赢 10ms 轮询」。
    thread.join(timeout=30.0)
    assert not thread.is_alive(), "后台线程没在收尾——进程可能真成了孤儿"
    assert all(process.returncode is not None for process in shim.processes)
    assert any(tid != main_thread for tid in stops), "收尾必须发生在后台线程上"


def test_user_closing_the_browser_window_offers_a_reopen(
    root_window, browser_shim, open_dialog
):
    """用户自己把浏览器关掉：给一句能照做的话，窗口别卡着。"""
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"

    browser_shim.processes[-1].exit(1)

    assert _wait_for(root_window, lambda: "重新打开浏览器" in dialog.status_var.get())
    assert "直接粘贴饼干登录" in dialog.status_var.get()
    assert dialog.winfo_exists()
    assert dialog.userhash is None
    assert str(dialog.retry_button.cget("state")) == "normal"
    dialog._on_cancel()


def test_leaf_cookie_fallback_is_used_when_the_cookie_is_not_there_yet(
    root_window, browser_shim, open_dialog
):
    """登录了但读不到 userhash 时，走「领一块叶子饼干」那条兜底（导航版）。

    v0.13.17 之前这条路是「在页面里 fetch 站点的两个接口」：fetch 走不完站点自己
    那一跳（跳转提示页不执行、第二跳不发），饼干罐里始终没有 userhash，真机上就是
    「用户明明登录好了，程序一路等到超时」。这里钉住新走法：程序自己导航到「饼干」
    页 → 跟着站点跳到「应用」地址 → 再重读饼干罐。
    """
    dialog = open_dialog()
    list_url = browser_login.COOKIE_SITE + browser_login.COOKIE_LIST_PATH
    apply_url = f"{browser_login.COOKIE_SITE}/Member/User/Cookie/switchTo/id/aaa.html"

    def ready() -> bool:
        for session in _dialog_sessions():
            session.pages = {
                list_url: {
                    "url": list_url,
                    "login": False,
                    "jump": "",
                    "kind": "list",
                    "ids": ["aaa"],
                    "href": apply_url,
                }
            }
            # 主站的「应用」走完才 Set-Cookie —— 真机上正是落地那一跳把饼干种进罐里。
            session.cookies = (
                [{"name": "userhash", "value": FAKE_USERHASH}]
                if apply_url in session.navigations
                else []
            )
        return dialog.userhash is not None

    assert _wait_for(root_window, ready), "兜底路径没能取到 userhash"
    session = _dialog_sessions()[0]
    assert list_url in session.navigations, "没自己去「饼干」页（还想靠 fetch？）"
    assert apply_url in session.navigations, "没跟着站点跳到「应用」地址"
    assert dialog.userhash == FAKE_USERHASH
    assert _wait_for(root_window, lambda: not dialog.winfo_exists())


def test_leaf_cookie_does_not_steal_the_page_while_the_login_form_is_open(
    root_window, browser_shim, open_dialog
):
    """用户停在登录页（可能正在输验证码）时，绝不能把他从表单上拽走。

    这条路每几秒就要跑一次；要是它也带导航，用户正打字就会被弹走 —— 比多等一会儿糟。
    """
    dialog = open_dialog()
    login_url = browser_login.LOGIN_URL
    list_url = browser_login.COOKIE_SITE + browser_login.COOKIE_LIST_PATH

    def on_the_form() -> bool:
        for session in _dialog_sessions():
            session.pages = {
                login_url: {
                    "url": login_url,
                    "login": True,
                    "jump": "",
                    "kind": "login",
                    "ids": [],
                }
            }
            session.cookies = []
        return bool(_dialog_sessions()) and bool(dialog.hint_var.get())

    assert _wait_for(root_window, on_the_form), "诊断行没有写出来"
    session = _dialog_sessions()[0]
    assert login_url in session.navigations, "没把标签页带到登录页"
    assert list_url not in session.navigations, "用户还在登录页，程序却把页面拖走了"
    assert "登录" in dialog.hint_var.get()
    dialog._on_cancel()


def test_leaf_cookie_stops_navigating_the_users_tab_after_the_limit(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """迟迟登录不成时，不能每几秒就把用户的标签页拖到「饼干」页去一次。

    上限用完之后只许重读饼干罐（``navigate=False``）；诊断行要一直有话说，
    这样真机上截个图就能看出卡在哪一步。
    """
    monkeypatch.setattr(gui, "BROWSER_LEAF_NAV_LIMIT", 1)
    dialog = open_dialog()
    list_url = browser_login.COOKIE_SITE + browser_login.COOKIE_LIST_PATH

    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.pages = {
        list_url: {
            "url": list_url,
            "login": False,
            "jump": "",
            "kind": "empty",
            "ids": [],
            "rows": 0,
        }
    }
    session.cookies = []

    assert _wait_for(
        root_window, lambda: list_url in session.navigations
    ), "第一次导航都没发生"
    assert _wait_for(root_window, lambda: bool(dialog.hint_var.get())), "诊断行没有写出来"
    navigations = len([url for url in session.navigations if url == list_url])
    assert navigations == 1

    # 再放它跑几轮：只许重读饼干罐，不许再导航。
    reads = len(session.cookie_reads)
    assert _wait_for(
        root_window, lambda: len(session.cookie_reads) > reads + 5, timeout=10.0
    ), "后面几轮连饼干罐都不读了"
    assert len([url for url in session.navigations if url == list_url]) == navigations
    dialog._on_cancel()


def test_waiting_status_says_how_long_and_which_window_counts() -> None:
    """「还在等」那句话：带秒数，并且点明是哪个浏览器窗口里的登录。"""
    text = gui._waiting_status(42)
    assert "42" in text
    assert "这个窗口打开的那个浏览器" in text
    assert "直接粘贴饼干登录" not in text  # 还没到超时，先别急着让人换法子


def test_long_wait_keeps_telling_the_user_how_long_and_where_to_log_in(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """等登录的这段时间界面不能一动不动。

    用户可能在自己平时用的浏览器里登录（程序看不到），也可能以为那句「浏览器
    已经打开了」就是全部提示 —— 界面长时间没有一点变化，看起来就是卡死。
    这里钉住：过了 BROWSER_PROGRESS_SECONDS 就换成带秒数的那句，而且是**替换**
    而不是追加（``("note", …)`` 那条路会越堆越长）。
    """
    monkeypatch.setattr(gui, "BROWSER_PROGRESS_SECONDS", 0.05)
    dialog = open_dialog()

    assert _wait_for(root_window, lambda: "已经等了" in dialog.status_var.get())
    status = dialog.status_var.get()
    assert status.startswith("已经等了"), status
    assert "这个窗口打开的那个浏览器" in status
    dialog._on_cancel()


def test_timeout_message_explains_which_browser_counts(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """等到超时：那句话要能照做 —— 说清是哪个窗口里的登录，并给出粘贴饼干这条路。"""
    monkeypatch.setattr(gui, "BROWSER_LOGIN_TIMEOUT", 0.3)
    dialog = open_dialog()

    assert _wait_for(root_window, lambda: "还没看到登录成功" in dialog.status_var.get())
    status = dialog.status_var.get()
    assert "自己平时用的浏览器里登录" in status
    assert "直接粘贴饼干登录" in status
    assert str(dialog.retry_button.cget("state")) == "normal"
    dialog._on_cancel()


def test_no_browser_installed_gets_a_readable_message(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """机器上没装浏览器：人话提示 + 明确告诉用户还能怎么登录。"""
    monkeypatch.setattr(browser_login, "find_browser", lambda explicit=None, env=None: None)
    dialog = open_dialog()

    assert _wait_for(root_window, lambda: "没找到" in dialog.status_var.get())
    assert "直接粘贴饼干登录" in dialog.status_var.get()
    assert dialog.winfo_exists()
    assert str(dialog.retry_button.cget("state")) == "normal"
    assert not browser_shim.processes
    dialog._on_cancel()


def test_browser_that_dies_at_once_reports_a_readable_error(
    root_window, install_browser_shim, open_dialog
):
    """浏览器起不来：错误要落到对话框上，不能抛成未捕获异常。"""
    install_browser_shim(exit_immediately=True)
    dialog = open_dialog()

    assert _wait_for(root_window, lambda: "打开浏览器失败" in dialog.status_var.get())
    status = dialog.status_var.get()
    assert "刚起来就退出了" in status
    # 每个候选都试过了：提示要说清「还能怎么登录」，别让用户对着死路反复点
    assert "直接粘贴饼干登录" in status
    assert dialog.winfo_exists()
    assert str(dialog.retry_button.cget("state")) == "normal"
    dialog._on_cancel()


def test_start_is_public_and_repeated_calls_open_only_one_browser(
    root_window, browser_shim, open_dialog
):
    """``start()`` 是公开入口：重复调用（含 ``__init__`` 里那次 after）不会开两遍。"""
    dialog = open_dialog()
    assert callable(dialog.start)
    dialog.start()
    dialog.start()

    assert _wait_for(root_window, lambda: "浏览器已经打开了" in dialog.status_var.get())
    _pump(root_window, 0.2)
    assert len(browser_shim.processes) == 1
    # 只开一条属于对话框的会话（库里拽登录页用的那条 HTTP 会话不算）。
    assert len(_dialog_sessions()) == 1
    dialog._on_cancel()


def test_login_dialog_button_opens_the_browser_dialog_and_takes_the_result(
    root_window, monkeypatch, artifacts_dir
):
    """登录窗口上的「用浏览器登录」确实接到子窗口，并把结果带回来。"""
    monkeypatch.setattr(gui.LoginDialog, "_load_form", lambda self: None)  # 不碰网络
    monkeypatch.setattr(gui, "BrowserLoginDialog", _StubBrowserLoginDialog)
    login = gui.LoginDialog(
        root_window, XdaoClient(), AppSettings(_path=Path(artifacts_dir) / "config.json")
    )
    try:
        assert str(login.browser_button.cget("text")) == "用浏览器登录"
        login._open_browser_login()  # 等于用户点了这个按钮
        assert login.userhash == FAKE_USERHASH
        assert not login.winfo_exists()
    finally:
        if login.winfo_exists():
            login.destroy()


class _StubBrowserLoginDialog(tk.Toplevel):
    """替身：一露面就算成功并自己关掉，用来验证接入点而不真开浏览器。"""

    def __init__(self, master, client, settings=None) -> None:
        super().__init__(master)
        self.client = client
        self.settings = settings
        self.userhash: str | None = FAKE_USERHASH
        self.after(10, self.destroy)

    def start(self) -> None:
        pass


def test_errors_while_reading_cookies_land_in_the_dialog(
    root_window, browser_shim, open_dialog
):
    """读饼干出错：提示落到对话框的对话窗上，不能抛成未捕获异常。"""
    dialog = open_dialog()

    def failed() -> bool:
        for session in _dialog_sessions():
            session.read_error = browser_login.BrowserLoginError("调试连接断了")
        return "读取浏览器饼干失败" in dialog.status_var.get()

    assert _wait_for(root_window, failed), "错误没有落到对话框上"
    assert "调试连接断了" in dialog.status_var.get()
    assert dialog.winfo_exists(), "读饼干出错不该把窗口关掉"
    assert str(dialog.retry_button.cget("state")) == "normal"
    dialog._on_cancel()


# ---------- 契约守卫：库长出新要求时，替身要跟着长 ----------


def _session_methods_used_by_library() -> set[str]:
    """扫库源码，找出它在会话对象上调用过的所有方法名。

    只统计形如 ``session.<名字>(...)`` 的调用 —— 这些就是会话对象**必须**提供的
    方法。曾经出过事：库在 ``LoginBrowser.start()`` 里自己开了一条会话并写成
    ``with CDPSession(...)``，替身没有 ``__enter__`` 就整个逃出 ``start()``，
    于是十几条用例一起报「浏览器没起来」，看着像偶发，其实是契约变了。
    """
    tree = ast.parse(Path(browser_login.__file__).read_text(encoding="utf-8"))
    used: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "session"
        ):
            used.add(func.attr)
    return used


def test_fake_session_covers_every_method_the_library_calls_on_a_session():
    """替身要覆盖库在会话上真正调用过的方法，外加上下文管理协议。"""
    used = _session_methods_used_by_library()
    # v0.13.17 起库自己也读饼干罐了（read_userhash_cookie 要重读好几遍），
    # 所以这四个都在库的调用集里；替身少一个，用例就会静默地测不到东西。
    assert {"call", "current_url", "evaluate", "read_cookies"} <= used, used
    missing = sorted(name for name in used if not callable(getattr(_FakeSession, name, None)))
    assert not missing, f"替身缺了库会调用的方法：{missing}"
    assert hasattr(_FakeSession, "__enter__") and hasattr(_FakeSession, "__exit__")


def test_library_does_not_open_its_own_session_while_starting_the_browser(
    monkeypatch, tmp_path
):
    """``LoginBrowser.start()`` 不该自己开 CDP 会话：那是界面层的活（也是那次事故的根因）。"""
    opened: list[str] = []
    monkeypatch.setattr(
        browser_login, "CDPSession", lambda *args, **kwargs: opened.append("opened")
    )
    monkeypatch.setattr(browser_login, "subprocess", _FakeSubprocess())
    browser = browser_login.LoginBrowser(
        browser_login.BrowserInfo(name="Fake", path="fake-browser.exe"),
        Path(tmp_path) / "profile",
    )
    browser.start()
    assert opened == [], "库在启动浏览器时自己开了 CDP 会话，替身会漏接"
    browser.stop()
