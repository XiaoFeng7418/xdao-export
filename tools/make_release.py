"""创建 GitHub Release 并上传附件（走 Release 附件专用上传接口）。

背景：本机 git 的 HTTPS 传输不可用，只能通过 API 维护仓库；
Release 附件的上传接口与 Git Data API 的 blob 接口是两条不同的通道，
附件接口更适合上传打包好的 exe。

用法：
    python tools/make_release.py --repo owner/name --tag v0.2.0 ^
        --name "v0.2.0：四种格式 + 断点续传 + 串监控" ^
        --notes-file RELEASE_NOTES.md --asset dist/xxx.exe

打包版从 v0.13.28 起还挂一份 ``*.sha256`` 校验文件（见 ``verify_sidecar``）：
上传前会拿它跟旁边那个包对一遍，对不上就停手 —— 一份和 zip 不配套的校验文件
比没有校验文件更坏，用户照着核对会以为下载坏了。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"

#: 校验附件的后缀。附件名就是「它描述的那个文件的完整文件名 + 这个后缀」。
SIDECAR_SUFFIX = ".sha256"


class ApiError(RuntimeError):
    pass


def gh_token() -> str:
    """取 GitHub 令牌：优先环境变量，其次 PATH 上的 gh。

    本机的 gh 可能不在 PATH 里，可以用环境变量 ``XDAO_GH`` 指向 gh.exe。
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    gh = os.environ.get("XDAO_GH") or "gh"
    try:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ApiError(f"找不到 gh（{gh}）；请先 gh auth login，或设置 XDAO_GH 指向 gh.exe") from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise ApiError("取不到 gh 令牌，请先 gh auth login")
    return out.stdout.strip()


def read_notes(path: Path) -> str:
    """读发布说明，顺手扔掉文件开头的 BOM，空文件直接报错。

    说明文件常是记事本另存出来的（带 BOM）。BOM 混进 Release 正文里在页面上看不见，
    却会跟着一起发出去；空的说明更糟 —— 发版规矩要求正文里说清下的是哪个附件。
    """
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ApiError(f"发布说明是空的：{path}")
    return text


def missing_assets(assets: list[Path]) -> list[Path]:
    """挑出根本不存在的附件。

    「文件不存在就跳过」在发版这件事上不算宽容：Release 会照样建出来，
    只是没有附件，而工具退出码还是 0 —— 等发现时版本已经发出去了。
    """
    return [path for path in assets if not path.is_file()]


def request_json(method: str, path: str, token: str, payload: dict | None = None,
                 retries: int = 4) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "xdao-release")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (429,) or 500 <= exc.code < 600:
                last = ApiError(f"{exc.code} {detail}")
            else:
                raise ApiError(f"{method} {path} -> {exc.code}\n{detail}") from exc
        except Exception as exc:
            last = exc
        if attempt < retries:
            wait = min(15.0, 2.0 * (attempt + 1))
            print(f"    （网络失败，{wait:.0f} 秒后重试：{type(last).__name__}）")
            time.sleep(wait)
    raise ApiError(f"{method} {path} 连续失败：{last}")


def request_text(path: str, token: str, retries: int = 4) -> str:
    """按路径取一段文本 —— Release 附件的正文走这条。

    ``request_json`` 会把响应当 JSON 解析；附件的正文不是 JSON，得单独走一条，
    靠 ``Accept: application/octet-stream`` 让 GitHub 把原始字节给出来。
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(API + path)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/octet-stream")
        req.add_header("User-Agent", "xdao-release")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (429,) or 500 <= exc.code < 600:
                last = ApiError(f"{exc.code} {detail}")
            else:
                raise ApiError(f"GET {path} -> {exc.code}\n{detail}") from exc
        except Exception as exc:
            last = exc
        if attempt < retries:
            wait = min(15.0, 2.0 * (attempt + 1))
            print(f"    （网络失败，{wait:.0f} 秒后重试：{type(last).__name__}）")
            time.sleep(wait)
    raise ApiError(f"GET {path} 连续失败：{last}")


def parse_sidecar(text: str, fallback_name: str = "") -> tuple[str, str]:
    """读一份 ``*.sha256`` 的内容，返回 (摘要, 文件名)。

    只认 ``sha256sum`` 那一行：``<64 位十六进制>  <文件名>``（两个空格，文件名可省）。
    文件名省略时用 ``fallback_name``（附件名去掉 ``.sha256``）。格式不对就抛 ``ApiError``。
    """
    line = next((raw.strip() for raw in text.splitlines() if raw.strip()), "")
    if not line:
        raise ApiError("校验文件是空的：里面应当有一行 <64 位十六进制>  <文件名>")
    parts = line.split()
    digest = parts[0].lower()
    name = parts[1] if len(parts) > 1 else fallback_name
    if name.startswith("*"):  # sha256sum -b 会加这个「二进制模式」记号
        name = name[1:]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ApiError(f"校验文件里不是一行 64 位十六进制摘要：{line[:80]}")
    if not name:
        raise ApiError("校验文件里没写它描述的是哪个文件（附件名去不掉 .sha256）")
    return digest, name


def verify_sidecar(asset: Path) -> tuple[str, str]:
    """核对一份 ``*.sha256`` 附件与它描述的那个文件（就在同一个目录里）。"""
    digest, name = parse_sidecar(
        asset.read_text(encoding="utf-8-sig"), asset.name[: -len(SIDECAR_SUFFIX)]
    )
    target = asset.parent / name
    if not target.is_file():
        raise ApiError(f"{asset.name} 说的文件不在这里：{target}")
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    if actual != digest:
        raise ApiError(
            f"{asset.name} 与 {name} 对不上：文件里写 {digest[:16]}…，实际算出来 {actual[:16]}…"
        )
    return digest, name


def upload_asset(upload_url: str, asset: Path, token: str, retries: int = 3) -> dict:
    """把文件作为 Release 附件上传（二进制直传，不做 base64 包装）。

    附件名必须显式传入 name 参数并做 URL 编码：走 ?name= 查询串时，
    接口只会取到第一个点号之前的部分，中文名还会被截断成单个字符。
    """
    data = asset.read_bytes()
    query = urllib.parse.urlencode({"name": asset.name})
    url = upload_url.split("{")[0] + "?" + query
    mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", mime)
        req.add_header("Content-Length", str(len(data)))
        req.add_header("User-Agent", "xdao-release")
        try:
            with urllib.request.urlopen(req, timeout=1800) as resp:
                result = json.loads(resp.read())
            if result.get("name") != asset.name:
                raise ApiError(
                    f"附件名不符合预期：期望 {asset.name}，实际 {result.get('name')}"
                )
            return result
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ApiError(f"上传附件失败 {exc.code}：{detail}") from exc
        except ApiError:
            raise
        except Exception as exc:
            last = exc
            print(f"    （上传中断：{type(exc).__name__}，重试 {attempt + 2}/{retries + 1}）")
            time.sleep(5 * (attempt + 1))
    raise ApiError(f"上传附件连续失败：{last}")


def rename_asset(repo: str, asset: dict, new_name: str, token: str) -> dict:
    """给已有附件改名（用于修正历史上传坏掉的文件名）。"""
    return request_json(
        "PATCH", f"/repos/{repo}/releases/assets/{asset['id']}", token, {"name": new_name}
    )


def delete_asset(repo: str, asset_id: int, token: str) -> None:
    request_json("DELETE", f"/repos/{repo}/releases/assets/{asset_id}", token)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="创建 Release 并上传附件")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--notes-file", required=True)
    parser.add_argument("--asset", action="append", default=[])
    parser.add_argument("--target", default="", help="该 tag 指向的提交（默认仓库默认分支）")
    args = parser.parse_args(argv)

    token = gh_token()
    notes = read_notes(Path(args.notes_file))

    assets = [Path(raw) for raw in args.asset]
    gone = missing_assets(assets)
    if gone:
        print("× 附件文件不存在：" + "、".join(str(path) for path in gone))
        print("  先打包再发布 —— 发出一个只有说明的 Release，比不发还难收拾。")
        return 1

    for sidecar in [path for path in assets if path.name.endswith(SIDECAR_SUFFIX)]:
        try:
            digest, name = verify_sidecar(sidecar)
        except ApiError as exc:
            print(f"× 校验文件对不上：{exc}")
            print("  别把一份和包不配套的 .sha256 发出去 —— 用户照着核对会以为下载坏了。")
            return 1
        print(f"校验文件 {sidecar.name} 与 {name} 一致（{digest[:16]}…）")

    existing = None
    try:
        existing = request_json("GET", f"/repos/{args.repo}/releases/tags/{args.tag}", token)
        print(f"Release {args.tag} 已存在，将复用它（不再新建）")
    except ApiError as exc:
        if "404" not in str(exc):
            raise

    if existing is None:
        payload = {
            "tag_name": args.tag,
            "name": args.name,
            "body": notes,
            "draft": False,
            "prerelease": False,
        }
        if args.target:
            payload["target_commitish"] = args.target
        release = request_json("POST", f"/repos/{args.repo}/releases", token, payload)
        print(f"已创建 Release {release['tag_name']}：{release['html_url']}")
    else:
        release = existing
        # 复用已有 Release 时同步发布说明与标题，避免说明停在旧版本。
        if release.get("body") != notes or release.get("name") != args.name:
            release = request_json(
                "PATCH",
                f"/repos/{args.repo}/releases/{release['id']}",
                token,
                {"name": args.name, "body": notes},
            )
            print(f"已更新 Release {release['tag_name']} 的发布说明")
        else:
            print(f"Release {args.tag} 的说明已是最新")

    uploaded = {a["name"] for a in release.get("assets", [])}
    for path in assets:
        if path.name in uploaded:
            print(f"跳过（附件已存在）：{path.name}")
            continue
        print(f"上传附件 {path.name}（{path.stat().st_size / 1024 / 1024:.2f} MB）…")
        asset = upload_asset(release["upload_url"], path, token)
        print(f"  完成：{asset['browser_download_url']}")

    final = request_json("GET", f"/repos/{args.repo}/releases/tags/{args.tag}", token)
    landed = {item["name"] for item in final.get("assets", [])}
    print(f"\nRelease {final['tag_name']} 附件：")
    for item in final.get("assets", []):
        print(f"  {item['name']}  {item['size'] / 1024 / 1024:.2f} MB  {item['browser_download_url']}")

    absent = [path.name for path in assets if path.name not in landed]
    if absent:
        print("× 这些附件没落到 Release 上：" + "、".join(absent))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
