"""把远端分支的提交图在本地逐字节重建，使本地与远端完全对齐。

用途：当本机 git 的 HTTPS 传输不可用、只能通过 API 推送时，本地与远端会产生
两套不同的 commit sha（排除大文件会改变树，重建历史又会换掉父提交）。
这个脚本用 API 读回远端对象，在本地重建出**完全相同**的 blob / tree / commit，
再把本地分支指过去，之后无论用 git 还是 API 推送都能正常续接。

用法：
    python tools/sync_from_api.py --repo XiaoFeng7418/xdao-export --branch master
    python tools/sync_from_api.py --repo ... --branch master --dry-run

默认在**这个脚本所在的仓库**里重建；`XDAO_REPO_DIR` 可以改指别处（只有测试会用到）。
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 「怎么问 GitHub」的真源在 xdao/github_api.py；脚本要能直接
# `python tools/sync_from_api.py` 跑，所以先把仓库根挂进 sys.path 再 import。
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xdao.github_api import (  # noqa: E402  （必须在上面那段 sys.path 之后）
    API_BASE,
    ApiError,
    gh_token as _gh_token,
    git as _git,
    linear_backoff,
    request_bytes,
)

API = API_BASE

#: ``gh_token`` 原先这份与 make_release 那份逐字相同，直接共用同一个实现。
gh_token = _gh_token


def api(token: str, path: str, retries: int = 5) -> dict:
    """GET 一个接口路径，返回解析好的 JSON。

    重试与退避的实现在 :func:`xdao.github_api.request_bytes`；这里保留原先的
    等待曲线（``min(20, 2×第几次)``、共 ``retries + 1`` 次尝试）、``xdao-sync``
    的 User-Agent、**重试期间不打印任何东西**，以及确定性错误里那句
    ``f"GET {path} -> {code}: {body!r}"``（正文取前 200 字节的 repr）。
    """
    body = request_bytes(
        "GET",
        path,
        token=token,
        timeout=180.0,
        user_agent="xdao-sync",
        attempts=retries + 1,
        backoff=lambda tries: linear_backoff(tries, limit=20.0),
        detail_bytes=200,
        detail_repr=True,
        http_failure=lambda code, detail: f"GET {path} -> {code}: {detail}",
    )
    return json.loads(body)


#: ``git`` 子进程封装与 push_via_api 那份是同一段逻辑，合并到 github_api。
git = _git


def worktree_changes() -> list[str]:
    """列出工作区里与 HEAD 不一样的**已跟踪**文件（未跟踪的不管）。

    盘点之后要核对「本地是不是真的跟远端一样」：引用指过去了、工作区却没对齐，
    是两回事 —— 只报「完成」就等于把这件事蒙混过去。
    """
    out = git("status", "--porcelain", "--untracked-files=no").decode("utf-8", "replace")
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def tracked_files() -> list[str]:
    """已跟踪文件的清单（用来把「核对过了」说清楚到底核对了多少）。"""
    out = git("ls-files").decode("utf-8", "replace")
    return [line for line in out.splitlines() if line.strip()]


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
            # 超过 1 MB 的 blob，JSON 接口只给一个空壳（encoding 不是 base64），
            # 必须换 raw 媒体类型单独取 —— 拿不到内容就没法核对，直接停下，
            # 不要让它变成一句莫名其妙的「blob 不一致」。
            if blob.get("encoding") != "base64":
                raise ApiError(
                    f"取不到 blob 内容：{prefix}{name}（encoding="
                    f"{blob.get('encoding')!r}）—— 超过 1 MB 的文件 JSON 接口不给内容，"
                    "要改用 raw 媒体类型单独取"
                )
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
    """读回从 head 起可达的全部提交，按「父先于子」排序（head 在最后）。

    以前只跟着**第一个**父提交往回走：远端一旦有过合并提交，另一条支线上的提交
    就不会被重建，引用照样指过去，本地仓库却缺对象 —— 而且 `git status` 看不出来。
    现在每个父提交都跟着走，并按拓扑序返回，保证建某个提交时它的父提交都已建好。
    """
    commits: dict[str, dict] = {}
    pending = [head]
    while pending:
        sha = pending.pop()
        if sha in commits:
            continue
        data = api(token, f"/repos/{repo}/git/commits/{sha}")
        commits[sha] = data
        pending.extend(parent["sha"] for parent in (data.get("parents") or []))

    order: list[str] = []
    seen: set[str] = set()
    stack = [(head, False)]
    while stack:
        sha, expanded = stack.pop()
        if expanded:
            order.append(sha)
            continue
        if sha in seen:
            continue
        seen.add(sha)
        stack.append((sha, True))
        for parent in commits[sha].get("parents") or []:
            stack.append((parent["sha"], False))
    return [commits[sha] for sha in order]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="用 API 数据在本地重建远端提交")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(
        os.environ.get("XDAO_REPO_DIR") or Path(__file__).resolve().parent.parent
    ).resolve()

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
        print(
            "演练结束：本地分支引用一个都没动"
            "（为核对 sha，上面这些 blob/tree/commit 松散对象已经写进本地 .git 了，"
            "它们是未引用的，`git gc` 会自行回收）。"
        )
        return 0

    print(f"把本地 {args.branch} 指向 {rebuilt[-1][:8]}")
    git("update-ref", f"refs/heads/{args.branch}", rebuilt[-1])

    dirty = worktree_changes()
    if dirty:
        print("× 引用已经指过去了，但工作区里这些已跟踪文件与新的 HEAD 不一样：")
        print("  " + "、".join(dirty[:5]) + ("…" if len(dirty) > 5 else ""))
        print("  别当同步完成 —— 先看 `git status` 把这些改动处理掉。")
        return 1
    print(f"完成：本地 {args.branch} 与远端逐字节一致，工作区里 {len(tracked_files())} "
          "个已跟踪文件也逐一对得上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
