"""只推「本地 HEAD 这一个提交」：远端父提交已在，只需上传变化的 blob + 建树。

push_via_api.py 会把本地全部历史（213 个提交 / 1328 个 blob）重建一遍，本机网络下要
一小时。这里利用「远端 master 正好是本地 HEAD 的父提交」这个前提，只补差异：

    本地 HEAD 的父 == 远端 master  →  上传变化 blob → 逐层建 tree → 建 commit → 改 ref

用法：python _scratch/push_one_commit.py [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xdao.github_api import ApiError, gh_token, linear_backoff, request_bytes  # noqa: E402

REPO = "XiaoFeng7418/xdao-export"
BRANCH = "master"


def git(*args: str) -> bytes:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return done.stdout


def api(token: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = request_bytes(
        method,
        path,
        token=token,
        payload=payload,
        timeout=180.0,
        user_agent="xdao-push-one",
        api_version="2022-11-28",
        attempts=5,
        backoff=lambda tries: linear_backoff(tries, limit=20.0),
        on_retry=lambda tries, wait, failure, last: print(
            f"    （网络失败，{wait:.0f} 秒后重试：{type(last).__name__}）", flush=True
        ),
        retry_error=lambda exc, detail: ApiError(f"{method} {path} -> {exc.code} {detail}"),
        gave_up=lambda last: f"{method} {path} 连续失败：{last}",
        chain_gave_up=True,
    )
    return json.loads(body) if body else {}


def parse_ident(line: str) -> dict:
    """'Name <mail> 1792... +0800' -> GitHub 要的 {"name","email","date"}。"""
    who, stamp, offset = line.rsplit(" ", 2)
    name, _, email = who.partition(" <")
    sign = 1 if offset[0] == "+" else -1
    tz = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])))
    when = datetime.fromtimestamp(int(stamp), tz=tz).isoformat()
    return {"name": name, "email": email.rstrip(">"), "date": when}


def commit_parts() -> tuple[str, str, dict, dict, str]:
    raw = git("cat-file", "commit", "HEAD").decode("utf-8")
    headers, _, message = raw.partition("\n\n")
    parent = ""
    author = committer = None
    for line in headers.split("\n"):
        if line.startswith("parent "):
            parent = line.split(" ", 1)[1]
        elif line.startswith("author "):
            author = parse_ident(line[len("author "):])
        elif line.startswith("committer "):
            committer = parse_ident(line[len("committer "):])
    assert parent and author and committer, "提交头解析失败"
    return parent, "", author, committer, message


def local_entries() -> list[tuple[str, str, str]]:
    """[(mode, sha, path)]，只取 blob（目录由路径推出来）。"""
    out = []
    for chunk in git("ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if not chunk:
            continue
        meta, _, path = chunk.partition(b"\t")
        mode, _kind, sha = meta.decode().split()
        out.append((mode, sha, path.decode("utf-8")))
    return out


def build_tree(node: dict, token: str, remote_blobs: dict[str, str], dry: bool) -> str:
    entries = []
    for name in sorted(node):
        value = node[name]
        if isinstance(value, dict):
            entries.append(
                {
                    "path": name,
                    "mode": "040000",
                    "type": "tree",
                    "sha": build_tree(value, token, remote_blobs, dry),
                }
            )
            continue
        mode, sha, path = value
        if remote_blobs.get(path) == sha:
            entries.append({"path": name, "mode": mode, "type": "blob", "sha": sha})
            continue
        data = git("cat-file", "blob", sha)
        print(f"  上传 {path}（{len(data)} 字节）", flush=True)
        if dry:
            new_sha = sha
        else:
            created = api(
                token,
                "POST",
                f"/repos/{REPO}/git/blobs",
                {"content": base64.b64encode(data).decode(), "encoding": "base64"},
            )
            new_sha = created["sha"]
        entries.append({"path": name, "mode": mode, "type": "blob", "sha": new_sha})
    if dry:
        return "dry-run-tree"
    return api(token, "POST", f"/repos/{REPO}/git/trees", {"tree": entries})["sha"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = gh_token()
    head = git("rev-parse", "HEAD").decode().strip()
    parent = git("rev-parse", "HEAD^").decode().strip()

    ref = api(token, "GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")
    remote_sha = ref["object"]["sha"]
    print(f"本地 HEAD {head[:10]}，父 {parent[:10]}，远端 {remote_sha[:10]}")
    if remote_sha == head:
        print("远端已经就是这个提交，无需推送。")
        return 0
    if remote_sha != parent:
        print(f"远端不是本地父提交（{remote_sha[:10]} != {parent[:10]}），本脚本不适用。")
        return 2

    parent_commit = api(token, "GET", f"/repos/{REPO}/git/commits/{parent}")
    remote_tree = api(
        token, "GET", f"/repos/{REPO}/git/trees/{parent_commit['tree']['sha']}?recursive=1"
    )
    remote_blobs = {
        entry["path"]: entry["sha"] for entry in remote_tree["tree"] if entry["type"] == "blob"
    }
    print(f"远端树 {len(remote_blobs)} 个文件")

    root: dict = {}
    for mode, sha, path in local_entries():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, sha, path)

    tree_sha = build_tree(root, token, remote_blobs, args.dry_run)
    print(f"新树 {tree_sha}")
    if args.dry_run:
        print("dry-run：未创建提交、未改 ref。")
        return 0

    _, _, author, committer, message = commit_parts()
    created = api(
        token,
        "POST",
        f"/repos/{REPO}/git/commits",
        {
            "message": message,
            "tree": tree_sha,
            "parents": [parent],
            "author": author,
            "committer": committer,
        },
    )
    new_sha = created["sha"]
    print(f"新提交 {new_sha[:10]}")
    api(
        token,
        "PATCH",
        f"/repos/{REPO}/git/refs/heads/{BRANCH}",
        {"sha": new_sha, "force": True},
    )
    check = api(token, "GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")
    actual = check["object"]["sha"]
    print(f"回读 ref：{actual[:10]}")
    return 0 if actual == new_sha else 1


if __name__ == "__main__":
    raise SystemExit(main())
