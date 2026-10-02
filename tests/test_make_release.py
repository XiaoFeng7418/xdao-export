"""`tools/make_release.py` 的用例：发布这一步不许「看着成功了」。

这个工具是发版流程的第 6 步（建 Release、传附件）。它以前有两处会让人误以为成功：

- 附件文件写错路径（比如忘了打包）时只印一行「跳过（文件不存在）」，Release 照样建出来，
  退出码还是 0 —— 等发现时版本已经发出去了，只剩一个没有附件的 Release；
- 发布说明按 utf-8 读，记事本另存出来的说明（带 BOM）会把 BOM 一起写进正文，页面上看不见。

下面用假 API 跑 `main()`（`gh_token` / `request_json` / `upload_asset` 全换成测试自己的），
并单独核 `upload_asset` 的请求本身（附件名必须走 `?name=` 查询串，这是历史上踩过的坑：
走别的写法时接口只取第一个点号之前的部分，中文名会被截断成一个字）。
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import make_release  # noqa: E402

REPO = "owner/name"
TAG = "v9.9.9"
NAME = "v9.9.9：测试"
NOTES = "这一版做了什么。\n附件是免安装包。\n"
ASSET_NAME = "xdao-export-v9.9.9-win64.zip"
UPLOAD_URL = "https://uploads.github.com/repos/owner/name/releases/1/assets{?name,label}"


def _notes_file(tmp_path: Path, text: str = NOTES, bom: bool = False) -> Path:
    data = (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")
    path = tmp_path / "NOTES.md"
    path.write_bytes(data)
    return path


def _release(*, body: str = NOTES, name: str = NAME, assets: list[dict] | None = None) -> dict:
    return {
        "id": 1,
        "tag_name": TAG,
        "name": name,
        "body": body,
        "html_url": f"https://github.com/{REPO}/releases/tag/{TAG}",
        "upload_url": UPLOAD_URL,
        "assets": list(assets or []),
    }


def _asset(name: str = ASSET_NAME, size: int = 2048) -> dict:
    return {
        "name": name,
        "size": size,
        "browser_download_url": f"https://github.com/{REPO}/releases/download/{TAG}/{name}",
    }


def _sidecar_for(asset: Path, *, digest: str | None = None, name: str | None = None) -> Path:
    """照着 ``asset`` 写一份 ``<附件名>.sha256``；可以故意写错摘要或写错文件名。"""
    if digest is None:
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    path = asset.parent / (asset.name + make_release.SIDECAR_SUFFIX)
    path.write_text(f"{digest}  {name or asset.name}\n", encoding="utf-8", newline="\n")
    return path


class _Api:
    """假 API：记下每次调用，按方法 + 路径回话；没准备的请求直接炸，免得用例悄悄放过。"""

    def __init__(self, *, existing=None, created=None, final=None, patched=None) -> None:
        self.existing = existing
        self.created = created
        self.final = final
        self.patched = patched
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict | None] = []
        self.lookups = 0

    def __call__(self, method: str, path: str, token: str, payload=None, retries: int = 4) -> dict:
        self.calls.append((method, path))
        self.payloads.append(payload)
        if method == "GET" and path == f"/repos/{REPO}/releases/tags/{TAG}":
            self.lookups += 1
            if self.lookups == 1:
                if isinstance(self.existing, Exception):
                    raise self.existing
                return dict(self.existing)
            if isinstance(self.final, Exception):
                raise self.final
            return dict(self.final)
        if method == "POST" and path == f"/repos/{REPO}/releases":
            if isinstance(self.created, Exception):
                raise self.created
            return dict(self.created)
        if method == "PATCH" and path.startswith(f"/repos/{REPO}/releases/"):
            if isinstance(self.patched, Exception):
                raise self.patched
            return dict(self.patched)
        raise AssertionError(f"用例没准备这个请求：{method} {path}")

    def methods(self) -> list[str]:
        return [method for method, _path in self.calls]


def _run_main(monkeypatch, capsys, tmp_path: Path, *, api, upload=None, asset: Path | None = None,
              extra_assets: list[Path] | None = None):
    """跑一次 main()：token、假 API、附件上传都换成测试自己的。"""
    notes = _notes_file(tmp_path)
    if asset is None:
        asset = tmp_path / ASSET_NAME
        asset.write_bytes(b"zip-bytes" * 16)
    uploaded: list[tuple[str, Path]] = []

    def fake_upload(upload_url: str, path: Path, token: str, retries: int = 3) -> dict:
        uploaded.append((upload_url, path))
        return _asset(path.name)

    monkeypatch.setattr(make_release, "gh_token", lambda: "tok")
    monkeypatch.setattr(make_release, "request_json", api)
    monkeypatch.setattr(make_release, "upload_asset", upload or fake_upload)
    argv = [
        "--repo", REPO, "--tag", TAG, "--name", NAME,
        "--notes-file", str(notes), "--asset", str(asset),
    ]
    for extra in extra_assets or []:
        argv += ["--asset", str(extra)]
    code = make_release.main(argv)
    return code, capsys.readouterr().out, asset, uploaded


# ---------------------------------------------------------------- 令牌


def test_the_token_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "  from-env\n")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert make_release.gh_token() == "from-env"

    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setenv("GH_TOKEN", "from-gh-token")
    assert make_release.gh_token() == "from-gh-token"


def test_the_token_falls_back_to_gh(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("XDAO_GH", r"<某个目录>\gh.exe")
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="gh-token\n", stderr="")

    monkeypatch.setattr(make_release.subprocess, "run", fake_run)
    assert make_release.gh_token() == "gh-token"
    assert seen == [[r"<某个目录>\gh.exe", "auth", "token"]]


def test_a_missing_gh_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def fake_run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(make_release.subprocess, "run", fake_run)
    with pytest.raises(make_release.ApiError) as info:
        make_release.gh_token()
    assert "XDAO_GH" in str(info.value)


def test_a_failed_gh_login_is_an_error(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(
        make_release.subprocess, "run",
        lambda argv, **kwargs: types.SimpleNamespace(returncode=1, stdout="", stderr="not logged in"),
    )
    with pytest.raises(make_release.ApiError) as info:
        make_release.gh_token()
    assert "gh auth login" in str(info.value)


# ---------------------------------------------------------------- 发布说明


def test_the_notes_file_loses_its_bom(tmp_path):
    text = make_release.read_notes(_notes_file(tmp_path, bom=True))
    assert text == NOTES
    assert not text.startswith("\ufeff")


def test_empty_notes_are_refused(tmp_path):
    path = _notes_file(tmp_path, text="  \n\n")
    with pytest.raises(make_release.ApiError) as info:
        make_release.read_notes(path)
    assert "空" in str(info.value)


# ---------------------------------------------------------------- main()：先验再动


def test_a_missing_asset_stops_before_anything_is_created(monkeypatch, capsys, tmp_path):
    api = _Api(existing=make_release.ApiError("404"))
    absent = tmp_path / "还没打包.zip"
    code, out, _asset_path, uploaded = _run_main(monkeypatch, capsys, tmp_path, api=api, asset=absent)

    assert code == 1
    assert "附件文件不存在" in out
    assert "还没打包.zip" in out
    assert api.calls == []          # 一个请求都没发出去
    assert uploaded == []


def test_missing_assets_lists_the_ones_that_are_not_there(tmp_path):
    here = tmp_path / "有.zip"
    here.write_bytes(b"x")
    gone = tmp_path / "没有.zip"
    assert make_release.missing_assets([here, gone]) == [gone]
    assert make_release.missing_assets([here]) == []


def test_a_directory_is_not_an_asset(tmp_path):
    folder = tmp_path / "打包目录"
    folder.mkdir()
    assert make_release.missing_assets([folder]) == [folder]


# ---------------------------------------------------------------- 校验文件（.sha256）


def test_a_sidecar_is_read_as_a_digest_and_a_filename():
    digest = "ab" * 32
    assert make_release.parse_sidecar(f"{digest}  x.zip\n") == (digest, "x.zip")
    assert make_release.parse_sidecar(f"{digest} *x.zip\n") == (digest, "x.zip")   # sha256sum -b
    assert make_release.parse_sidecar(digest, "x.zip") == (digest, "x.zip")        # 只写摘要


@pytest.mark.parametrize(
    "text",
    ["", "   \n\n", "abc  x.zip\n", "z" * 64 + "  x.zip\n", "ab" * 32 + "   \n"],
)
def test_a_sidecar_that_is_not_a_hash_is_refused(text):
    with pytest.raises(make_release.ApiError):
        make_release.parse_sidecar(text)


def test_a_matching_sidecar_is_verified(tmp_path):
    asset = tmp_path / ASSET_NAME
    asset.write_bytes(b"zip-bytes" * 16)
    sidecar = _sidecar_for(asset)
    digest, name = make_release.verify_sidecar(sidecar)
    assert (digest, name) == (hashlib.sha256(asset.read_bytes()).hexdigest(), ASSET_NAME)


def test_a_sidecar_that_lies_about_the_file_is_refused(tmp_path):
    asset = tmp_path / ASSET_NAME
    asset.write_bytes(b"zip-bytes" * 16)
    sidecar = _sidecar_for(asset, digest="00" * 32)
    with pytest.raises(make_release.ApiError) as info:
        make_release.verify_sidecar(sidecar)
    assert "对不上" in str(info.value)


def test_a_sidecar_without_its_file_is_refused(tmp_path):
    asset = tmp_path / ASSET_NAME
    asset.write_bytes(b"zip-bytes" * 16)
    sidecar = _sidecar_for(asset)
    asset.unlink()
    with pytest.raises(make_release.ApiError) as info:
        make_release.verify_sidecar(sidecar)
    assert "不在这里" in str(info.value)


def test_a_good_sidecar_goes_up_alongside_the_zip(monkeypatch, capsys, tmp_path):
    api = _Api(
        existing=make_release.ApiError(f"GET /repos/{REPO}/releases/tags/{TAG} -> 404\n{{}}"),
        created=_release(assets=[]),
        final=_release(assets=[_asset(), _asset(ASSET_NAME + ".sha256")]),
    )
    asset = tmp_path / ASSET_NAME
    asset.write_bytes(b"zip-bytes" * 16)
    sidecar = _sidecar_for(asset)
    code, out, _asset_path, uploaded = _run_main(
        monkeypatch, capsys, tmp_path, api=api, asset=asset, extra_assets=[sidecar]
    )

    assert code == 0
    assert f"校验文件 {sidecar.name} 与 {ASSET_NAME} 一致" in out
    assert [path.name for _url, path in uploaded] == [ASSET_NAME, sidecar.name]


def test_a_stale_sidecar_stops_before_anything_is_created(monkeypatch, capsys, tmp_path):
    """挂着一份和 zip 不配套的 .sha256，比不挂更坏：用户照它核对会以为下载坏了。"""
    api = _Api(existing=make_release.ApiError(f"GET /repos/{REPO}/releases/tags/{TAG} -> 404\n{{}}"))
    asset = tmp_path / ASSET_NAME
    asset.write_bytes(b"zip-bytes" * 16)
    sidecar = _sidecar_for(asset, digest="11" * 32)
    code, out, _asset_path, uploaded = _run_main(
        monkeypatch, capsys, tmp_path, api=api, asset=asset, extra_assets=[sidecar]
    )

    assert code == 1
    assert "校验文件对不上" in out
    assert api.calls == []
    assert uploaded == []


# ---------------------------------------------------------------- main()：新建 / 复用


def test_creating_a_new_release_uploads_the_asset(monkeypatch, capsys, tmp_path):
    api = _Api(
        existing=make_release.ApiError(f"GET /repos/{REPO}/releases/tags/{TAG} -> 404\n{{}}"),
        created=_release(assets=[]),
        final=_release(assets=[_asset()]),
    )
    code, out, asset, uploaded = _run_main(monkeypatch, capsys, tmp_path, api=api)

    assert code == 0
    assert "已创建 Release" in out
    assert "上传附件 xdao-export-v9.9.9-win64.zip" in out
    assert uploaded == [(UPLOAD_URL, asset)]
    assert "browser_download_url" not in out          # 印的是地址本身，不是字段名
    assert _asset()["browser_download_url"] in out
    assert api.methods() == ["GET", "POST", "GET"]
    assert api.payloads[1]["tag_name"] == TAG and api.payloads[1]["body"] == NOTES


def test_an_existing_release_gets_its_notes_refreshed(monkeypatch, capsys, tmp_path):
    api = _Api(
        existing=_release(body="上一版的说明", assets=[_asset()]),
        patched=_release(assets=[_asset()]),
        final=_release(assets=[_asset()]),
    )
    code, out, _asset_path, uploaded = _run_main(monkeypatch, capsys, tmp_path, api=api)

    assert code == 0
    assert "已更新 Release" in out
    assert uploaded == []                              # 附件已在，不重传
    assert "跳过（附件已存在）" in out


def test_an_up_to_date_release_is_not_patched(monkeypatch, capsys, tmp_path):
    api = _Api(
        existing=_release(assets=[_asset()]),
        final=_release(assets=[_asset()]),
    )
    code, out, _asset_path, _uploaded = _run_main(monkeypatch, capsys, tmp_path, api=api)

    assert code == 0
    assert "说明已是最新" in out
    assert "PATCH" not in api.methods()


def test_a_non_404_lookup_failure_is_not_swallowed(monkeypatch, capsys, tmp_path):
    api = _Api(existing=make_release.ApiError(f"GET /repos/{REPO}/releases/tags/{TAG} -> 401\n{{}}"))
    with pytest.raises(make_release.ApiError) as info:
        _run_main(monkeypatch, capsys, tmp_path, api=api)
    assert "401" in str(info.value)


# ---------------------------------------------------------------- main()：发完再核一遍


def test_a_release_without_the_asset_fails_loudly(monkeypatch, capsys, tmp_path):
    api = _Api(
        existing=_release(assets=[]),
        final=_release(assets=[]),                     # 传了，但 Release 上没有
    )

    def fake_upload(upload_url: str, path: Path, token: str, retries: int = 3) -> dict:
        return _asset(path.name)

    code, out, _asset_path, _uploaded = _run_main(
        monkeypatch, capsys, tmp_path, api=api, upload=fake_upload
    )
    assert code == 1
    assert "没落到 Release 上" in out
    assert ASSET_NAME in out


# ---------------------------------------------------------------- upload_asset 本身


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _capture_urlopen(monkeypatch, payload: dict, requests: list) -> None:
    def fake_urlopen(request, timeout=None):
        requests.append(request)
        return _Response(payload)

    monkeypatch.setattr(make_release.urllib.request, "urlopen", fake_urlopen)


def test_the_asset_name_goes_in_the_query_string(monkeypatch, tmp_path):
    asset = tmp_path / ASSET_NAME
    asset.write_bytes(b"z" * 4096)
    requests: list = []
    _capture_urlopen(monkeypatch, _asset(size=asset.stat().st_size), requests)

    result = make_release.upload_asset(UPLOAD_URL, asset, "tok")

    assert result["name"] == ASSET_NAME
    (request,) = requests
    assert request.full_url == (
        "https://uploads.github.com/repos/owner/name/releases/1/assets"
        f"?name={ASSET_NAME}"
    )
    assert request.get_header("Content-length") == "4096"
    # zip 的 MIME 两套都合法：Windows 从注册表读到 application/x-zip-compressed，
    # Linux（CI）按内置表读到 application/zip —— 别把平台差异写死成一种。
    assert request.get_header("Content-type") in {"application/zip", "application/x-zip-compressed"}
    assert request.get_method() == "POST"


def test_an_upload_that_lands_with_another_name_is_an_error(monkeypatch, tmp_path):
    asset = tmp_path / ASSET_NAME
    asset.write_bytes(b"z" * 16)
    _capture_urlopen(monkeypatch, {"name": "xdao"}, [])       # 名字被接口截断了

    with pytest.raises(make_release.ApiError) as info:
        make_release.upload_asset(UPLOAD_URL, asset, "tok")
    assert "附件名不符合预期" in str(info.value)


def test_an_http_error_while_uploading_is_an_error(monkeypatch, tmp_path):
    asset = tmp_path / ASSET_NAME
    asset.write_bytes(b"z" * 16)

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 422, "Unprocessable Entity", {},
            io.BytesIO(b'{"message":"Validation Failed"}'),
        )

    monkeypatch.setattr(make_release.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(make_release.ApiError) as info:
        make_release.upload_asset(UPLOAD_URL, asset, "tok")
    assert "422" in str(info.value)
    assert "Validation Failed" in str(info.value)
