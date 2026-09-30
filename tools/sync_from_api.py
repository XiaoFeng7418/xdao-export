"""把远端分支的提交图在本地逐字节重建，使本地与远端完全对齐。

用途：当本机 git 的 HTTPS 传输不可用、只能通过 API 推送时，本地与远端会产生
两套不同的 commit sha（排除大文件会改变树，重建历史又会换掉父提交）。
这个脚本用 API 读回远端对象，在本地重建出**完全相同**的 blob / tree / commit，
再把本地分支指过去，之后无论用 git 还是 API 推送都能正常续接。

用法：
    python tools/sync_from_api.py --repo XiaoFeng7418/xdao-export --branch master
    python tools/sync_from_api.py --repo ... --branch master --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"


class ApiError(RuntimeError):
    pass


def gh_token() -> str:
    """取 GitHub 令牌：优先环境变量，其次 PATH 上的 gh（可用 XDAO_GH 指定路径）。"""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    gh = os.environ.get("XDAO_GH") or "gh"
    try:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ApiError(f"找不到 gh（{gh}）；请先 gh auth login，或设置 XDAO_GH") from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise ApiError("取不到 gh 令牌，请先 gh auth login")
    return out.stdout.strip()


def api(token: str, path: str, retries: int = 5) -> dict:
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(API + path)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "xdao-sync")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (429,) or 500 <= exc.code < 600:
                last = exc
            else:
                raise ApiError(f"GET {path} -> {exc.code}: {exc.read()[:200]!r}") from exc
        except Exception as exc:
            last = exc
        if attempt < retries:
            time.sleep(min(20.0, 2.0 * (attempt + 1)))
    raise ApiError(f"GET {path} 连续失败：{last}")


def git(*args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *args], input=data, capture_output=True)
    if result.returncode != 0:
        raise ApiError(f"git {' '.join(args)} 失败：{result.stderr.decode('utf-8', 'replace')}")
    return result.stdout


def git_ident(api_date: str, name: str, email: str, offset: str) -> str:
    """把 API 的时间转成 git 身份行。

    git 在提交对象里把时间写成「unix 时间戳 + 时区偏移」。API 返回的 ISO 时间
    已经是同一时刻，时间戳部分确定；偏移写法取决于当初创建提交时的时区
    （实测 GitHub 会保留推送方的本地偏移，例如 +0800）。
    """
    moment = datetime.fromisoformat(api_date.replace("Z", "+00:00"))
    return f"{name} <{email}> {int(moment.timestamp())} {offset}"


def write_commit(remote: dict, tree: str) -> str:
    """重建提交对象，返回 sha。

    提交对象的精确字节会影响 sha，而 API 不返回原始字节，所以这里按可能的
    差异逐个尝试（时区偏移写法、提交说明末尾是否带换行），命中远端 sha 为止。
    """
    author = remote["author"]
    committer = remote.get("committer") or author
    message = remote["message"]
    offsets = ("+0800", "+0000")

    for auth_off in offsets:
        for comm_off in offsets:
            lines = [f"tree {tree}"]
            for parent in remote.get("parents") or []:
                lines.append(f"parent {parent['sha']}")
            lines.append(
                "author "
                + git_ident(author["date"], author["name"], author["email"], auth_off)
            )
            lines.append(
                "committer "
                + git_ident(committer["date"], committer["name"], committer["email"], comm_off)
            )
            lines.append("")
            head = ("\n".join(lines) + "\n").encode("utf-8")
            for trailing in (False, True):
                body = head + message.encode("utf-8")
                if trailing and not body.endswith(b"\n"):
                    body += b"\n"
                created = (
                    git("hash-object", "-w", "-t", "commit", "--stdin", data=body)
                    .decode()
                    .strip()
                )
                if created == remote["sha"]:
                    return created
    raise ApiError(
        f"提交不一致：远端 {remote['sha']}，各种偏移/换行组合都无法复现；"
        "请检查作者信息或提交说明是否被改动"
    )


def write_blob(content: bytes) -> str:
    """把内容写成松散对象，返回 blob sha。"""
    return git("hash-object", "-w", "--stdin", data=content).decode().strip()


def write_tree(items: list[dict]) -> str:
    """用 mktree 写树。items 为 (mode, type, sha, name)。"""
    lines = []
    for item in items:
        lines.append(f'{item["mode"]} {item["type"]} {item["sha"]}\t{item["name"]}')
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    return git("mktree", data=payload).decode().strip()


def build_tree(token: str, repo: str, tree_sha: str, prefix: str = "") -> str:
    """递归重建一棵树及其所有 blob，返回新树的 sha（应与远端一致）。

    注意：读的是**非递归**的树接口，条目的字段是 path（即名字）；
    递归接口返回的是完整相对路径，不适合直接拿来建树。
    """
    data = api(token, f"/repos/{repo}/git/trees/{tree_sha}")
    if data.get("truncated"):
        raise ApiError(f"树被截断，无法完整重建：{prefix or '/'}")
    items = []
    for entry in data["tree"]:
        name = entry["path"]
        if entry["type"] == "tree":
            sha = build_tree(token, repo, entry["sha"], f"{prefix}{name}/")
        else:
            blob = api(token, f"/repos/{repo}/git/blobs/{entry['sha']}")
            content = base64.b64decode(blob["content"])
            sha = write_blob(content)
            if sha != entry["sha"]:
                raise ApiError(
                    f"blob 不一致：{prefix}{name} 远端 {entry['sha']} 本地 {sha}"
                )
        items.append(
            {
                "mode": entry["mode"],
                "type": entry["type"],
                "sha": sha,
                "name": name,
            }
        )
    created = write_tree(items)
    if created != tree_sha:
        raise ApiError(f"树不一致：{prefix or '/'} 远端 {tree_sha} 本地 {created}")
    return created


def commit_chain(token: str, repo: str, head: str) -> list[dict]:
    chain = []
    sha = head
    while sha:
        data = api(token, f"/repos/{repo}/git/commits/{sha}")
        chain.append(data)
        parents = data.get("parents") or []
        sha = parents[0]["sha"] if parents else ""
    chain.reverse()
    return chain


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="用 API 数据在本地重建远端提交")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    import os

    os.chdir(root)

    token = gh_token()
    head = api(token, f"/repos/{args.repo}/git/ref/heads/{args.branch}")["object"]["sha"]
    print(f"远端 {args.branch} = {head[:8]}")

    chain = commit_chain(token, args.repo, head)
    print(f"提交链共 {len(chain)} 个，开始重建对象")

    rebuilt: list[str] = []
    for index, remote in enumerate(chain, start=1):
        tree = build_tree(token, args.repo, remote["tree"]["sha"])
        created = write_commit(remote, tree)
        rebuilt.append(created)
        print(f"  [{index}/{len(chain)}] {created[:8]} 与远端一致 ✓  "
              f"{remote['message'].splitlines()[0][:44]}")

    if args.dry_run:
        print("演练结束，未改动本地引用。")
        return 0

    print(f"把本地 {args.branch} 指向 {rebuilt[-1][:8]}")
    git("update-ref", f"refs/heads/{args.branch}", rebuilt[-1])
    print("完成。可执行 git status 确认工作区内容一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
