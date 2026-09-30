"""创建 GitHub Release 并上传附件（走 Release 附件专用上传接口）。

背景：本机 git 的 HTTPS 传输不可用，只能通过 API 维护仓库；
Release 附件的上传接口与 Git Data API 的 blob 接口是两条不同的通道，
附件接口更适合上传打包好的 exe。

用法：
    python tools/make_release.py --repo owner/name --tag v0.2.0 ^
        --name "v0.2.0：四种格式 + 断点续传 + 串监控" ^
        --notes-file RELEASE_NOTES.md --asset dist/xxx.exe
"""

from __future__ import annotations

import argparse
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
    notes = Path(args.notes_file).read_text(encoding="utf-8")

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
    for raw in args.asset:
        path = Path(raw)
        if not path.exists():
            print(f"跳过（文件不存在）：{path}")
            continue
        if path.name in uploaded:
            print(f"跳过（附件已存在）：{path.name}")
            continue
        print(f"上传附件 {path.name}（{path.stat().st_size / 1024 / 1024:.2f} MB）…")
        asset = upload_asset(release["upload_url"], path, token)
        print(f"  完成：{asset['browser_download_url']}")

    final = request_json("GET", f"/repos/{args.repo}/releases/tags/{args.tag}", token)
    print(f"\nRelease {final['tag_name']} 附件：")
    for item in final.get("assets", []):
        print(f"  {item['name']}  {item['size'] / 1024 / 1024:.2f} MB  {item['browser_download_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
