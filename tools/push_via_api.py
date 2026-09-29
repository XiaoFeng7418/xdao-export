"""用 GitHub Git Data API 把本地提交推送到远端。

为什么需要它：本机受限环境下 git 的 HTTPS 传输不可用
（schannel 拿不到 TLS 凭据、openssl 连接被重置），但 gh 的 API 通道正常。
于是改为直接调用 Git Data API 重建对象。

关键点：Git 对象是内容寻址的。只要 blob 内容、树结构、提交元数据
（tree / parent / author / committer / 时间 / 提交说明）完全一致，
重建出来的 commit sha 就与本地**完全相同**——脚本会逐对象校验这一点。

用法：
    python tools/push_via_api.py --repo XiaoFeng7418/xdao-export --branch master
    python tools/push_via_api.py --repo ... --branch main --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
GH = r"C:\Users\14515\Documents\Codex\2026-09-05\w-x\work\ghcli\bin\gh.exe"


class ApiError(RuntimeError):
    pass


def gh_token() -> str:
    result = subprocess.run([GH, "auth", "token"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise ApiError(f"取不到 gh 令牌：{result.stderr.strip()}")
    return result.stdout.strip()


def api(token: str, method: str, path: str, payload: dict | None = None,
        retries: int = 4) -> dict:
    """调用 GitHub API。网络抖动在本机很常见，因此默认重试若干次。"""
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "xdao-push")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            # 5xx 与 429 值得重试；4xx 属于确定性错误（除非是限流）。
            if exc.code in (429,) or 500 <= exc.code < 600:
                last_error = ApiError(f"{method} {path} -> {exc.code} {detail}")
            else:
                raise ApiError(f"{method} {path} -> {exc.code}\n{detail}") from exc
        except Exception as exc:  # 超时、连接重置、SSL EOF 都重试
            last_error = exc
        if attempt < retries:
            wait = min(20.0, 2.0 * (attempt + 1))
            print(f"    （网络失败，{wait:.0f} 秒后重试 {attempt + 2}/{retries + 1}：{type(last_error).__name__}）")
            time.sleep(wait)
    raise ApiError(f"{method} {path} 连续 {retries + 1} 次失败：{last_error}") from last_error


def git(*args: str) -> bytes:
    result = subprocess.run(["git", *args], capture_output=True)
    if result.returncode != 0:
        raise ApiError(f"git {' '.join(args)} 失败：{result.stderr.decode('utf-8', 'replace')}")
    return result.stdout


def local_commits() -> list[dict]:
    """按从旧到新的顺序读取本地全部提交及其树结构。"""
    shas = git("rev-list", "--reverse", "HEAD").decode().split()
    commits = []
    for sha in shas:
        meta = git(
            "show", "-s",
            "--format=%T%n%P%n%an%n%ae%n%aI%n%cn%n%ce%n%cI",
            sha,
        ).decode("utf-8").split("\n")
        message = git("log", "-1", "--pretty=%B", sha).decode("utf-8").rstrip("\n")
        tree_shas = git("ls-tree", "-r", "-z", sha)
        entries = []
        for raw in tree_shas.split(b"\0"):
            if not raw:
                continue
            head, _, name = raw.partition(b"\t")
            mode, otype, osha = head.decode().split()
            entries.append(
                {"mode": mode, "type": otype, "sha": osha, "path": name.decode("utf-8")}
            )
        commits.append(
            {
                "sha": sha,
                "tree": meta[0],
                "parents": meta[1].split() if meta[1].strip() else [],
                "author_name": meta[2],
                "author_email": meta[3],
                "author_date": meta[4],
                "committer_name": meta[5],
                "committer_email": meta[6],
                "committer_date": meta[7],
                "message": message,
                "entries": entries,
            }
        )
    return commits


def blob_content(sha: str) -> bytes:
    return git("cat-file", "blob", sha)


def build_path_tree(entries: list[dict], repo: str, token: str, cache: dict) -> str:
    """按路径逐层构建树，返回根树 sha。

    结构与文件分开存放：同名的情况下（例如既有 foo 文件又有 foo/ 目录）
    也不会互相覆盖，同时避免把文件条目误当成子树。
    """
    root: dict = {"files": {}, "dirs": {}}
    for entry in entries:
        parts = entry["path"].split("/")
        node = root
        for part in parts[:-1]:
            node = node["dirs"].setdefault(part, {"files": {}, "dirs": {}})
        node["files"][parts[-1]] = entry

    def create_tree(node: dict) -> str:
        items = []
        for name, entry in node["files"].items():
            items.append(
                {
                    "path": name,
                    "mode": entry["mode"],
                    "type": "blob",
                    "sha": entry["sha"],
                }
            )
        for name, child in node["dirs"].items():
            items.append(
                {
                    "path": name,
                    "mode": "040000",
                    "type": "tree",
                    "sha": create_tree(child),
                }
            )
        # Git 的树条目按「名字 + 类型」排序：目录名视同带一个 "/" 后缀。
        items.sort(key=lambda item: item["path"] + ("/" if item["type"] == "tree" else ""))
        result = api(token, "POST", f"/repos/{repo}/git/trees", {"tree": items})
        cache.setdefault("trees", set()).add(result["sha"])
        return result["sha"]

    return create_tree(root)


def upload_blobs(
    commits: list[dict], repo: str, token: str, known: set[str], exclude: set[str]
) -> int:
    """上传本地有、远端还没有的 blob（按 sha 去重）。返回上传个数。"""
    unique: dict[str, dict] = {}
    for commit in commits:
        for entry in commit["entries"]:
            if entry["path"] in exclude:
                continue
            unique.setdefault(entry["sha"], entry)
    uploaded = 0
    total = len(unique)
    for index, (sha, entry) in enumerate(unique.items(), start=1):
        if sha in known:
            continue
        content = blob_content(sha)
        payload = {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        }
        result = api(token, "POST", f"/repos/{repo}/git/blobs", payload)
        if result.get("sha") != sha:
            raise ApiError(f"blob sha 不一致：本地 {sha} / 远端 {result.get('sha')}")
        known.add(sha)
        uploaded += 1
        print(f"    [{index}/{total}] {sha[:8]} {entry['path']} ({len(content)} 字节)")
    return uploaded


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="用 GitHub API 推送本地提交")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--branch", required=True, help="远端分支名")
    parser.add_argument("--dry-run", action="store_true", help="只校验，不写远端")
    parser.add_argument("--expect-head", default="", help="远端当前分支 sha，不符则中止")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="不推送到远端的路径（可重复）；用于把体积很大的构建产物留在本地",
    )
    args = parser.parse_args(argv)
    exclude = set(args.exclude)

    repo_root = Path(__file__).resolve().parent.parent
    import os

    os.chdir(repo_root)

    token = gh_token()
    commits = local_commits()
    print(f"本地共 {len(commits)} 个提交，目标 {args.repo}@{args.branch}")

    # 远端已有对象，避免重复上传
    known_blobs: set[str] = set()
    try:
        head = api(token, "GET", f"/repos/{args.repo}/git/ref/heads/{args.branch}")
        remote_head = head["object"]["sha"]
        print(f"远端 {args.branch} 当前指向 {remote_head[:8]}")
        if args.expect_head and remote_head != args.expect_head:
            raise ApiError(f"远端分支 sha 与预期不符：{remote_head} != {args.expect_head}")
    except ApiError as exc:
        if "404" in str(exc):
            remote_head = ""
            print(f"远端还没有 {args.branch} 分支，将新建")
        else:
            raise

    # 收集远端已有 blob（用最后一个提交的树，避免重复传输大文件）
    if remote_head:
        try:
            remote_tree = api(token, "GET", f"/repos/{args.repo}/git/trees/{remote_head}?recursive=1")
            for item in remote_tree.get("tree", []):
                if item.get("type") == "blob":
                    known_blobs.add(item["sha"])
            print(f"远端已有 {len(known_blobs)} 个 blob 可复用")
        except ApiError as exc:
            print(f"（读取远端树失败，将重新上传全部 blob：{exc}）")

    print("1) 上传缺失的 blob")
    if exclude:
        print(f"   按约定排除：{'、'.join(sorted(exclude))}（只存在于本地与 Release 附件）")
    uploaded = upload_blobs(commits, args.repo, token, known_blobs, exclude)
    print(f"   本次上传 {uploaded} 个 blob")

    print("2) 构建树与提交")
    parent = remote_head or None
    cache: dict = {}
    new_shas = []
    for commit in commits:
        entries = [e for e in commit["entries"] if e["path"] not in exclude]
        tree_sha = build_path_tree(entries, args.repo, token, cache)
        if not exclude and tree_sha != commit["tree"]:
            raise ApiError(f"树 sha 不一致：本地 {commit['tree']} / 重建 {tree_sha}")
        payload = {
            "message": commit["message"],
            "tree": tree_sha,
            "author": {
                "name": commit["author_name"],
                "email": commit["author_email"],
                "date": commit["author_date"],
            },
            "committer": {
                "name": commit["committer_name"],
                "email": commit["committer_email"],
                "date": commit["committer_date"],
            },
        }
        if parent:
            payload["parents"] = [parent]
        if args.dry_run:
            print(f"    [演练] 将创建提交（父 {parent[:8] if parent else '无'}）：{commit['message'].splitlines()[0][:50]}")
            parent = "DRYRUN"
            continue
        result = api(token, "POST", f"/repos/{args.repo}/git/commits", payload)
        created = result["sha"]
        if not exclude:
            # 只有完整复刻时才可能、也应该得到与本地相同的 sha。
            if created != commit["sha"]:
                raise ApiError(
                    f"commit sha 不一致：本地 {commit['sha']} / 远端 {created}\n"
                    "说明提交元数据有差异，请检查 author/committer 与时间。"
                )
            print(f"    提交 {created[:8]} 与本地一致 ✓  {commit['message'].splitlines()[0][:46]}")
        else:
            print(
                f"    提交 {created[:8]}（本地 {commit['sha'][:8]}，"
                f"因排除项内容不同）  {commit['message'].splitlines()[0][:40]}"
            )
        new_shas.append(created)
        parent = created

    if args.dry_run:
        print("演练结束，未改动远端。")
        return 0

    print("3) 更新分支引用")
    final = new_shas[-1]
    if remote_head:
        api(token, "PATCH", f"/repos/{args.repo}/git/refs/heads/{args.branch}",
            {"sha": final, "force": True})
    else:
        api(token, "POST", f"/repos/{args.repo}/git/refs",
            {"ref": f"refs/heads/{args.branch}", "sha": final})
    print(f"   {args.branch} -> {final[:8]}")
    print("完成，远端提交与本地逐一对应。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
