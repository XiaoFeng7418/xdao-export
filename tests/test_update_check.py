"""新版本检查的离线用例。

这部分逻辑只做两件事：比版本号、问一次 GitHub。问接口的过程全部用替身，
真机怎么问由 ``_scratch/probe_update_check.py`` 负责（那种事只能人工看一眼）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from xdao import __version__, update_check

FAKE_JSON = json.dumps(
    {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/XiaoFeng7418/xdao-export/releases/tag/v9.9.9",
    }
).encode("utf-8")


class FakeResponse:
    """够用的假响应：只需要 read()、能被 with 用。"""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        self.read_bytes += 1
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeOpener:
    """替换 build_opener()，并记下最后一次请求。"""

    def __init__(self, body: bytes = FAKE_JSON, error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.requests: list[object] = []
        self.timeouts: list[float] = []

    def open(self, request, timeout=None):  # noqa: ANN001
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body)


@pytest.fixture()
def cache_file(tmp_path: Path) -> Path:
    """一份独立的缓存文件，别碰用户真实配置目录。"""
    return tmp_path / "update-check.json"


# ---------------------------------------------------------------- 版本号


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v0.11.0", (0, 11, 0)),
        ("0.11.0", (0, 11, 0)),
        ("V1.2.3", (1, 2, 3)),
        ("v0.12", (0, 12, 0)),
        ("1", (1, 0, 0)),
        ("v1.2.3.4", (1, 2, 3)),
        ("v0.12.0-beta1", (0, 12, 0)),
        ("0.11.0+build7", (0, 11, 0)),
        ("  v0.10.2  ", (0, 10, 2)),
    ],
)
def test_parse_version_reads_the_numbers(text: str, expected: tuple[int, int, int]) -> None:
    assert update_check.parse_version(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "v", "abc", "latest", "vX.Y.Z", ".", ".."])
def test_parse_version_falls_back_to_zero(text: str) -> None:
    """认不出来就是 0.0.0：宁可不说「有新版本」，也不能瞎报。"""
    assert update_check.parse_version(text) == (0, 0, 0)


def test_is_newer_compares_the_three_numbers() -> None:
    assert update_check.is_newer("v0.12.0", "0.11.0") is True
    assert update_check.is_newer("v0.11.1", "0.11.0") is True
    assert update_check.is_newer("v1.0.0", "0.99.99") is True


def test_is_newer_is_false_for_the_same_or_older() -> None:
    assert update_check.is_newer("v0.11.0", "v0.11.0") is False
    assert update_check.is_newer("v0.10.9", "0.11.0") is False


def test_is_newer_is_false_when_the_tag_is_unreadable() -> None:
    assert update_check.is_newer("新版本", "0.11.0") is False


def test_release_info_reports_both_forms() -> None:
    info = update_check.ReleaseInfo(tag="v9.9.9", url="https://example.invalid/9")
    assert info.version == (9, 9, 9)
    assert info.display_version == "9.9.9"


# ---------------------------------------------------------------- 代理


def test_detect_proxy_prefers_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(update_check, "_winreg_proxy", lambda: "")
    assert update_check.detect_proxy() == "http://127.0.0.1:7890"


def test_detect_proxy_reads_the_system_settings_when_env_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(update_check, "_winreg_proxy", lambda: "127.0.0.1:7890")
    assert update_check.detect_proxy() == "http://127.0.0.1:7890"


def test_detect_proxy_is_empty_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(update_check, "_winreg_proxy", lambda: "")
    assert update_check.detect_proxy() == ""


@pytest.mark.parametrize(
    "text",
    ["", "   ", "没有端口", "ftp://127.0.0.1:21", "socks5://127.0.0.1:1080", "http://"],
)
def test_usable_proxy_rejects_nonsense(text: str) -> None:
    assert update_check._usable_proxy(text) == ""


def test_usable_proxy_adds_the_scheme() -> None:
    assert update_check._usable_proxy("127.0.0.1:7890") == "http://127.0.0.1:7890"


def test_winreg_proxy_handles_the_per_protocol_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """注册表里可能写成 ``http=a;https=b``。"""
    import sys
    import types

    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = 0
    fake.QueryValueEx = lambda key, name: (  # type: ignore[attr-defined]
        (1, 0) if name == "ProxyEnable" else ("http=10.0.0.1:8080;https=10.0.0.2:8443", 0)
    )

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    fake.OpenKey = lambda *args, **kwargs: Key()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert update_check._winreg_proxy() == "10.0.0.2:8443"


def test_winreg_proxy_returns_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = 0
    fake.QueryValueEx = lambda key, name: (0, 0)  # type: ignore[attr-defined]

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    fake.OpenKey = lambda *args, **kwargs: Key()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert update_check._winreg_proxy() == ""


# ---------------------------------------------------------------- 缓存


def test_cache_round_trip(cache_file: Path) -> None:
    assert update_check.read_cache(cache_file) is None
    update_check.write_cache("v9.9.9", "https://example.invalid/9", cache_file)
    payload = update_check.read_cache(cache_file)
    assert payload is not None
    assert payload["tag"] == "v9.9.9"
    assert payload["url"] == "https://example.invalid/9"
    assert isinstance(payload["checked_at"], float)


def test_read_cache_survives_a_broken_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{不是 JSON", encoding="utf-8")
    assert update_check.read_cache(broken) is None
    assert update_check.read_cache(tmp_path / "没有这个文件.json") is None


def test_read_cache_rejects_a_non_object(tmp_path: Path) -> None:
    array = tmp_path / "array.json"
    array.write_text("[1, 2, 3]", encoding="utf-8")
    assert update_check.read_cache(array) is None


def test_write_cache_does_not_raise_when_it_cannot_write(tmp_path: Path) -> None:
    """记不住就算了，不该因为缓存写不进去而报错。"""
    locked = tmp_path / "目录其实是个文件夹"
    locked.mkdir()
    update_check.write_cache("v9.9.9", "u", locked)  # 目标是目录 → OSError 被吞掉
    assert update_check.read_cache(locked) is None


# ---------------------------------------------------------------- 问接口


def test_fetch_latest_reads_the_tag_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = FakeOpener()
    monkeypatch.setattr(update_check.urllib.request, "build_opener", lambda *h: opener)
    info = update_check.fetch_latest()
    assert info.tag == "v9.9.9"
    assert info.url.endswith("/releases/tag/v9.9.9")
    assert opener.timeouts and opener.timeouts[0] == update_check.TIMEOUT_SECONDS
    request = opener.requests[0]
    assert request.full_url == update_check.API_URL
    assert "xdao-export" in request.get_header("User-agent")


def test_fetch_latest_builds_a_url_when_the_api_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"tag_name": "v9.9.9"}).encode("utf-8")
    monkeypatch.setattr(
        update_check.urllib.request, "build_opener", lambda *h: FakeOpener(body)
    )
    info = update_check.fetch_latest()
    assert info.url == f"https://github.com/{update_check.REPO}/releases/tag/v9.9.9"


def test_fetch_latest_reports_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    error = urllib.error.HTTPError(update_check.API_URL, 404, "Not Found", {}, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        update_check.urllib.request, "build_opener", lambda *h: FakeOpener(error=error)
    )
    with pytest.raises(update_check.UpdateCheckError) as caught:
        update_check.fetch_latest()
    assert "404" in str(caught.value)


def test_fetch_latest_reports_an_unreachable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    error = urllib.error.URLError("连接被重置")
    monkeypatch.setattr(
        update_check.urllib.request, "build_opener", lambda *h: FakeOpener(error=error)
    )
    with pytest.raises(update_check.UpdateCheckError) as caught:
        update_check.fetch_latest()
    assert "连不上 GitHub" in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [b"<html>not json</html>", b"[1,2,3]", b'{"name":"x"}', b"{}", "<html>不是 JSON</html>".encode("utf-8")],
)
def test_fetch_latest_rejects_shapes_it_cannot_use(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    monkeypatch.setattr(
        update_check.urllib.request, "build_opener", lambda *h: FakeOpener(body)
    )
    with pytest.raises(update_check.UpdateCheckError):
        update_check.fetch_latest()


# ---------------------------------------------------------------- 对外入口


def _fetcher(info=None, error: Exception | None = None):
    calls: list[dict] = []

    def fetch(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        if error is not None:
            raise error
        return info

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_check_for_update_says_when_there_is_a_newer_release(cache_file: Path) -> None:
    fetch = _fetcher(update_check.ReleaseInfo(tag="v9.9.9", url="https://example.invalid/9"))
    result = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert result.ok is True
    assert result.newer is True
    assert result.latest_version == "v9.9.9"
    assert result.url == "https://example.invalid/9"
    assert "有新版本可用" in result.line()
    assert "0.11.0" in result.line()


def test_check_for_update_uses_the_real_current_version_by_default(tmp_path: Path) -> None:
    """不传 current 时用的就是程序自己的版本号。

    缓存路径给一个临时目录里的、还不存在的文件 —— 以前这里写的是相对路径
    （仓库根目录下一个不存在的名字），跑一次用例就在工作区里留下一个 json 文件。
    """
    fetch = _fetcher(update_check.ReleaseInfo(tag=__version__))
    result = update_check.check_for_update(path=tmp_path / "缓存.json", fetcher=fetch)
    assert result.ok is True
    assert result.current == __version__
    assert result.newer is False


def test_check_for_update_reports_being_up_to_date(cache_file: Path) -> None:
    fetch = _fetcher(update_check.ReleaseInfo(tag="v0.11.0"))
    result = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert result.ok is True
    assert result.newer is False
    assert "已是最新版本" in result.line()


def test_check_for_update_writes_the_cache_and_then_uses_it(cache_file: Path) -> None:
    fetch = _fetcher(update_check.ReleaseInfo(tag="v9.9.9", url="https://example.invalid/9"))
    first = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert first.from_cache is False
    assert len(fetch.calls) == 1

    second = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert second.from_cache is True
    assert second.newer is True
    assert "（24 小时内问过）" in second.reason
    assert len(fetch.calls) == 1, "命中缓存时不该再问一次接口"


def test_check_for_update_ignores_a_stale_cache(cache_file: Path) -> None:
    update_check.write_cache("v9.9.9", "u", cache_file)
    stale = json.loads(cache_file.read_text(encoding="utf-8"))
    stale["checked_at"] = time.time() - update_check.CACHE_TTL_SECONDS - 60
    cache_file.write_text(json.dumps(stale), encoding="utf-8")

    fetch = _fetcher(update_check.ReleaseInfo(tag="v0.11.0"))
    result = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert result.from_cache is False
    assert len(fetch.calls) == 1


def test_check_for_update_force_ignores_the_cache(cache_file: Path) -> None:
    update_check.write_cache("v9.9.9", "u", cache_file)
    fetch = _fetcher(update_check.ReleaseInfo(tag="v0.11.0"))
    result = update_check.check_for_update(
        force=True, path=cache_file, fetcher=fetch, current="0.11.0"
    )
    assert result.from_cache is False
    assert result.newer is False
    assert len(fetch.calls) == 1


def test_check_for_update_treats_a_cache_without_timestamp_as_missing(
    cache_file: Path,
) -> None:
    cache_file.write_text(json.dumps({"tag": "v9.9.9"}), encoding="utf-8")
    fetch = _fetcher(update_check.ReleaseInfo(tag="v0.11.0"))
    result = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert result.from_cache is False
    assert len(fetch.calls) == 1


def test_check_for_update_swallows_network_failures(cache_file: Path) -> None:
    fetch = _fetcher(error=update_check.UpdateCheckError("连不上 GitHub（连接被重置）"))
    result = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert result.ok is False
    assert result.newer is False
    assert result.reason == "连不上 GitHub（连接被重置）"
    assert "没查到" in result.line()


def test_check_for_update_swallows_unexpected_errors(cache_file: Path) -> None:
    """第三方接口什么都能抛，这里是最后一道防线。"""
    fetch = _fetcher(error=RuntimeError("天知道"))
    result = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert result.ok is False
    assert "RuntimeError" in result.reason


def test_check_for_update_rejects_a_result_without_a_tag(cache_file: Path) -> None:
    fetch = _fetcher(update_check.ReleaseInfo(tag=""))
    result = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert result.ok is False
    assert result.reason == "接口没给出可用的版本号"


def test_check_for_update_rejects_a_result_of_the_wrong_type(cache_file: Path) -> None:
    fetch = _fetcher("v9.9.9")  # 不是 ReleaseInfo
    result = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert result.ok is False
    assert result.reason == "接口没给出可用的版本号"


def test_check_for_update_does_not_cache_a_failure(cache_file: Path) -> None:
    fetch = _fetcher(error=update_check.UpdateCheckError("没网"))
    update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    assert update_check.read_cache(cache_file) is None


def test_check_for_update_passes_the_detected_proxy(
    cache_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真问接口时要把探到的代理传下去。"""
    seen: list[dict] = []

    def real_fetcher(**kwargs):  # noqa: ANN003
        seen.append(kwargs)
        return update_check.ReleaseInfo(tag="v0.11.0")

    monkeypatch.setattr(update_check, "fetch_latest", real_fetcher)
    monkeypatch.setattr(update_check, "detect_proxy", lambda: "http://127.0.0.1:7890")
    update_check.check_for_update(path=cache_file, current="0.11.0")
    assert seen == [{"proxy": "http://127.0.0.1:7890"}]


def test_check_for_update_accepts_an_explicit_proxy(cache_file: Path) -> None:
    seen: list[dict] = []

    def real_fetcher(**kwargs):  # noqa: ANN003
        seen.append(kwargs)
        return update_check.ReleaseInfo(tag="v0.11.0")

    original = update_check.fetch_latest
    update_check.fetch_latest = real_fetcher  # type: ignore[assignment]
    try:
        update_check.check_for_update(path=cache_file, current="0.11.0", proxy="")
    finally:
        update_check.fetch_latest = original  # type: ignore[assignment]
    assert seen == [{"proxy": ""}]


def test_result_json_is_readable_chinese(cache_file: Path) -> None:
    fetch = _fetcher(update_check.ReleaseInfo(tag="v9.9.9", url="https://example.invalid/9"))
    result = update_check.check_for_update(path=cache_file, fetcher=fetch, current="0.11.0")
    payload = json.loads(result.to_json())
    assert payload["当前版本"] == "0.11.0"
    assert payload["有没有新的"] is True
    assert payload["最新版本"] == "v9.9.9"
    assert payload["下载页"] == "https://example.invalid/9"
    assert "9.9.9" in result.to_json()


def test_cache_path_lives_next_to_the_config() -> None:
    path = update_check.cache_path()
    assert path.name == "update-check.json"
    from xdao.settings import app_config_dir

    # 只比路径，不碰真实文件系统：CI 的 Linux 机器上配置目录还没建，
    # 断言 path.parent.exists() 会红（本机 Windows 因为跑过程序所以是绿的）。
    assert path.parent == app_config_dir()
