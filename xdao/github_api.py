"""访问 GitHub 的共用层：令牌、带退避重试的请求、代理装配、git 子进程。

为什么要有这个模块：同一件事原先散在各处 —— ``gh_token()`` 在四个 ``tools/`` 脚本里
各写了一份、带退避重试的 GitHub 请求四份（等待时长与次数还各不相同）、git 子进程封装
三份；``xdao/updater.py`` 与 ``xdao/update_check.py`` 也各自装配过一套代理，
``updater`` 为了拿仓库 slug 还得反过来 import ``update_check``（依赖方向是倒的）。

这里收的是**实现**，不是**策略**：各调用点原本的等待时长、重试次数与提示文案由参数
带进来（见 :func:`request_bytes` 的 ``should_retry`` / ``backoff`` / ``on_retry`` /
``http_failure`` / ``retry_error`` / ``gave_up`` 等钩子），所以对用户和 CI 日志来说，
每个脚本的可观察行为与从前一致 —— 包括 ``ci_logs`` 那套「比别的脚本少等一次、
重试提示打到 stderr、状态码白名单更窄」的例外。

依赖方向：这一层只依赖标准库，不 import ``xdao`` 里的业务模块（``update_check`` /
``updater`` / ``gui`` …），也不 import ``tools``；``tools/`` 与 ``xdao/`` 都来 import 它。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

#: GitHub REST API 的根地址（原先四个脚本各写了一遍这个字面量）。
API_BASE = "https://api.github.com"

#: 请求超时（秒）的默认值。各调用点用的值不同（120 / 180），由调用方传。
DEFAULT_TIMEOUT = 120.0

#: ``ci_logs`` 认的那几个「值得重试」的状态码。别的脚本是「429 或任意 5xx」，
#: 501/505 因此不在这个更窄的白名单里 —— 这个差异保持原样。
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    """GitHub 接口调用失败。

    四个 ``tools/`` 脚本原先各定义了一份同名异常，现在都指向这里（脚本里仍保留
    ``ApiError = github_api.ApiError`` 这样的名字）：异常类型名、``str()`` 文案与
    ``except ApiError`` 的写法都没变，跨脚本 ``except`` 也仍然成立。
    """


# ---------------------------------------------------------------- 令牌


def gh_token(
    *,
    env_names: Sequence[str] = ("GITHUB_TOKEN", "GH_TOKEN"),
    error: Callable[[str], BaseException] = ApiError,
    missing: str = "找不到 gh（{gh}）；请先 gh auth login，或设置 XDAO_GH 指向 gh.exe",
    failed: str = "取不到 gh 令牌，请先 gh auth login",
) -> str:
    """按「环境变量 → ``gh auth token``」的顺序取 GitHub 令牌。

    四个脚本原先各写了一份，差别只有这四件事，都用参数表达：

    * ``env_names``：先看哪个环境变量（``make_release`` / ``push_via_api`` /
      ``sync_from_api`` 是 ``GITHUB_TOKEN`` 在前，``ci_logs`` 反过来）；
    * ``error``：抛什么异常（都是 ``ApiError``，``ci_logs`` 包一层 ``SystemExit``）；
    * ``missing``：找不到 ``gh`` 时的话（``{}`` 里可用的有 ``gh``）；
    * ``failed``：``gh`` 没登录成时的话（可用的有 ``gh`` 与 ``stderr``）。
    """
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    gh = os.environ.get("XDAO_GH") or "gh"
    try:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise error(missing.format(gh=gh)) from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise error(failed.format(gh=gh, stderr=out.stderr.strip()))
    return out.stdout.strip()


# ---------------------------------------------------------------- 代理
#
# 这个功能问的是 GitHub，和 X 岛接口不是一回事：用户为 X 岛配的代理可能只
# 代理了别处，而 GitHub 在国内常常需要梯子。所以这里自己决定用哪个代理：
# 环境变量优先，其次 Windows「Internet 选项」里的系统代理（很多梯子只写这
# 一处），都没有就直连。


def _env_proxy() -> str:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _winreg_proxy() -> str:
    """读 Windows 的「Internet 选项」代理设置（读不到就空串）。"""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return ""
    if not enabled or not server:
        return ""
    text = str(server)
    # 可能是「http=host:port;https=host:port」这种按协议分开的写法。
    if "=" in text:
        for item in text.split(";"):
            name, _, value = item.partition("=")
            if name.strip().lower() == "https" and value.strip():
                return value.strip()
        return ""
    return text


def _usable_proxy(text: str) -> str:
    """把注册表里的 ``127.0.0.1:7890`` 补成 ``http://127.0.0.1:7890``。"""
    candidate = (text or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    if not parsed.hostname or not parsed.port:
        return ""
    return candidate


def detect_proxy() -> str:
    """当前该用哪个代理（空串表示直连）。"""
    return _usable_proxy(_env_proxy()) or _usable_proxy(_winreg_proxy())


def proxy_handlers(proxy: str = "") -> list[urllib.request.BaseHandler]:
    """按代理设置给出 opener 的 handlers。

    ``proxy`` 为空串时显式装配空代理 —— 系统级代理常常只对特定程序生效，继承过来
    反而会让「本可以直连」的环境连不上（``xdao/update_check.py`` 与
    ``xdao/updater.py`` 原先各写了一遍这个判断，两处的意图与行为一致）。
    """
    if proxy:
        return [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    return [urllib.request.ProxyHandler({})]


def build_opener_with_proxy(proxy: str = "") -> urllib.request.OpenerDirector:
    """按代理设置装一个 opener（查询新版本、下载升级包两处原先各装了一遍）。"""
    return urllib.request.build_opener(*proxy_handlers(proxy))


# ---------------------------------------------------------------- 请求


def default_backoff(tries: int) -> float:
    """默认退避：第 ``tries`` 次失败后等 ``2 × tries`` 秒（``tries`` 从 1 数起）。"""
    return 2.0 * tries


def linear_backoff(
    tries: int,
    *,
    seconds_per_try: float = 2.0,
    limit: float | None = None,
) -> float:
    """``seconds_per_try × tries`` 秒，可选封顶 —— 给各脚本原来那套等待曲线用。

    ``make_release`` 是 ``min(15.0, 2.0 × 第几次)``，``push_via_api`` 与
    ``sync_from_api`` 是 ``min(20.0, 2.0 × 第几次)``。
    """
    wait = seconds_per_try * tries
    return min(limit, wait) if limit is not None else wait


def should_retry(code: int, tries: int = 1, attempts: int = 0) -> bool:
    """429 与任意 5xx 值得再试一次（``tools/make_release.py`` 等三个脚本的尺度）。"""
    return code == 429 or 500 <= code < 600


def should_retry_ci(code: int, tries: int = 1, attempts: int = 0) -> bool:
    """``tools/ci_logs.py`` 的尺度：只看 :data:`RETRY_STATUS`，且轮次用完就不再试。"""
    return code in RETRY_STATUS and tries < attempts


def request_bytes(
    method: str,
    path: str,
    *,
    token: str = "",
    payload: Any = None,
    base_url: str = API_BASE,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str,
    accept: str = "application/vnd.github+json",
    api_version: str = "",
    attempts: int = 5,
    should_retry: Callable[[int, int, int], bool] = should_retry,
    backoff: Callable[[int], float] = default_backoff,
    sleep_on_last_failure: bool = False,
    on_retry: Callable[[int, float, BaseException, BaseException], None] | None = None,
    error: Callable[[str], BaseException] = ApiError,
    http_failure: Callable[[int, str], str] | None = None,
    retry_error: Callable[[urllib.error.HTTPError, str], BaseException] | None = None,
    gave_up: Callable[[BaseException], str] | None = None,
    chain_gave_up: bool = False,
    detail_bytes: int = 300,
    detail_repr: bool = False,
    parse: Callable[[bytes], Any] | None = None,
) -> Any:
    """发一次 GitHub 请求并返回响应体；失败按调用方给的策略退避重试。

    只有这一处实现，各脚本的差别全在参数里：

    * ``path`` 以 ``http`` 开头就原样使用，否则拼在 ``base_url`` 后面；
    * ``payload`` 非 ``None`` 时按 JSON 发出去（顺手带上 ``Content-Type``）；
    * ``attempts`` 是**总尝试次数**。原先各脚本的 ``retries`` 语义并不一致
      （三个是 ``retries + 1`` 次尝试，``ci_logs`` 是 ``retries`` 次），由调用方
      换算好再传，这里不去猜；
    * ``should_retry(code, tries, attempts)``：这个状态码还要不要再试一次
      （``tries`` 从 1 数起）；
    * ``backoff(tries)``：第 ``tries`` 次失败之后等多少秒；
    * ``sleep_on_last_failure``：轮次用完之后那一次失败还要不要等 —— 多数脚本不等，
      ``ci_logs`` 的网络异常分支会等；
    * ``on_retry(tries, wait, failure, last)``：每次重试前的提示，各脚本一种写法
      （有的打 stdout，有的打 stderr，有的干脆不打）；
    * ``error``：抛什么异常（三个脚本是 :class:`ApiError`，``ci_logs`` 是 ``SystemExit``）；
    * ``http_failure(code, detail)``：不可重试的 HTTP 错误该怎么说话
      （默认是 ``"{method} {path} -> {code}\\n{detail}"``）；
    * ``retry_error(exc, detail)``：重试期间「记着」哪个异常/怎么包装它，
      默认直接记原始 :class:`urllib.error.HTTPError`；
    * ``gave_up(last)`` / ``chain_gave_up``：重试用尽时的话，以及要不要 ``from last``。
    * ``parse(body)``：怎么把响应体变成返回值。解析就放在重试循环**里面**，
      所以「响应体解析不了」跟网络抖动一样会被重试（原先四个脚本都是这个行为：
      它们把 ``json.loads`` 写在 ``try`` 里）；不给就直接返回 ``bytes``。
    """
    url = path if path.startswith("http") else base_url + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    if http_failure is None:
        http_failure = lambda code, detail: f"{method} {path} -> {code}\n{detail}"
    if gave_up is None:
        gave_up = lambda last: f"{method} {path} 连续失败：{last}"
    last: BaseException | None = None
    for tries in range(1, attempts + 1):
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", accept)
        request.add_header("User-Agent", user_agent)
        if api_version:
            request.add_header("X-GitHub-Api-Version", api_version)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                return parse(body) if parse is not None else body
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            detail = repr(raw[:detail_bytes]) if detail_repr else raw.decode("utf-8", "replace")[:detail_bytes]
            if not should_retry(exc.code, tries, attempts):
                raise error(http_failure(exc.code, detail)) from exc
            failure = exc
            last = retry_error(exc, detail) if retry_error is not None else exc
        except Exception as exc:
            failure = exc
            last = exc
        if tries < attempts or sleep_on_last_failure:
            wait = backoff(tries)
            if on_retry is not None:
                on_retry(tries, wait, failure, last)
            time.sleep(wait)
    if chain_gave_up and last is not None:
        raise error(gave_up(last)) from last
    raise error(gave_up(last))


# ---------------------------------------------------------------- git


def git(*args: str, data: bytes | None = None) -> bytes:
    """跑一条 ``git`` 命令，返回 stdout；失败抛 :class:`ApiError`。

    ``data`` 喂给 git 的标准输入（``git mktree``、``git hash-object --stdin``
    这类要从 stdin 读的命令用得上）。原先 ``tools/push_via_api.py`` 与
    ``tools/sync_from_api.py`` 各有一份，差别只有这个 ``input``。
    """
    result = subprocess.run(["git", *args], input=data, capture_output=True)
    if result.returncode != 0:
        raise ApiError(f"git {' '.join(args)} 失败：{result.stderr.decode('utf-8', 'replace')}")
    return result.stdout


def git_in(repo_dir: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在 ``repo_dir`` 里跑一条 ``git`` 命令（文本模式），**不抛异常**。

    ``tools/repo_check.py`` 里那三处封装原本各写了一遍这段调用，只是参数、解码方式
    一致而失败处理不同（两处抛 :class:`ApiError`、一处返回 ``None``），所以这里只收
    「怎么起进程」，``returncode`` 交回调用方自己判断。
    """
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, encoding="utf-8",
    )
