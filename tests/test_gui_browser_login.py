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
        # v0.13.29：整罐读（不过滤地址那一读）的替身答案，默认空罐 —— 老用例里
        # 「按地址读」就是全部答案，行为不变。
        self.all_cookies: list[dict] = []
        self.read_all_error: Exception | None = None
        # v0.13.31：界面层每轮先重挑标签。替身没有真标签，默认永远报「没换」；
        # 用例想让 retarget 抛异常（演「连接断了」），把它换成 raise 的函数即可。
        self.retarget_error: Exception | None = None
        self.retargets = 0
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

    def retarget(self) -> bool:
        # v0.13.31：界面层每轮先重挑标签再读。替身没有真标签，默认报「没换」；
        # 用例把 retarget_error 设成异常就能演「连接断了、下一轮才救回来」。
        self.retargets += 1
        if self.retarget_error is not None:
            raise self.retarget_error
        return False

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

    def read_all_cookies(self) -> list[dict]:
        if self.read_all_error is not None:
            raise self.read_all_error
        return list(self.all_cookies)

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
    monkeypatch.setattr(gui, "BROWSER_UI_POLL_MS", 20)
    # 领饼干那条 HTTP 路（v0.13.23）不再导航标签页，所以这里只剩「等站点自己的跳转」
    # 那两段等待要调小。
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

    def factory(client_for_dialog=None, log=None) -> gui.BrowserLoginDialog:
        dialog = gui.BrowserLoginDialog(
            root_window,
            client_for_dialog if client_for_dialog is not None else client,
            settings,
            log=log,
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
    # 等窗口上真的写出「这次用谁」，而不是只等会话连上。那句话在 browser.start()
    # **之前**就投了队（gui.py 里先报「用谁」再启动），所以要连进程一起等，
    # 否则机器忙时这里会抢在假 Popen 落账前断言（以前偶发失败就是这个竞态）。
    assert _wait_for(
        root_window,
        lambda: "这次用" in dialog.browser_note_var.get()
        and " 打开" in dialog.browser_note_var.get()
        and bool(shim.processes),
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


def test_leaf_cookie_comes_over_http_without_touching_the_users_tab(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """登录了但读不到 userhash 时走 HTTP 那条路领（v0.13.23），一个标签页都不碰。

    v0.13.17~v0.13.22 是让程序自己导航用户那个标签页去站点「应用」：站点那一跳是页面
    里的 JS 倒计时，程序一导航就把用户的页面留在「饼干切换成功!」那张**永远原地重载**
    的页上（用户看到的就是「一直无限跳转」，m29953/m29954/m30629）。现在浏览器只负责
    让用户把验证码认过去，领饼干交给 ``XdaoClient.apply_cookie`` 那条从 v0.6.1 起就在
    线上跑通的 HTTP 协议。
    """
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.pages = {}
    # 罐里有一个真会话（不是只有匿名会话号）：HTTP 那条路才会真的去试。
    session.cookies = [
        {"name": "PHPSESSID", "value": "abc123"},
        {"name": "memberUserspapapa", "value": "logged-in"},
    ]
    monkeypatch.setattr(
        browser_login,
        "apply_leaf_cookie_over_http",
        lambda cookies, **kwargs: browser_login.LeafCookie(FAKE_USERHASH, ""),
    )
    assert _wait_for(
        root_window, lambda: dialog.userhash == FAKE_USERHASH, timeout=10.0
    ), "HTTP 那条路领到的饼干没被采纳"
    assert not [
        url for url in session.navigations if "/Cookie/" in url
    ], f"程序动了用户的标签页：{session.navigations}"
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
        return (
            bool(_dialog_sessions())
            and "登录" in dialog.hint_var.get()
        )

    assert _wait_for(root_window, on_the_form), "诊断行没有写出来"
    session = _dialog_sessions()[0]
    assert login_url in session.navigations, "没把标签页带到登录页"
    assert list_url not in session.navigations, "用户还在登录页，程序却把页面拖走了"
    assert "登录" in dialog.hint_var.get()
    dialog._on_cancel()


def test_the_dialog_never_navigates_the_users_tab(
    root_window, browser_shim, open_dialog
):
    """迟迟登录不成时也只读饼干罐，一个标签页都不动（v0.13.23）。

    v0.13.21 起是「最多导航 6 次、用完只读罐」——可导航本身就是用户那次「一直无限
    跳转」的来源（站点的倒计时页会把自己重载一遍又一遍，m29953/m29954/m30629），
    所以现在一次都不导航。
    """
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.pages = {}
    session.cookies = []

    reads = len(session.cookie_reads)
    assert _wait_for(
        root_window, lambda: len(session.cookie_reads) > reads + 5, timeout=10.0
    ), "后面几轮连饼干罐都不读了"
    assert not [
        url for url in session.navigations if "/Cookie/" in url
    ], f"程序动了用户的标签页：{session.navigations}"
    # 诊断行是主线程从队列里取出来才写进 hint_var 的，别跟上面那几条断言抢时间
    # （2026-10-02 CI 上真抢过一次：`assert dialog.hint_var.get()` 红在「诊断行没有写出来」）。
    assert _wait_for(
        root_window, lambda: bool(dialog.hint_var.get()), timeout=10.0
    ), "诊断行没有写出来"
    dialog._on_cancel()


def test_login_finished_late_still_gets_a_chance_to_apply_the_cookie(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """用户登录得慢一点，程序不能把「真去领饼干」的机会提前用光。

    0.13.18 的真机现场：程序开浏览器时把标签页停在登录页，头几次「领饼干」都在登录页
    上什么都没碰就返回了（「还没登录」）—— 等用户登进去，程序却已经不再去领了，而
    userhash 只有真去「应用」才会被主站种下，于是界面一路等到超时。
    v0.13.23 起程序干脆一次都不导航用户的标签页，登录晚了几十秒也照样领得到：领饼干
    的正事走 HTTP（`XdaoClient.apply_cookie` 的老协议），浏览器只管让用户认验证码。
    """
    dialog = open_dialog()
    login_url = browser_login.LOGIN_URL
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
        root_window, lambda: bool(dialog.hint_var.get()), timeout=10.0
    ), "诊断行没写出来"
    assert _wait_for(
        root_window, lambda: len(session.cookie_reads) > 6, timeout=10.0
    ), "轮询没跑起来"
    session = _dialog_sessions()[0]
    assert not [
        url for url in session.navigations if "/Cookie/" in url
    ], "用户还在登录页，程序却把页面拖走了"
    assert "还没登录" in dialog.hint_var.get()

    # 用户登进去了：站点把会话饼干种进罐里（只摆这一次 —— 每轮都去改替身的地址，
    # 会和程序自己的判断打架，那验的就不是真实行为了）。
    for item in _dialog_sessions():
        item.pages = {login_url: login_page}
        item.cookies = [
            {"name": "PHPSESSID", "value": "abc123"},
            {"name": "memberUserspapapa", "value": "logged-in"},
        ]
        item.current_url_value = login_url
    monkeypatch.setattr(
        browser_login,
        "apply_leaf_cookie_over_http",
        lambda cookies, **kwargs: browser_login.LeafCookie(FAKE_USERHASH, ""),
    )

    assert _wait_for(
        root_window, lambda: dialog.userhash == FAKE_USERHASH, timeout=10.0
    ), "登录晚了就领不到饼干了"
    assert not [
        url for url in _dialog_sessions()[0].navigations if "/Cookie/" in url
    ], "登录成功之后程序又去动用户的标签页了"


def test_timeout_message_lists_the_steps_the_program_tried(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """超时那句话要按顺序留下试过的几步，不能只留最后一条。

    v0.13.23 起每轮会先说一句「罐子里有哪些饼干」（状态），再说 CDP 那句结论 ——
    收尾的结论（「自动取饼干这条路试过了…」）信息量最小，只留它会把状态那一句盖掉，
    用户和我们都看不出卡在哪一步。
    """
    monkeypatch.setattr(gui, "BROWSER_LOGIN_TIMEOUT", 1.5)
    # 这条钉的是超时那句的形状；「到点续等」由 v0.13.31 那组用例单独钉，这里关掉免把用例拖长。
    monkeypatch.setattr(gui, "BROWSER_LOGIN_EXTRA_ROUNDS", 0)
    dialog = open_dialog()
    login_url = browser_login.LOGIN_URL

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
    # 只有匿名会话号：HTTP 那条路会跳过（用户可能正在打验证码），于是留下的两步
    # 正好是「罐子里有哪些饼干」和 CDP 那句「还没登录」。
    session.cookies = [{"name": "PHPSESSID", "value": "abc123"}]
    assert _wait_for(
        root_window, lambda: "还没登录" in dialog.hint_var.get(), timeout=5.0
    ), "第一句诊断没出来"

    assert _wait_for(
        root_window, lambda: "还没看到登录成功" in dialog.status_var.get(), timeout=10.0
    )
    status = dialog.status_var.get()
    assert "①" in status and "②" in status, status
    # 先状态、后结论：程序每一轮就是这个顺序（先报罐子，再报 CDP 的结论）。
    assert status.index("浏览器里的饼干") < status.index("还没登录"), status
    assert "程序最后试到的一步" not in status


def test_the_dialog_keeps_working_after_the_http_path_fails(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """HTTP 那条路也没领到时程序还得继续试（v0.13.23）：读罐、报诊断、不碰页面。"""
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.pages = {}
    session.cookies = [
        {"name": "PHPSESSID", "value": "abc123"},
        {"name": "memberUserspapapa", "value": "logged-in"},
    ]
    monkeypatch.setattr(
        browser_login,
        "apply_leaf_cookie_over_http",
        lambda cookies, **kwargs: browser_login.LeafCookie(
            None, "服务端说：这块饼干已经过期了"
        ),
    )
    assert _wait_for(
        root_window, lambda: "服务端说" in dialog.http_var.get(), timeout=10.0
    ), "HTTP 那条路说了什么，界面上一句都没有"
    reads = len(session.cookie_reads)
    assert _wait_for(
        root_window, lambda: len(session.cookie_reads) > reads + 3, timeout=10.0
    ), "失败之后就不再试了"
    assert not [
        url for url in session.navigations if "/Cookie/" in url
    ], f"程序动了用户的标签页：{session.navigations}"
    dialog._on_cancel()


# ---------- v0.13.31：挂错标签、一次读挂、超时续等、验饼干换口音 ----------


def test_every_round_reselects_the_tab_before_reading_it(
    root_window, browser_shim, open_dialog
):
    """每一轮读之前先 retarget 重挑标签（v0.13.31）。

    真机 m34935：用户关掉登录那个标签、另开一个去饼干页登录，程序却盯着
    connect() 那一刻挂上的旧标签一路报「页面停在登录页」。从这一版起每轮
    重新挑一次，挂错了当场换过去。
    """
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    assert _wait_for(root_window, lambda: session.retargets > 3, timeout=10.0), (
        "轮询没在每轮开头重挑标签"
    )
    dialog._on_cancel()


def test_every_round_pins_the_banner_to_the_watched_tab(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """每一轮都往被盯的标签插「程序正在看这个窗口」的横幅（v0.13.33）。

    真机 m35762/m35800：用户在自己看的窗口里登录、点应用，程序却看着另一个
    窗口的罐 —— 两个窗口长得一模一样，光靠文字说不清谁是谁。从这一版起横幅
    每轮重插一次（导航会把页面整个换掉），插的就是这一轮 retarget 后真正在读
    的那个 session，指哪看哪。
    """
    banners: list[object] = []
    monkeypatch.setattr(
        browser_login,
        "ensure_watch_banner",
        lambda session: banners.append(session) or "added",
    )
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    assert _wait_for(root_window, lambda: len(banners) >= 3, timeout=10.0), (
        "横幅没有每轮都插"
    )
    assert all(item is session for item in banners), "横幅没钉在这一轮真正在读的标签上"
    dialog._on_cancel()


def test_a_leaf_round_without_userhash_writes_the_forensics_line(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """每一轮领饼干领空了，就把「读罐对账」写进运行日志（v0.13.34）。

    真机 m36307：F12 看得见 userhash、程序只报一句合并结果「浏览器里还是没有
    userhash」，谁也分不出漏在哪一读。从这一版起，无果的领饼干轮会把四路读法
    各自看见的名字清单并排写进日志（值绝不写）—— 下次一张日志截图就能定位。
    通道是**只进日志**：对话框已经三行话了，不再给它添第四行。
    """
    calls: list[object] = []

    def fake_forensics(session):
        calls.append(session)
        return "挂=https://www.nmbxd1.com/x｜按地址读=PHPSESSID｜整罐读=PHPSESSID｜页面JS=空｜合并=PHPSESSID"

    monkeypatch.setattr(browser_login, "jar_forensics", fake_forensics)
    logged: list[str] = []
    dialog = open_dialog(log=logged.append)
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.cookies = [{"name": "PHPSESSID", "value": "abc123"}]
    assert _wait_for(
        root_window,
        lambda: any(line.startswith("读罐对账：") for line in logged),
        timeout=10.0,
    ), f"对账行没进运行日志：{logged}"
    assert all(item is session for item in calls), "对账读的不是这一轮真正挂着的标签"
    line = next(text for text in logged if text.startswith("读罐对账："))
    assert "PHPSESSID" in line
    assert "abc123" not in line, "对账行漏了饼干值"
    assert "挂=" not in dialog.http_var.get(), "对账不该挤进 HTTP 那一行"
    # 名单没变就只写一次：十分钟的等待不该刷出二十行同样的话。
    _pump(root_window, 0.6)
    assert sum(1 for text in logged if text.startswith("读罐对账：")) == 1, logged
    dialog._on_cancel()


def test_one_flaky_read_retries_instead_of_killing_the_wait(
    root_window, browser_shim, open_dialog
):
    """读挂一下不算挂：下一轮 retarget 会重连，等待必须继续（v0.13.31）。

    以前（v0.13.30 及更早）循环里任何异常都立刻认输，对话框当场写「读取浏览器
    饼干失败」—— 而用户正关标签、重开页面的那一下 CDP 就是会抽风。这里演一次
    抽风：连着两次读挂之后放开替身，程序要能自己缓过来并读到饼干。
    """
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.read_error = browser_login.BrowserLoginError("调试连接断了（演一下）")

    def recovered() -> bool:
        # 挂够两次就放开（认输上限是 4 次，这里必须够不着）。
        if len(session.cookie_reads) >= 2:
            session.read_error = None
            session.cookies = [{"name": "userhash", "value": FAKE_USERHASH}]
        return dialog.userhash == FAKE_USERHASH

    assert _wait_for(root_window, recovered, timeout=10.0), "一次读挂就把等待掐死了"
    assert session.retargets >= 2, "缓过来之前没重挑过标签"
    assert _wait_for(root_window, lambda: not dialog.winfo_exists())


def test_the_live_check_speaks_with_the_browsers_own_user_agent(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """验饼干算不算数之前，主客户端先换成浏览器自报的 UA（v0.13.31）。

    站点会把「同一罐饼干、另一张嘴」的请求弹回登录页（m34394），而主客户端
    写死的 UA 正是另一张嘴 —— 不换的话，用户刚点「应用」挣来的**新**饼干也会被
    验成「旧饼干不认」（m34935 截图里那句冤案就是这么来的）。
    """
    seen: list[str | None] = []

    def spy(client, value: str) -> str | None:
        seen.append(client.user_agent)
        return None

    monkeypatch.setattr(gui, "verify_userhash_live", spy)
    dialog = open_dialog()

    def ready() -> bool:
        for session in _dialog_sessions():
            # navigator.userAgent 落在替身的兜底返回值上（见 _FakeSession.evaluate）。
            session.evaluate_value = "Edg/140.0"
            session.cookies = [{"name": "userhash", "value": FAKE_USERHASH}]
        return dialog.userhash is not None

    assert _wait_for(root_window, ready), "没能从浏览器读到 userhash"
    assert seen, "没验过饼干"
    assert set(seen) == {"Edg/140.0"}, f"验饼干时客户端的 UA 不是浏览器那一张：{seen}"


def test_the_wait_extends_itself_while_the_browser_is_still_open(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """到点了、浏览器还开着：自动续等，续到上限为止才报超时（v0.13.31）。

    m34935 的抱怨就是「我登录了它已经不看了」—— 五分钟到点时用户还在打密码，
    程序直接宣布超时撒手。这里把一轮压到 0.6 秒、续两轮，钉住：报超时那句话
    出现之前，程序至少多盯了整两轮没撒手。
    """
    monkeypatch.setattr(gui, "BROWSER_LOGIN_TIMEOUT", 0.6)
    monkeypatch.setattr(gui, "BROWSER_LOGIN_EXTRA_ROUNDS", 2)
    dialog = open_dialog()
    started = time.monotonic()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    assert _wait_for(
        root_window,
        lambda: "还没看到登录成功" in dialog.status_var.get(),
        timeout=15.0,
    ), "续等用完也没报超时"
    waited = time.monotonic() - started
    # 0.6 秒 ×（首轮 + 续两轮）≈ 1.8 秒：明显超过一轮，又没越过上限。
    assert waited > 3 * 0.6, f"没续等，只等了 {waited:.1f} 秒就撒手"
    assert waited < 4 * 0.6 + 1.5, f"续等越过了上限：{waited:.1f} 秒"


def test_the_diagnosis_joins_what_the_program_learned() -> None:
    """失败原因要带上「罐子里有哪些饼干」和「HTTP 那条路的原话」（v0.13.23）。

    用户报问题贴的是运行日志，这两样是「到底卡在哪一步」唯一的书面依据：m30629 那次
    日志里只有一句「浏览器窗口已经关掉了，还没取到饼干」，谁也没法查。
    """
    dialog = object.__new__(gui.BrowserLoginDialog)  # 只验拼句子，不开窗口
    dialog._jar_note = ""  # `__init__` 里的初值；这条用例绕过了它，得自己摆上
    dialog._http_note = ""
    assert dialog._diagnosis() == ""
    dialog._jar_note = "浏览器里的饼干：PHPSESSID（还没有 userhash）。"
    assert dialog._diagnosis() == "（浏览器里的饼干：PHPSESSID（还没有 userhash）。）"
    dialog._http_note = "服务端说：这块饼干已经过期了"
    assert dialog._diagnosis() == (
        "（浏览器里的饼干：PHPSESSID（还没有 userhash）。；"
        "领饼干那条路：服务端说：这块饼干已经过期了）"
    )


def test_the_failure_reason_carries_the_diagnosis(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """失败原因（`_open_browser_login` 进运行日志的那一句）要带上诊断（v0.13.23）。

    m30629 那次日志里只有「浏览器窗口已经关掉了，还没取到饼干」：罐子里有什么、HTTP
    那条路说了什么，一个字都没有，远程根本查不动。这里钉住「日志那一句」的形状。
    """
    dialog = open_dialog()
    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.cookies = [
        {"name": "PHPSESSID", "value": "abc123"},
        {"name": "memberUserspapapa", "value": "logged-in"},
    ]
    monkeypatch.setattr(
        browser_login,
        "apply_leaf_cookie_over_http",
        lambda cookies, **kwargs: browser_login.LeafCookie(
            None, "服务端说：这块饼干已经过期了"
        ),
    )
    assert _wait_for(
        root_window, lambda: dialog._http_note != "", timeout=10.0
    ), "HTTP 那条路还没留下说法"

    # 现在让读饼干彻底断掉：收尾那句 failure 就是会被写进运行日志的那一句。
    for item in _dialog_sessions():
        item.read_error = browser_login.BrowserLoginError("调试连接断了")

    assert _wait_for(root_window, lambda: bool(dialog.failure), timeout=10.0), "没写 failure"
    failure = dialog.failure
    assert "调试连接断了" in failure, failure
    assert "浏览器里的饼干" in failure, failure
    # v0.13.30：这一行里此刻还带着页面内 fetch 那句话（两条路都败要看全貌），
    # 所以钉「标签在」+「站点原话在」，不钉连写。
    assert "领饼干那条路：" in failure, failure
    assert "服务端说：这块饼干已经过期了" in failure, failure


def test_the_jar_summary_says_which_cookies_are_in_the_browser() -> None:
    """罐子诊断三种情形都要有话说（v0.13.23）：读不到 / 空的 / 有会话饼干。

    第四种情形是 v0.13.24 补的：罐里**已经有 userhash** 时不能再报「还没有 userhash」。
    真机上就是这句话把我带偏过 —— 日志里看着像没读到饼干，其实读到了、只是站点不认
    （m31364：浏览器里明明登着，程序却说罐里没有 userhash）。
    """

    class _Backend:
        def __init__(self, cookies=None, error=None) -> None:
            self.cookies = cookies or []
            self.error = error

        def read_site_cookies(self, session):
            if self.error is not None:
                raise self.error
            return self.cookies

        summarize_cookies = staticmethod(browser_login.summarize_cookies)
        userhash_from_cookies = staticmethod(browser_login.userhash_from_cookies)

    assert gui.BrowserLoginDialog._jar_summary(
        _Backend(error=RuntimeError("连接断了")), object()
    ) == "读不到浏览器里的饼干：连接断了"
    assert gui.BrowserLoginDialog._jar_summary(_Backend(), object()) == (
        "浏览器里现在没有任何属于 X 岛的饼干（还没登录过）。"
    )
    assert gui.BrowserLoginDialog._jar_summary(
        _Backend([{"name": "PHPSESSID", "value": "x"}]), object()
    ) == "浏览器里的饼干：PHPSESSID（还没有 userhash）。"
    assert gui.BrowserLoginDialog._jar_summary(
        _Backend([{"name": "userhash", "value": FAKE_USERHASH}]), object()
    ) == "浏览器里的饼干：userhash（里面有 userhash）。"


def test_the_jar_summary_points_out_cookies_with_empty_values() -> None:
    """空值的饼干要点名（v0.13.26）。

    为什么：真机上站点把 ``memberUserspapapa`` 写成了空值，罐子看着「登录过了」、其实
    一个真会话都没有；用户看名单分不出「有会话」和「只有空壳」，而程序原来还会据此
    判断「算不算登录着」（m32057 那张截图里报的正是「PHPSESSID、memberUserspapapa」）。
    """

    class _Backend:
        def read_site_cookies(self, session):
            return [
                {"name": "PHPSESSID", "value": "abc"},
                {"name": "memberUserspapapa", "value": ""},
            ]

        summarize_cookies = staticmethod(browser_login.summarize_cookies)
        userhash_from_cookies = staticmethod(browser_login.userhash_from_cookies)

    text = gui.BrowserLoginDialog._jar_summary(_Backend(), object())
    assert "其中有 1 块是空值：memberUserspapapa" in text, text
    assert "还没有 userhash" in text, text


def test_the_jar_summary_keeps_quiet_when_every_cookie_has_a_value() -> None:
    """全都带值的罐子不该多那句「有几块是空值」—— 否则每份日志都多一句废话。"""

    class _Backend:
        def read_site_cookies(self, session):
            return [{"name": "PHPSESSID", "value": "abc"}, {"name": "_uid", "value": "7"}]

        summarize_cookies = staticmethod(browser_login.summarize_cookies)
        userhash_from_cookies = staticmethod(browser_login.userhash_from_cookies)

    text = gui.BrowserLoginDialog._jar_summary(_Backend(), object())
    assert "空值" not in text, text


def test_the_jar_summary_says_when_userhash_is_present_but_implausible() -> None:
    """「有 userhash 但值的形状不像」要和「根本没有」分开说（v0.13.29）。

    以前两种都写成「还没有 userhash」：名单里明明列着 userhash 却这么报，用户截图
    里自相矛盾 yet 查不出程序到底看见了什么。这里只写形状、不写值本身。
    """

    class _Backend:
        def read_site_cookies(self, session):
            return [{"name": "userhash", "value": "ab", "domain": ".nmbxd1.com"}]

        summarize_cookies = staticmethod(browser_login.summarize_cookies)
        userhash_from_cookies = staticmethod(browser_login.userhash_from_cookies)
        named_userhash_entries = staticmethod(browser_login.named_userhash_entries)

    text = gui.BrowserLoginDialog._jar_summary(_Backend(), object())
    assert "有 userhash 但值的形状不像" in text, text
    assert "2 个字符" in text, text
    assert "还没有 userhash" not in text, text


def test_read_userhash_falls_back_to_the_whole_jar_and_says_so() -> None:
    """按地址那读漏了 userhash 时：整罐读补上，并把漏在哪说出来（v0.13.29）。

    真机上的情形（2026-10-02 四张截图）：对话框名单里从头到尾没有 userhash，
    同一个浏览器的 F12 Application 面板里却一直有 —— ``Network.getCookies``
    的地址过滤把它滤掉了。补上之后界面要能说出「是整罐读补上的、藏在哪个域
    哪条路径」，下次真机报告才有得查。
    """

    class _Session:
        def __init__(self) -> None:
            self.url_reads = 0
            self.all_reads = 0

        def current_url(self) -> str:
            return browser_login.LOGIN_URL

        def read_cookies(self, urls=None):
            self.url_reads += 1
            return [
                {"name": "memberUserspapapa", "value": "G%C8%8D", "domain": ".nmbxd1.com"}
            ]

        def read_all_cookies(self):
            self.all_reads += 1
            return [
                {"name": "memberUserspapapa", "value": "G%C8%8D", "domain": ".nmbxd1.com"},
                {
                    "name": "userhash",
                    "value": "D-9691%04%02ab",
                    "domain": "www.nmbxd1.com",
                    "path": "/Member",
                },
            ]

    session = _Session()
    value, note = gui.BrowserLoginDialog._read_userhash(browser_login, session)
    assert value == "D-9691%04%02ab"
    assert "整罐读补上" in note and "www.nmbxd1.com" in note and "/Member" in note, note
    assert session.all_reads >= 1, "漏了就该去问整罐"

    # 按地址就能读到时：整罐读一次都不许碰，诊断也留空。
    class _Direct(_Session):
        def read_cookies(self, urls=None):
            self.url_reads += 1
            return [{"name": "userhash", "value": "ABCDEF12", "domain": ".nmbxd1.com"}]

    direct = _Direct()
    value, note = gui.BrowserLoginDialog._read_userhash(browser_login, direct)
    assert value == "ABCDEF12"
    assert note == ""
    assert direct.all_reads == 0


def test_the_http_verdict_gets_its_own_line_and_the_log(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """「领饼干那条路」的结论要单独占一行，并当场写进运行日志（v0.13.26）。

    为什么：m32057 那张截图里，对话框只有一行饼干名单、运行日志里只有「就绪」和
    「已是最新版本」—— 整轮登录没留下任何可查的东西，连那条 HTTP 路跑没跑都说不清。
    """
    logged: list[str] = []
    dialog = open_dialog(log=logged.append)
    home = f"{browser_login.COOKIE_SITE}/Member/User/Index/index.html"
    verdict = "站点说：这个账号还没有可用的饼干"

    def fake_http(cookies, **kwargs):
        return browser_login.LeafCookie(None, verdict)

    monkeypatch.setattr(browser_login, "apply_leaf_cookie_over_http", fake_http)

    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.cookies = [{"name": "PHPSESSID", "value": "abc123"}]
    session.pages = {home: {"url": home, "login": False, "jump": "", "kind": "other", "ids": []}}
    session.current_url_value = home

    assert _wait_for(
        root_window, lambda: verdict in dialog.http_var.get(), timeout=10.0
    ), f"那条路的结论没进它自己那一行：{dialog.http_var.get()!r}"
    assert dialog.http_var.get().startswith("领饼干那条路："), dialog.http_var.get()
    assert any(verdict in line for line in logged), logged


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
    """只演 ``_try_leaf_cookie_http`` 需要的那几个接口的替身。

    v0.13.30：``apply_leaf_cookie_over_http`` 多收一个 ``user_agent``（界面层从浏览器
    读来再传下去），这里记下它好断言；``apply_leaf_cookie_in_browser`` / ``read_user_agent``
    **默认不给** —— 老替身没有这两面时界面层必须照跑（getattr 守卫），由用例自愿装上。
    """

    def __init__(self, cookies, *, value=None, detail="", error=None) -> None:
        self.cookies = list(cookies)
        self.value = value
        self.detail = detail
        self.error = error
        self.handed: list[list] = []
        self.user_agents: list[str] = []

    def read_site_cookies(self, session):
        return list(self.cookies)

    def apply_leaf_cookie_over_http(self, cookies, *, user_agent=""):
        self.handed.append(list(cookies))
        self.user_agents.append(user_agent)
        if self.error is not None:
            raise self.error
        return browser_login.LeafCookie(self.value, self.detail)

    def looks_like_userhash(self, value):
        return browser_login.looks_like_userhash(value)


def test_try_leaf_cookie_http_tries_even_a_bare_anonymous_jar() -> None:
    """罐里只有一个匿名会话号时**也要试**（v0.13.26）。

    v0.13.24/v0.13.25 在这里会跳过：判断依据是「罐里除了 PHPSESSID 还有没有别的非空
    饼干」。真机上站点把 ``memberUserspapapa`` 写成空值时，罐子看着「登录过了」、其实
    一个真会话都没有 —— 判错的代价是整轮一次都不试，界面上只剩「还没有 userhash」，
    用户和我都看不出这条路到底跑没跑（m32057）。现在不猜了：照试，只是按时间节流，
    试过什么一律留痕。
    """
    backend = _HttpLeafBackend([{"name": "PHPSESSID", "value": "abc123"}], detail="没权限")
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object()) == (None, "没权限")
    assert backend.handed, "罐里只有匿名会话号，程序却一次都没试"


def test_try_leaf_cookie_http_tries_anyway_when_the_jar_looks_logged_in() -> None:
    """罐里有登录之后才有的饼干时，照试（这条路不碰页面，不会把人从表单上拽走）。"""
    backend = _HttpLeafBackend(
        [{"name": "PHPSESSID", "value": "abc123"}, {"name": "_uid", "value": "9527"}],
        value=FAKE_USERHASH,
    )
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(
        backend, object()
    ) == (FAKE_USERHASH, "")
    assert backend.handed, "罐里登录着，却没去领饼干"


def test_try_leaf_cookie_http_does_not_connect_while_the_jar_is_empty() -> None:
    """罐里还没有会话饼干（用户一个字都没填）时，一个请求都不该发。

    没得试也要留一句书面记录（v0.13.25）：以前这里返回空字符串，于是「这条路没跑」
    在日志里一个字都不留，用户报「拿不到 userhash」时看不出程序试没试。
    """
    backend = _HttpLeafBackend([])
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object()) == (
        None,
        gui.BROWSER_HTTP_NO_COOKIES_NOTE,
    )
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
    """HTTP 那条路回来的东西同样要过 ``looks_like_userhash``，不能照单全收。

    而且被挡下时要**说得出话**（v0.13.25）：以前这里静默返回空 detail，界面上只剩
    「还没有 userhash」，用户和我都看不出程序到底接住过什么（m31725：用户自己在站点里
    看饼干列表一切正常，程序却一个字都不说）。只写值的形状，不写值本身 —— 那是凭据。
    """
    secret = "这是一句人话，不是一块饼干"
    backend = _HttpLeafBackend([{"name": "a", "value": "b"}], value=secret)
    value, detail = gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object())
    assert value is None
    assert "形状不像 userhash" in detail, detail
    assert "个字符" in detail, detail
    assert secret not in detail, f"诊断里把值本身写出来了：{detail}"


def test_try_leaf_cookie_http_accepts_a_value_with_high_bytes() -> None:
    """高位字节/替换字符不再一票否决（v0.13.25）：真假交给服务端去判。

    真机上站点从导出页回给我们的值可能带着二进制字节解出来的字符，老粗筛要求
    「全是可打印 ASCII」，于是把真值判成「不像」—— 用户看到的就是「程序什么都不说」。
    """
    value = "D-\x91\x04\x02\x12\xf8\xb8E9"
    backend = _HttpLeafBackend([{"name": "PHPSESSID", "value": "x"}], value=value)
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object()) == (value, "")


def test_try_leaf_cookie_http_admits_when_the_site_says_nothing() -> None:
    """站点既没给值也没给原话时，也要写一句「没取到值」而不是静默（v0.13.25）。"""
    backend = _HttpLeafBackend([{"name": "PHPSESSID", "value": "x"}])
    assert gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object()) == (
        None,
        "HTTP 那条路没取到值，站点也没说为什么。",
    )


def test_describe_value_shape_never_prints_the_value() -> None:
    """形状描述只报「几个字符、几个非 ASCII、几个百分号、开头是什么」。"""
    text = gui._describe_value_shape("D-%91%04%02%12")
    assert "个字符" in text and "个非 ASCII" in text and "个百分号" in text
    assert "D-" not in text, f"形状描述里漏出了值本身：{text}"
    assert gui._describe_value_shape("") == "0 个字符，0 个非 ASCII，0 个百分号，开头是非字母数字"


def test_jar_signature_notices_changes_and_ignores_the_order() -> None:
    """罐头指纹：只看「变了没有」，与饼干在列表里的先后无关（v0.13.25）。"""
    one = [{"name": "PHPSESSID", "value": "a"}, {"name": "_uid", "value": "b"}]
    same_the_other_way_round = [
        {"name": "_uid", "value": "b"},
        {"name": "PHPSESSID", "value": "a"},
    ]
    assert gui._jar_signature(one) == gui._jar_signature(same_the_other_way_round)
    assert gui._jar_signature([]) == ()
    assert gui._jar_signature(one) != gui._jar_signature(
        [{"name": "PHPSESSID", "value": "a"}, {"name": "_uid", "value": "c"}]
    )
    assert gui._jar_signature(one) != gui._jar_signature(
        [{"name": "PHPSESSID", "value": "a"}, {"name": "_uid", "value": "b"}, {"name": "x", "value": "y"}]
    )
    # 坏数据不该把它弄崩：不是字典的条目直接跳过。
    assert gui._jar_signature([None, "x", {"name": "a", "value": "b"}]) == (("a", "b"),)


def test_try_leaf_cookie_http_turns_a_crash_into_one_readable_line() -> None:
    """这条路是兜底，崩了不算流程错误，但要在界面上留一句人话（要进「试过的几步」）。"""
    backend = _HttpLeafBackend(
        [{"name": "a", "value": "b"}], error=RuntimeError("连接被重置")
    )
    value, detail = gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object())
    assert value is None
    assert "走 HTTP 领饼干时出错" in detail, detail
    assert "连接被重置" in detail, detail


class _PageFetchBackend(_HttpLeafBackend):
    """装上 v0.13.30 两张新面的替身：页面内 fetch 领饼干 + 浏览器自报 UA。

    ``_HttpLeafBackend`` 故意没有这两面，由上面那组用例钉住「缺了就完整退回
    v0.13.29」；这一组才演新链路本身。
    """

    def __init__(
        self,
        cookies,
        *,
        page_value=None,
        page_detail="",
        page_error=None,
        user_agent="",
        **kwargs,
    ) -> None:
        super().__init__(cookies, **kwargs)
        self.page_value = page_value
        self.page_detail = page_detail
        self.page_error = page_error
        self.user_agent = user_agent
        self.page_calls = 0

    def read_user_agent(self, session):
        return self.user_agent

    def apply_leaf_cookie_in_browser(self, session):
        self.page_calls += 1
        if self.page_error is not None:
            raise self.page_error
        return browser_login.LeafCookie(self.page_value, self.page_detail)


def test_page_fetch_wins_before_the_http_replay_is_bothered() -> None:
    """第一条路（页面内 fetch）拿到值，HTTP 重放**一趟都不许跑**。

    顺序本身就是这条版本的意义（真机 m34394/m34396）：重放被弹回登录页而浏览器里回帖
    成功 —— 差的只是客户端长相。fetch 由浏览器自己发，身份天然全等，先问它。
    """
    jar = [{"name": "PHPSESSID", "value": "abc", "domain": ".nmbxd1.com"}]
    backend = _PageFetchBackend(jar, page_value=FAKE_USERHASH, user_agent="Edg/999")
    value, note = gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object())
    assert value == FAKE_USERHASH
    assert note == ""
    assert backend.page_calls == 1
    assert backend.handed == [], "fetch 都拿到值了，不许再跑重放"


def test_page_fetch_failure_falls_back_to_http_replay_with_the_browsers_ua() -> None:
    """fetch 没成 → 回落 HTTP 重放，并且把浏览器自报的 UA 一路带下去。"""
    jar = [{"name": "PHPSESSID", "value": "abc", "domain": ".nmbxd1.com"}]
    ua = "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Edg/124.0.0.0"
    backend = _PageFetchBackend(
        jar,
        page_detail="在浏览器页面里问「饼干」列表也被弹回了登录页",
        value=FAKE_USERHASH,
        user_agent=ua,
    )
    value, note = gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object())
    assert value == FAKE_USERHASH
    assert note == "", "第二条路救回来了，前一条路的失败话不必再讲"
    assert backend.user_agents == [ua]


def test_both_leaf_paths_failing_shows_the_whole_picture() -> None:
    """两条路都败 → 界面上必须同时看得见两条路各自的原话（v0.13.30）。

    只留一条的话，「fetch 也被弹回登录页」（会话真死了）和「fetch 没问动、重放被弹回」
    （多半是客户端长相）这两种完全不同的诊断就分不开了 —— m34394 正是靠这组对照定位的。
    """
    jar = [{"name": "PHPSESSID", "value": "abc", "domain": ".nmbxd1.com"}]
    backend = _PageFetchBackend(
        jar,
        page_detail="在浏览器页面里问「饼干」列表没问动（页面可能正在换地址）。",
        detail="登录后没能进入用户系统（X 岛把请求弹回了登录页。）",
    )
    value, note = gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object())
    assert value is None
    assert note == (
        "页面内 fetch：在浏览器页面里问「饼干」列表没问动（页面可能正在换地址）。"
        "；HTTP 重放：登录后没能进入用户系统（X 岛把请求弹回了登录页。）"
    ), note


def test_page_fetch_shape_mismatch_is_written_not_swallowed() -> None:
    """fetch 取回一个不像饼干的东西：写形状（绝不写值），HTTP 重放照跑、两行都留。"""
    secret = "这是一段中文说明，不是一块饼干"
    jar = [{"name": "PHPSESSID", "value": "abc", "domain": ".nmbxd1.com"}]
    backend = _PageFetchBackend(jar, page_value=secret, detail="站点没回话。")
    value, note = gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object())
    assert value is None
    assert "页面内 fetch 取到了值，但形状不像 userhash" in note, note
    assert secret not in note, f"诊断里把值本身写出来了：{note}"
    assert backend.handed == [jar], "fetch 被粗筛挡下，HTTP 重放必须接着试"
    assert "HTTP 重放：站点没回话。" in note, note


def test_page_fetch_crash_becomes_a_line_and_http_still_runs() -> None:
    """第一条路整个抛出来（会话断了之类）：变成一句话，不拦第二条路。"""
    jar = [{"name": "PHPSESSID", "value": "abc", "domain": ".nmbxd1.com"}]
    backend = _PageFetchBackend(jar, page_error=RuntimeError("连接断了"), value=FAKE_USERHASH)
    value, note = gui.BrowserLoginDialog._try_leaf_cookie_http(backend, object())
    assert value == FAKE_USERHASH
    assert backend.user_agents == [""], "没读 UA 的替身传空串，HTTP 支按缺省走"


def test_joined_leaf_notes_only_joins_when_both_have_something_to_say() -> None:
    """拼接行的规矩（v0.13.30）：只有一条有话时**原样**返回那条 —— 逐字断言不许被前缀污染。"""
    assert gui._joined_leaf_notes("", "乙") == "乙"
    assert gui._joined_leaf_notes("甲", "") == "甲"
    assert gui._joined_leaf_notes("甲", "乙") == "甲；HTTP 重放：乙"


def test_the_timeout_message_carries_the_diagnosis_too() -> None:
    """等超时那句话要同时带上「试过的几步」和书面诊断（v0.13.24）。

    用户报问题时贴的是运行日志。只写「试过的几步」的话，饼干名单和「走 HTTP 领饼干」
    那条路的原话就永远进不了日志 —— m31364 那份日志里一条 HTTP 记录都没有，
    看不出那条路到底跑没跑。
    """
    text = gui.BrowserLoginDialog._timeout_message(
        [
            "浏览器里的饼干：PHPSESSID（还没有 userhash）。",
            "走 HTTP 领饼干：X 岛把请求弹回了登录页。",
        ],
        "（走 HTTP 领饼干：X 岛说这块饼干不认）",
    )
    assert "还没看到登录成功" in text
    assert "① 浏览器里的饼干：PHPSESSID" in text
    assert "② 走 HTTP 领饼干" in text
    assert "走 HTTP 领饼干：X 岛说这块饼干不认" in text

    # 只有一步时用「一步」的说法，不硬凑序号；诊断照样要有。
    one = gui.BrowserLoginDialog._timeout_message(
        ["浏览器里的饼干：PHPSESSID（还没有 userhash）。"], "（x）"
    )
    assert "①" not in one
    assert "程序试过的一步" in one

    # 一步都没有时不该硬凑序号；诊断照样要有。
    alone = gui.BrowserLoginDialog._timeout_message([], "（读不到浏览器里的饼干：连接断了）")
    assert "程序试过" not in alone
    assert "连接断了" in alone


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
    # 同上：这条钉那句超时的**内容**，不钉续等（不关会把 15 秒的等待窗口顶满）。
    monkeypatch.setattr(gui, "BROWSER_LOGIN_EXTRA_ROUNDS", 0)
    dialog = open_dialog()
    home = f"{browser_login.COOKIE_SITE}/Member/User/Index/index.html"
    pages = {home: {"url": home, "login": False, "jump": "", "kind": "other", "ids": []}}
    urls = {"home": home}
    marker = "服务端说：这块饼干已经过期了"

    monkeypatch.setattr(
        browser_login,
        "apply_leaf_cookie_over_http",
        lambda cookies, **kwargs: browser_login.LeafCookie(None, marker),
    )

    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.cookies = [
        {"name": "PHPSESSID", "value": "abc123"},
        {"name": "memberUserspapapa", "value": "logged-in"},
    ]
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


def test_the_http_path_is_throttled_by_time_not_by_the_jar(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """这条路两条腿：隔 ``BROWSER_LEAF_HTTP_SECONDS`` 试一次；罐头一变**立刻**再试。

    为什么不能只按「罐头变没变」：真机上出现过罐头一直没变 ⇒ 整轮一次都不试 ⇒ 界面上
    只剩「还没有 userhash」、日志里一条登录记录都没有（m32057）。这里钉住那一半：
    节流窗口之内不重复连站点。另一半（罐头一变就必须立刻试）由
    ``test_login_finished_late_still_gets_a_chance_to_apply_the_cookie`` 钉住。
    """
    monkeypatch.setattr(gui, "BROWSER_LEAF_HTTP_SECONDS", 30.0)
    dialog = open_dialog()
    home = f"{browser_login.COOKIE_SITE}/Member/User/Index/index.html"
    calls: list[list] = []

    def fake_http(cookies, **kwargs):
        calls.append(list(cookies))
        return browser_login.LeafCookie(None, "站点说：这个账号还没有可用的饼干")

    monkeypatch.setattr(browser_login, "apply_leaf_cookie_over_http", fake_http)

    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.cookies = [
        {"name": "PHPSESSID", "value": "abc123"},
        {"name": "memberUserspapapa", "value": "logged-in"},
    ]
    session.pages = {home: {"url": home, "login": False, "jump": "", "kind": "other", "ids": []}}
    session.current_url_value = home

    assert _wait_for(root_window, lambda: bool(calls), timeout=10.0), "第一轮就没去试 HTTP"
    # BROWSER_LEAF_SECONDS 被 fast_browser_polling 调成 0.15 秒：这一秒里够跑好几轮，
    # 而 30 秒的节流窗口还没到，罐头也没变 —— 只该试过那一次。
    _pump(root_window, 1.0)
    assert len(calls) == 1, f"节流窗口之内连了 {len(calls)} 次站点"

    # 罐头一变（用户刚登录完，站点换了新值）就该立刻再试一次，不用等那 30 秒。
    session.cookies = [
        {"name": "PHPSESSID", "value": "abc123"},
        {"name": "memberUserspapapa", "value": "logged-in-for-real"},
    ]
    assert _wait_for(root_window, lambda: len(calls) >= 2, timeout=10.0), "罐头变了却没再试"


def test_the_http_path_keeps_trying_even_when_nothing_changes(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """罐头一动不动，也要按时间一次次去试（v0.13.26 修的就是这个）。

    v0.13.25 的闸门是「罐头变了才试」，而真机上罐头可以整轮都不变 —— 结果是界面上只
    剩一行饼干名单、运行日志里连一条登录记录都没有（m32057）。这里把节流窗口调到
    0.05 秒，罐头一个字都不改，看它会不会自己去第二次。
    """
    monkeypatch.setattr(gui, "BROWSER_LEAF_HTTP_SECONDS", 0.05)
    dialog = open_dialog()
    home = f"{browser_login.COOKIE_SITE}/Member/User/Index/index.html"
    calls: list[list] = []

    def fake_http(cookies, **kwargs):
        calls.append(list(cookies))
        return browser_login.LeafCookie(None, "站点说：这个账号还没有可用的饼干")

    monkeypatch.setattr(browser_login, "apply_leaf_cookie_over_http", fake_http)

    assert _wait_for(root_window, lambda: bool(_dialog_sessions())), "浏览器没起来"
    session = _dialog_sessions()[0]
    session.cookies = [
        {"name": "PHPSESSID", "value": "abc123"},
        {"name": "memberUserspapapa", "value": "logged-in"},
    ]
    session.pages = {home: {"url": home, "login": False, "jump": "", "kind": "other", "ids": []}}
    session.current_url_value = home

    assert _wait_for(root_window, lambda: len(calls) >= 3, timeout=10.0), (
        f"罐头一直没变，只试了 {len(calls)} 次 —— 真机上这就是「什么都没留下」"
    )
    # 每次都拿的是同一罐饼干：试的是「这条路还通不通」，不是「换一罐再试」。
    assert all(cookies == calls[0] for cookies in calls), calls


def test_waiting_status_says_how_long_and_which_window_counts() -> None:
    """「还在等」那句话：带秒数，点明是哪个浏览器窗口里的登录，并当场给出退路。

    v0.13.32 改了口径：老断言是「还没到超时，先别急着让人换法子」，真机 m35456 却
    证明等的人根本分不清两个 Edge 窗口 —— 在自己常用的浏览器里登录完就干等，
    左下角的粘贴按钮没人去翻。等待那句必须自己把退路说出来。

    v0.13.33 又补了两件事（m35762/m35800）：等的人分不清两个长得一样的窗口，
    句里给出棕色横幅这个肉眼判据；以及「要点一行『应用』才拿得到」。
    后半句在真机 m36307 被证伪：登录成功后 userhash 就在罐里（值=账号当前饼干），
    『应用』只是**换一块**的动作。v0.13.34 起口径改回来，但横幅、『应用』、
    「饼干切换成功」三个词照钉 —— 换饼干这条路还在教。
    """
    text = gui._waiting_status(42)
    assert "42" in text
    assert "这个窗口打开的那个浏览器" in text
    assert "自己平时用的浏览器里登录，程序看不到" in text
    assert "直接粘贴饼干登录" in text
    # 哪个窗口才是被看的：肉眼判据（棕色横幅）。
    assert "棕色横条" in text
    assert "『应用』" in text
    assert "饼干切换成功" in text
    # v0.13.34（m36307）：登录即自动带上当前饼干，别再吓人说「光登录不算完」。
    assert "自动带上" in text
    assert "光登录不算完" not in text


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


def test_a_two_minute_wait_points_at_the_paste_button(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """等够久还没登录：程序主动把「八成登错了窗口」说破一次，按钮换成显眼说法。

    真机 m35456：用户在自己常用的浏览器里登录+应用了饼干，程序盯着一次性窗口
    干等 18 分钟 —— 粘贴按钮一直在，可没人会在干等时去翻一个没变过的按钮。
    等得够久就要自己开口指路；而且只说一次，反复刷反而像坏了。
    """
    monkeypatch.setattr(gui, "BROWSER_PASTE_NUDGE_SECONDS", 0.2)
    monkeypatch.setattr(gui, "BROWSER_PROGRESS_SECONDS", 0.05)
    dialog = open_dialog()

    assert _wait_for(root_window, lambda: dialog._paste_nudges >= 1, timeout=10.0)
    assert "userhash" in dialog.hint_var.get()
    assert str(dialog.paste_button.cget("text")) == gui.BROWSER_PASTE_BUTTON_TEXT
    # 再多等几轮也不许刷第二遍。
    _pump(root_window, 0.5)
    assert dialog._paste_nudges == 1, "这句提醒只该说一次"
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

    def __init__(self, master, client, settings=None, log=None) -> None:
        super().__init__(master)
        self.client = client
        self.settings = settings
        self.log = log  # v0.13.26：真对话框多了这个「写运行日志」的回调
        self.userhash: str | None = FAKE_USERHASH
        self.after(10, self.destroy)

    def start(self) -> None:
        pass


def test_errors_while_reading_cookies_land_in_the_dialog(
    root_window, browser_shim, open_dialog, monkeypatch
):
    """读饼干出错：提示落到对话框的对话窗上，不能抛成未捕获异常。

    v0.13.31 起一次失败不再立刻认输（下一轮 retarget 会重连），这里把上限压到 1
    来钉「最终还是要落到对话框上」；「磕一下不死」由
    ``test_one_flaky_read_retries_instead_of_killing_the_wait`` 钉住。
    """
    monkeypatch.setattr(gui, "BROWSER_READ_RETRY_LIMIT", 1)
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
