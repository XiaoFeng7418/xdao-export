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
    # v0.13.21：领饼干现在要「等站点自己的倒计时页跳完」再「等饼干罐里出现 userhash」，
    # 这两段等待也调小，免得每条走到「应用」的用例都真空等几秒。
    monkeypatch.setattr(browser_login, "NAVIGATE_POLL", 0.01)
    monkeypatch.setattr(browser_login, "JUMP_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(browser_login, "COOKIE_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(browser_login, "COOKIE_WAIT_POLL", 0.01)


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


@pytest.fixture(autouse=True)
def no_real_http_leaf_cookie(monkeypatch):
    """挡住 v0.13.22 新加的那条 HTTP 路：用例一律不许真去连站点。

    它默认说「HTTP 这条路没成、也没什么可说的」（空 detail），于是各条既有用例照旧
    验浏览器那条路的老行为（罐里没有饼干时更是连请求都不会发）。要验 HTTP 路本身的
    用例自己再 ``monkeypatch.setattr`` 覆盖一次。
    """
    monkeypatch.setattr(
        browser_login,
        "apply_leaf_cookie_over_http",
        lambda cookies, **kwargs: browser_login.LeafCookie(None, ""),
    )


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


def test_login_finished_late_still_gets_a_chance_to_apply_the_cookie(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """用户登录得慢一点，程序不能把「真去领饼干」的机会提前用光。

    0.13.18 的真机现场：程序开浏览器时把标签页停在登录页，头两次「领饼干」都在登录页
    上什么都没碰就返回了（「还没登录」），可次数照样被扣掉 —— 等用户登进去，程序已经只
    会重读饼干罐，而 userhash 只有真去「应用」才会被主站种下，于是界面一路等到超时。
    这里钉住：**只有真动过用户的标签页才扣次数**，登录晚了几十秒也还领得到。
    """
    monkeypatch.setattr(gui, "BROWSER_LEAF_NAV_LIMIT", 1)
    dialog = open_dialog()
    login_url = browser_login.LOGIN_URL
    list_url = browser_login.COOKIE_SITE + browser_login.COOKIE_LIST_PATH
    apply_url = f"{browser_login.COOKIE_SITE}/Member/User/Cookie/switchTo/id/aaa.html"
    home_url = f"{browser_login.COOKIE_SITE}/Member/User/Index/index.html"
    login_page = {
        "url": login_url,
        "login": True,
        "jump": "",
        "kind": "login",
        "ids": [],
    }

    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.pages = {login_url: login_page}
    session.cookies = []

    # 用户在登录页上待「好几轮」——真机上这就是他打账号密码的那几十秒。
    assert _wait_for(
        root_window, lambda: len(session.cookie_reads) > 6, timeout=10.0
    ), "轮询没跑起来"
    session = _dialog_sessions()[0]
    assert list_url not in session.navigations, "用户还在登录页，程序却把页面拖走了"

    # 用户登进去了：站点自己把人带到用户首页，标签页地址跟着变（只摆这一次 ——
    # 每轮都去改替身的地址，会和程序自己的导航打架，那验的就不是真实行为了）。
    for item in _dialog_sessions():
        item.pages = {
            login_url: login_page,
            home_url: {
                "url": home_url,
                "login": False,
                "jump": "",
                "kind": "other",
                "ids": [],
            },
            list_url: {
                "url": list_url,
                "login": False,
                "jump": "",
                "kind": "list",
                "ids": ["aaa"],
                "href": apply_url,
            },
        }
        item.current_url_value = home_url

    def gives_the_cookie() -> bool:
        # 主站的「应用」走完才 Set-Cookie —— 真机上正是落地那一跳把饼干种进罐里。
        for item in _dialog_sessions():
            if apply_url in item.navigations:
                item.cookies = [{"name": "userhash", "value": FAKE_USERHASH}]
        return dialog.userhash is not None

    assert _wait_for(root_window, gives_the_cookie, timeout=10.0), "登录晚了就领不到饼干了"
    session = _dialog_sessions()[0]
    assert list_url in session.navigations, "登录成功后没去「饼干」页"
    assert apply_url in session.navigations, "没跟着站点跳到「应用」地址"
    assert dialog.userhash == FAKE_USERHASH


def test_timeout_message_lists_the_steps_the_program_tried(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """超时那句话要按顺序留下试过的几步，不能只留最后一条。

    收尾那条「自动取饼干这条路试过了：浏览器里还是没有 userhash」信息量最小，
    只留它会把更有用的「这个窗口里还没登录」盖掉 —— 0.13.18 的截图上就是这样，
    用户和我们都看不出卡在哪一步。
    """
    monkeypatch.setattr(gui, "BROWSER_LOGIN_TIMEOUT", 1.5)
    dialog = open_dialog()
    login_url = browser_login.LOGIN_URL
    list_url = browser_login.COOKIE_SITE + browser_login.COOKIE_LIST_PATH
    home_url = f"{browser_login.COOKIE_SITE}/Member/User/Index/index.html"

    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
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
    assert _wait_for(
        root_window, lambda: "还没登录" in dialog.hint_var.get(), timeout=5.0
    ), "第一句诊断没出来"

    # 用户登进去了，但这个账号还没领过饼干：只摆这一次地址，别和程序的导航打架。
    for item in _dialog_sessions():
        item.pages = {
            home_url: {
                "url": home_url,
                "login": False,
                "jump": "",
                "kind": "other",
                "ids": [],
            },
            list_url: {
                "url": list_url,
                "login": False,
                "jump": "",
                "kind": "empty",
                "ids": [],
                "rows": 0,
            },
        }
        item.current_url_value = home_url
        item.cookies = []

    assert _wait_for(
        root_window,
        lambda: "没有可以应用的饼干" in dialog.hint_var.get(),
        timeout=5.0,
    ), "第二句诊断没出来"
    assert _wait_for(
        root_window, lambda: "还没看到登录成功" in dialog.status_var.get(), timeout=10.0
    )
    status = dialog.status_var.get()
    assert "①" in status and "②" in status, status
    assert status.index("还没登录") < status.index("没有可以应用的饼干"), status
    assert "程序最后试到的一步" not in status


def _failed_apply_kit() -> tuple[dict[str, str], dict[str, dict]]:
    """一套「登录没问题、但饼干就是领不到」的页面脚本。

    真机上的顺序（用户 m29500 的两张截图）：程序走到「应用」→ 站点回自己的跳转页 →
    程序抢在站点那一跳之前导航去导出页 → 导出页（此时还没有 userhash）把标签页弹回
    登录页。这里就照这个顺序摆：**导出页和登录页共用同一份登录页状态**。
    """
    urls = {
        "login": browser_login.LOGIN_URL,
        "home": f"{browser_login.COOKIE_SITE}/Member/User/Index/index.html",
        "list": browser_login.COOKIE_SITE + browser_login.COOKIE_LIST_PATH,
        "apply": f"{browser_login.COOKIE_SITE}/Member/User/Cookie/switchTo/id/aaa.html",
        "export": f"{browser_login.COOKIE_SITE}/Member/User/Cookie/export/id/aaa.html",
    }
    login_page = {
        "url": urls["login"],
        "login": True,
        "jump": "",
        "kind": "login",
        "ids": [],
    }
    pages = {
        urls["home"]: {
            "url": urls["home"],
            "login": False,
            "jump": "",
            "kind": "other",
            "ids": [],
        },
        urls["list"]: {
            "url": urls["list"],
            "login": False,
            "jump": "",
            "kind": "list",
            "ids": ["aaa"],
            "href": urls["apply"],
        },
        urls["apply"]: {
            "url": urls["apply"],
            "login": False,
            "jump": "",
            "kind": "other",
            "ids": [],
        },
        urls["export"]: login_page,
        urls["login"]: login_page,
    }
    return urls, pages


def _start_with_one_failed_apply(root_window, open_dialog):
    """摆好上面那套脚本，等第一趟「应用」走完（值领不到）。返回 (dialog, session, urls)。"""
    urls, pages = _failed_apply_kit()
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.cookies = []  # 领不到：真机那次是切换成功、但 userhash 没种上
    session.pages = pages
    session.current_url_value = urls["home"]
    assert _wait_for(
        root_window, lambda: urls["apply"] in session.navigations, timeout=10.0
    ), "第一趟「应用」都没走到"
    assert _wait_for(
        root_window, lambda: urls["export"] in session.navigations, timeout=10.0
    ), "没等到导出页那一步"
    return dialog, session, urls


def test_leaf_cookie_keeps_trying_after_the_tab_is_bounced_back_to_login(
    root_window, browser_shim, open_dialog
):
    """被弹回登录页之后还要接着去领饼干，不能就此停手（v0.13.21）。

    0.13.20 的卡法：那一趟导出页导航把标签页弹到登录页，之后每次领饼干都在
    「页面停在登录页」上早退 —— 标签页再也不动，界面上每两秒重复同一句
    「这个窗口里还没登录（页面停在登录页）」，用户看到的就是「切换完饼干就卡住」。
    """
    dialog, session, urls = _start_with_one_failed_apply(root_window, open_dialog)
    # 程序眼里标签页现在正停在登录页上（导出页被弹回登录页的那一份状态）。

    def goes_back_to_the_list() -> bool:
        return len([url for url in session.navigations if url == urls["list"]]) >= 2

    assert _wait_for(
        root_window, goes_back_to_the_list, timeout=10.0
    ), "被弹回登录页之后就不再试着领饼干了"
    dialog._on_cancel()


def test_leaf_cookie_says_so_when_the_apply_budget_is_used_up(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """次数用完、改成只读饼干罐时，界面上要明说（v0.13.21）。

    以前是静默降级：用完就只剩每两秒重复一句「自动取饼干这条路试过了：浏览器里还是
    没有 userhash。」，用户从界面上看不出程序已经不再动浏览器里的标签页了。
    """
    monkeypatch.setattr(gui, "BROWSER_LEAF_NAV_LIMIT", 1)
    dialog, _session, _urls = _start_with_one_failed_apply(root_window, open_dialog)
    assert _wait_for(
        root_window, lambda: "试满" in dialog.hint_var.get(), timeout=10.0
    ), "次数用完了界面上没说"
    hint = dialog.hint_var.get()
    assert "1 次" in hint, hint
    assert "只读饼干罐" in hint, hint
    assert "不再动浏览器里的标签页" in hint, hint
    dialog._on_cancel()


def test_leaf_cookie_budget_is_high_enough_to_survive_a_slow_login() -> None:
    """自动领饼干的名额不能太低（v0.13.21 从 2 提到 6）。

    2 次在几十秒里就用完了，之后只剩「只读饼干罐」—— 用户看到的是「程序忽然再也不动
    我的标签页了」。这条守卫防止有人又把它调小，也钉住那句降级说明的形状。
    """
    assert gui.BROWSER_LEAF_NAV_LIMIT >= 4
    hint = gui.BROWSER_LEAF_CAP_HINT.format(limit=gui.BROWSER_LEAF_NAV_LIMIT)
    assert str(gui.BROWSER_LEAF_NAV_LIMIT) in hint, hint
    assert "只读饼干罐" in hint, hint
    assert "不再动浏览器里的标签页" in hint, hint


def test_try_leaf_cookie_hands_the_waiting_flag_to_the_library() -> None:
    """界面层要把「已经真应用过没有」告诉库（v0.13.21）。

    只有第一趟才该把登录页当免打扰牌（那才是用户可能正在打字）；之后停在登录页
    是标签页被弹回去了，程序得继续去领。
    """
    seen: list[dict] = []

    class _Backend:
        @staticmethod
        def fetch_leaf_cookie(session, *, navigate=True, waiting_for_login=True):
            seen.append({"navigate": navigate, "waiting_for_login": waiting_for_login})
            return browser_login.LeafCookie(None, "", True)

    assert gui.BrowserLoginDialog._try_leaf_cookie(_Backend, object()) == (None, "", True)
    assert seen == [{"navigate": True, "waiting_for_login": True}]

    gui.BrowserLoginDialog._try_leaf_cookie(
        _Backend, object(), navigate=False, waiting_for_login=False
    )
    assert seen[-1] == {"navigate": False, "waiting_for_login": False}


class _HttpLeafBackend:
    """只演 ``_try_leaf_cookie_http`` 需要的那三个接口的替身。"""

    def __init__(self, cookies, *, value=None, detail="", error=None) -> None:
        self.cookies = list(cookies)
        self.value = value
        self.detail = detail
        self.error = error
        self.handed: list[list] = []

    def read_site_cookies(self, session):
        return list(self.cookies)

    def apply_leaf_cookie_over_http(self, cookies):
        self.handed.append(list(cookies))
        if self.error is not None:
            raise self.error
        return browser_login.LeafCookie(self.value, self.detail)

    def looks_like_userhash(self, value):
        return browser_login.looks_like_userhash(value)


def test_try_leaf_cookie_http_leaves_a_bare_anonymous_jar_alone() -> None:
    """用户可能还在输验证码：罐里只有一个匿名会话号时别去连站点。

    未登录时站点只给一个 ``PHPSESSID``；这时候抢先去跑 HTTP 只会白连站点，而且
    它那句「没权限访问」会把更有用的「这个窗口里还没登录」从提示里顶掉。
    """
    backend = _HttpLeafBackend([{"name": "PHPSESSID", "value": "abc123"}])
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(
        backend, object(), may_skip_for_typing=True
    ) == (None, "")
    assert backend.handed == [], "罐里只有匿名会话号，却照样去领了饼干"


def test_try_leaf_cookie_http_tries_anyway_when_the_jar_looks_logged_in() -> None:
    """罐里有登录之后才有的饼干时，「可能还在打字」这个顾虑不成立：照试。

    这条路不碰页面，所以它不会把正在输验证码的人从表单上拽走 —— 用户 m29953 就是
    标签页被弹回登录页、罐里其实还登录着，程序一直在等他把表单再填一遍。
    """
    backend = _HttpLeafBackend(
        [{"name": "PHPSESSID", "value": "abc123"}, {"name": "_uid", "value": "9527"}],
        value=FAKE_USERHASH,
    )
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(
        backend, object(), may_skip_for_typing=True
    ) == (FAKE_USERHASH, "")
    assert backend.handed, "罐里登录着，却没去领饼干"


def test_try_leaf_cookie_http_does_not_connect_while_the_jar_is_empty() -> None:
    """罐里还没有会话饼干（用户一个字都没填）时，一个请求都不该发。"""
    backend = _HttpLeafBackend([])
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object()) == (None, "")
    assert backend.handed == [], "罐里是空的却照样去连了站点"


def test_try_leaf_cookie_http_hands_the_whole_jar_over() -> None:
    """整罐饼干都要交给 HTTP 那条路 —— 站点认的是会话，不只是 userhash。"""
    jar = [
        {"name": "PHPSESSID", "value": "abc123"},
        {"name": "userhash", "value": FAKE_USERHASH},
    ]
    backend = _HttpLeafBackend(jar, value=FAKE_USERHASH)
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object()) == (
        FAKE_USERHASH,
        "",
    )
    assert backend.handed == [jar]


def test_try_leaf_cookie_http_rejects_a_value_that_is_not_a_userhash() -> None:
    """HTTP 那条路回来的东西同样要过 ``looks_like_userhash``，不能照单全收。"""
    backend = _HttpLeafBackend([{"name": "a", "value": "b"}], value="x")
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object()) == (None, "")


def test_try_leaf_cookie_http_turns_a_crash_into_one_readable_line() -> None:
    """这条路是兜底，崩了不算流程错误，但要在界面上留一句人话（要进「试过的几步」）。"""
    backend = _HttpLeafBackend(
        [{"name": "a", "value": "b"}], error=RuntimeError("连接被重置")
    )
    value, detail = gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object())
    assert value is None
    assert "走 HTTP 领饼干时出错" in detail, detail
    assert "连接被重置" in detail, detail


def test_http_leaf_cookie_rescues_a_tab_parked_on_the_login_form(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """v0.13.22 的关键场面：标签页被弹回登录页，罐里其实还登录着。

    用户 m29953/m29954 就卡在这里：程序把「页面停在登录页」当「用户还在打字」，
    于是再也不去领饼干，一直等到超时（浏览器那边还在「饼干切换成功!」上原地跳）。
    新走法：罐里有真会话就直接走 HTTP 领，**一个页面都不动**。
    """
    dialog = open_dialog()
    login_url = browser_login.LOGIN_URL
    handed: list[list] = []

    def fake_http(cookies, **kwargs):
        handed.append(list(cookies))
        return browser_login.LeafCookie(FAKE_USERHASH, "")

    monkeypatch.setattr(browser_login, "apply_leaf_cookie_over_http", fake_http)

    def ready() -> bool:
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
            session.cookies = [
                {"name": "PHPSESSID", "value": "abc123"},
                # 登录之后才多出来的那一块：它说明罐里其实是登录着的（名字不重要，
                # 判据只认「有一个不是 PHPSESSID、值也非空」）。
                {"name": "_uid", "value": "9527"},
            ]
        return dialog.userhash is not None

    assert _wait_for(root_window, ready), "罐里登录着、页面停在登录页时又卡住了"
    session = _dialog_sessions()[0]
    assert handed, "没把罐里的饼干交给 HTTP 那条路"
    aside = [url for url in session.navigations if url != login_url]
    assert aside == [], f"走了 HTTP 却还是动了用户的标签页：{aside}"
    assert dialog.userhash == FAKE_USERHASH
    assert _wait_for(root_window, lambda: not dialog.winfo_exists())


def test_http_leaf_cookie_detail_reaches_the_timeout_message(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """两条路都没成时，HTTP 那条路的原话也要进超时那句话（截图里唯一的线索）。"""
    monkeypatch.setattr(gui, "BROWSER_LOGIN_TIMEOUT", 3.0)
    dialog = open_dialog()
    urls, pages = _failed_apply_kit()
    marker = "服务端说：这块饼干已经过期了"

    monkeypatch.setattr(
        browser_login,
        "apply_leaf_cookie_over_http",
        lambda cookies, **kwargs: browser_login.LeafCookie(None, marker),
    )

    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.cookies = [{"name": "PHPSESSID", "value": "abc123"}]
    session.pages = pages
    session.current_url_value = urls["home"]
    assert _wait_for(
        root_window, lambda: "还没看到登录成功" in dialog.status_var.get(), timeout=15.0
    )
    status = dialog.status_var.get()
    assert marker in status, status
    # 顺序也钉住：HTTP 那条路是**后**试的，它的原话要排在浏览器那条路之后
    # （不然「把原话挪到末尾」这个行为被改掉也没人发现）。
    assert status.index(marker) > status.index("userhash"), status


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
