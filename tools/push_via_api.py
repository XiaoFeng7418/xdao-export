"""用 GitHub Git Data API 把本地提交推送到远端。

为什么还需要它：本机 git 的 HTTPS 传输长期不可用（schannel 拿不到 TLS 凭据、
openssl 连接被重置），但 gh 的 API 通道一直正常，于是改为直接调用 Git Data API
重建对象。**2026-09-30 起本机配好了代理（127.0.0.1:7890），`git push` 已经可用**，
日常推送优先用 git；本脚本保留给代理不可用时的备用通道，也用于需要逐对象校验的场景。

注意：走 API 创建提交时，远端对象是 GitHub 按收到的字段重新生成的，**对象字节由
GitHub 决定**（它保留我们送上去的时区偏移，但接口读回来的日期一律被改写成 UTC）。
所以：
- 本地对象的 sha **能**逐字节复算校验（`git cat-file commit` 原文 + `git hash-object`）；
- 远端对象的 sha **无法**从接口返回值复算 —— 原始偏移量（本机 `+0800`）在接口里
  已经丢失，而偏移量是提交对象的一部分。这不是"远端历史被篡改"，只是写法不同。
- **提交说明的结尾换行也是对象的一部分，而且本地历史里两种形态都有**：多数提交
  以 `\n` 收尾，少数没有。所以消息必须用 `git cat-file commit` 原样取，
  不能用 `git log --pretty=%B`（它会擅自补一个换行）。
- 想让两边 sha 完全一致，只能用 `git push` 把本地对象原样送上去（代理已配好）。

用法：
    python tools/push_via_api.py --repo XiaoFeng7418/xdao-export --branch master
    python tools/push_via_api.py --repo ... --branch main --dry-run

`--dry-run` 不会创建提交、也不会动分支引用，但为了核对「重建出来的树与本地一致」，
它**仍会在远端留下未引用的 blob/tree 对象**（看不到、不影响任何分支，GitHub 会自行
回收）—— 所以它不是"完全不写远端"，别把它当只读命令用。
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
from datetime import datetime, timedelta, timezone
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
        result = subprocess.run([gh, "auth", "token"], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ApiError(f"找不到 gh（{gh}）；请先 gh auth login，或设置 XDAO_GH") from exc
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


def commit_message(sha: str) -> str:
    """原样取出一条提交的说明（逐字节，含或不含结尾换行都保持原样）。

    不能用 `git log --pretty=%B`：它会给没有结尾换行的说明擅自补一个 `\n`，
    于是拼出来的对象多一个字节、sha 与本地对不上（本地历史里确实两种都有）。
    """
    raw = git("cat-file", "commit", sha)
    _, _, body = raw.partition(b"\n\n")
    return body.decode("utf-8")


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
        # 提交说明必须原样取（见 commit_message 的注释：%B 会补换行，不能用来拼对象）。
        message = commit_message(sha)
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
                # git 提交对象的原始写法（`{epoch} {±HHMM}`），拼对象字节时用它；
                # 送 Git Data API 仍用上面的 ISO 写法（两种写法接口都收）。
                "author_date_git": git_date(meta[4]),
                "committer_date_git": git_date(meta[7]),
                "message": message,
                "entries": entries,
            }
        )
    return commits


def remote_chain(repo: str, token: str, head: str, limit: int = 100) -> list[dict]:
    """取远端分支上最近的若干提交（从旧到新），用于判断哪些已经推过。"""
    if not head:
        return []
    chain = []
    sha = head
    while sha and len(chain) < limit:
        data = api(token, "GET", f"/repos/{repo}/git/commits/{sha}")
        chain.append(data)
        parents = data.get("parents") or []
        sha = parents[0]["sha"] if parents else ""
    chain.reverse()
    return chain


def _same_email(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def _same_instant(a: str, b: str) -> bool:
    """比较两个时间是否为同一时刻（容忍时区写法不同，例如 +08:00 与 Z）。"""
    from datetime import datetime

    if not a or not b:
        return False
    try:
        left = datetime.fromisoformat(a.replace("Z", "+00:00"))
        right = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return a == b
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left == right


def git_ident(date_text: str, name: str, email: str) -> str:
    """把任意写法的时间转成 git 提交对象里的 `{epoch} {±HHMM}` 写法。

    实测（2026-09-30）：GitHub 的提交对象**会保留**我们送上去的偏移量，
    例如送 `2026-09-05T22:44:23+08:00` 存下来就是 `1788619463 +0800`；
    但 **接口读回来的 date 一律被改写成 `...Z`**。所以偏移量从接口那边
    是拿不回来的 —— 这也正是「树一样、sha 不一样」无法从接口侧弥合的根因。
    """
    return f"{name} <{email}> {git_date(date_text)}"


def git_date(date_text: str) -> str:
    """把带时区的时间写成 git 提交对象里的 `{epoch} {±HHMM}`。"""
    moment = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    total = int((moment.utcoffset() or timedelta(0)).total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{int(moment.timestamp())} {sign}{total // 3600:02d}{(total % 3600) // 60:02d}"


def _api_date_to_git(value: str) -> str:
    """把 GitHub 返回的 ISO 时间转成 git 提交对象里的写法。"""
    from datetime import datetime

    if not value:
        return ""
    text = value.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    offset = moment.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{int(moment.timestamp())} {sign}{total // 3600:02d}{(total % 3600) // 60:02d}"


def git_commit_object(commit: dict, *, tree: str, parents: list[str], message: str) -> bytes:
    """按 git 的规范拼出提交对象的原始字节（用于自己算 sha）。

    时间必须用 `{epoch} {±HHMM}` 写法；commit 字典里的 `author_date` 是 ISO 写法
    （那是喂给 API 的），所以优先用 `author_date_git`。
    """
    author_date = commit.get("author_date_git") or git_date(commit["author_date"])
    committer_date = commit.get("committer_date_git") or git_date(commit["committer_date"])
    lines = [f"tree {tree}"]
    lines += [f"parent {p}" for p in parents]
    lines.append(f"author {commit['author_name']} <{commit['author_email']}> {author_date}")
    lines.append(
        f"committer {commit['committer_name']} <{commit['committer_email']}> {committer_date}"
    )
    # 提交对象 = 头部 + 一个空行 + 提交说明，说明按原样拼，**不要补也不要删换行**。
    # 实测（2026-09-30）本地历史里两种形态都有：多数提交的说明以 `\n` 收尾，
    # 但有 7 条没有（`6749e5bc`、`67847a7b`、`a7e00042`、`bc94f2fa`、`93fb0f53`、
    # `6d0b59aa`、`5e7713e3`）。所以消息必须来自 `git cat-file commit`（逐字节原样），
    # 不能用 `git log --pretty=%B` —— 后者会擅自补一个换行，导致 sha 算不对。
    return ("\n".join(lines) + "\n\n" + message).encode("utf-8")


def commit_object_probe(commit: dict, *, tree: str, parents: list[str]) -> str:
    """返回一段人类可读的说明：这条提交的对象字节如何复算出来。

    用于在没有 `git cat-file` 原文字节时（例如校验远端对象）判断差异出在哪。
    """
    body = commit["message"]
    candidates = {
        "原样": body,
        "去掉尾换行": body.rstrip("\n"),
        "补一个尾换行": body if body.endswith("\n") else body + "\n",
    }
    tried = []
    for label, text in candidates.items():
        raw = git_commit_object(commit, tree=tree, parents=parents, message=text)
        tried.append(f"{label}={_sha_of(raw)[:8]}")
    return "候选复算：" + "，".join(tried)


def _sha_of(raw: bytes) -> str:
    import hashlib

    return hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def diagnose_commit_mismatch(commit: dict, tree: str, remote: dict, created: str) -> str:
    """弄清"树一样但 sha 不一样"到底差在哪。

    两种情况要分开说，不然就是在猜：
      ① 本地这份能用「本地字段 + 本地父链」复算出本地 sha → 本地对象没毛病，
         那差异必然出在远端那份字节上；
      ② 远端那份的字节**无法**从接口返回值复算出来 → 因为接口把日期改写成 UTC
         （`...Z`），偏移量（例如 `+0800`）拿不回来，而偏移量是提交对象的一部分。
         这种情况只能靠 `git push` 把本地对象原样送上去，接口通道弥合不了。
    """
    parents = [p["sha"] for p in (remote.get("parents") or [])]
    remote_message = remote.get("message") or ""

    def sha_for(message: str, use_remote_meta: bool) -> str:
        payload = commit
        if use_remote_meta:
            payload = dict(commit)
            payload["author_name"] = (remote.get("author") or {}).get("name", commit["author_name"])
            payload["author_email"] = (remote.get("author") or {}).get("email", commit["author_email"])
            payload["author_date_git"] = _api_date_to_git((remote.get("author") or {}).get("date", ""))
            payload["committer_name"] = (remote.get("committer") or {}).get("name", commit["committer_name"])
            payload["committer_email"] = (remote.get("committer") or {}).get("email", commit["committer_email"])
            payload["committer_date_git"] = _api_date_to_git(
                (remote.get("committer") or {}).get("date", "")
            )
        return _sha_of(git_commit_object(payload, tree=tree, parents=parents, message=message))

    local_ok = sha_for(commit["message"], use_remote_meta=False) == commit["sha"]
    if not local_ok:
        return (
            "本地提交对象就无法复算（连本地 sha 都对不上）——"
            "本地历史可能被重写过，请检查 `git log` 与工作区状态"
        )
    if sha_for(remote_message, use_remote_meta=True) == created:
        delta = "提交说明" if remote_message != commit["message"] else "作者/提交时间/父提交"
        return f"远端对象可复算，差异在：{delta}"
    return (
        "远端对象无法用接口返回值复算：接口返回的日期被改写成 UTC（`...Z`），"
        "原始时区偏移（本机是 `+0800`）丢失，而偏移量是提交对象的一部分。"
        "接口通道因此无法让两边 sha 对齐，需要用 `git push` 把本地对象原样送上去"
        "（本机已有可用代理，见文件头说明）"
    )


def _same_commit(local_commit: dict, remote_commit: dict) -> bool:
    remote_author = remote_commit.get("author") or {}
    return (
        local_commit["message"].strip() == (remote_commit.get("message") or "").strip()
        and _same_email(local_commit["author_email"], remote_author.get("email", ""))
        and _same_instant(local_commit["author_date"], remote_author.get("date", ""))
    )


def find_pushed_prefix(local: list[dict], remote: list[dict]) -> int:
    """返回「本地提交中已经推到远端」的个数。

    以提交说明 + 作者邮箱 + 作者时间比对：即使因为排除大文件、重写提交等原因
    导致 tree 与 sha 不同，也能认出同一条改动，从而只推送新增的部分。

    对齐方式：在远端链里找**能连续匹配最多个本地提交**的起点。
    远端链常比本地长（重跑推送、修正提交都会留下同名的旧副本），
    因此不能只认某一次匹配 —— 取最长匹配才不会漏判或误报。
    """
    if not local or not remote:
        return 0
    best = 0
    for start in range(len(remote)):
        if not _same_commit(local[0], remote[start]):
            continue
        matched = 0
        for offset, local_commit in enumerate(local):
            position = start + offset
            if position >= len(remote) or not _same_commit(local_commit, remote[position]):
                break
            matched += 1
        best = max(best, matched)
    return best


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


def unknown_excludes(commits: list[dict], exclude: set[str]) -> list[str]:
    """挑出 `--exclude` 里在本地历史中一个都没出现的路径（多半是写错了）。

    写错一个字母的代价是把本该留在本地的大文件传上去，所以调用方要在任何网络写入
    之前就停下（先验再动）。这里对着**全部**本地提交找，而不是只对着这次要推的那些：
    某个路径只出现在很早的提交里、这次推送里没有，是正常情况，不该报警。
    """
    seen = {entry["path"] for commit in commits for entry in commit["entries"]}
    return sorted(path for path in exclude if path not in seen)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="用 GitHub API 推送本地提交")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--branch", required=True, help="远端分支名")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验：不创建提交、不动分支引用（为核对树，仍会在远端留下未引用的 blob/tree 对象）",
    )
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

    # 先验再动：--exclude 的路径写错时，以前什么都不说，大文件照样被传上去。
    unknown = unknown_excludes(commits, exclude)
    if unknown:
        print("× --exclude 里的这些路径在本地历史里根本不存在：" + "、".join(unknown))
        print("  写错一个字母，本该留在本地的大文件就会被传上去 —— 先核对路径再重来。")
        return 1

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

    # 已经推过的提交不再重建，只推送新增部分（否则每次都会产生一套新的 sha）
    parent = remote_head or None
    pending = commits
    if remote_head:
        try:
            # 先看最直白的一种：远端顶端就是本地 HEAD（本地历史一长，
            # remote_chain 的 100 条窗口就装不下最早的提交，比对会误判成「一个都没推」）。
            if commits and remote_head == commits[-1]["sha"]:
                print("远端分支的顶端就是本地 HEAD，没有需要推送的新提交。")
                return 0
            chain = remote_chain(args.repo, token, remote_head)
            pushed = find_pushed_prefix(commits, chain)
            if pushed:
                print(f"远端已有 {pushed} 个提交与本地对应，从第 {pushed + 1} 个开始推送")
                pending = commits[pushed:]
            if not pending:
                print("没有需要推送的新提交，远端已是最新。")
                return 0
        except ApiError as exc:
            print(f"（读取远端历史失败，将按完整历史重建：{exc}）")
            parent = None

    print("1) 上传缺失的 blob")
    if exclude:
        print(f"   按约定排除：{'、'.join(sorted(exclude))}")
    uploaded = upload_blobs(pending, args.repo, token, known_blobs, exclude)
    print(f"   本次上传 {uploaded} 个 blob")

    print("2) 构建树与提交")
    cache: dict = {}
    new_shas = []
    sha_mismatch = False
    for commit in pending:
        entries = [e for e in commit["entries"] if e["path"] not in exclude]
        tree_sha = build_path_tree(entries, args.repo, token, cache)
        if tree_sha != commit["tree"] and not exclude:
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
        if created == commit["sha"]:
            # 完整复刻：连 sha 都与本地一致。
            print(f"    提交 {created[:8]} 与本地一致 ✓  {commit['message'].splitlines()[0][:46]}")
        elif tree_sha != commit["tree"]:
            # 有排除项时树内容与本地不同，sha 自然不同，这是预期内的。
            print(
                f"    提交 {created[:8]}（本地 {commit['sha'][:8]}，"
                f"内容因排除项与本地不同）  {commit['message'].splitlines()[0][:40]}"
            )
        else:
            # 树相同但 sha 不同：以前一律归因为"GitHub 规范化了时区"，
            # 这是错的——真正的根因多半是对象字节不一致（例如消息末尾换行被吃掉）。
            # 这里逐字段复算 sha，指出到底差在哪，避免把 bug 藏起来。
            reason = diagnose_commit_mismatch(commit, tree_sha, result, created)
            print(
                f"    提交 {created[:8]}（本地 {commit['sha'][:8]}，{reason}）"
                f"  {commit['message'].splitlines()[0][:40]}"
            )
            sha_mismatch = True
        new_shas.append(created)
        parent = created

    if args.dry_run:
        print(
            "演练结束：没有创建提交，也没有动分支引用"
            "（上面为核对树而建的 blob/tree 对象是未引用的，GitHub 会自行回收）。"
        )
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
    if sha_mismatch:
        print(
            "警告：有提交的 sha 与本地不一致（原因见上）。这会让本地与远端的提交"
            "\n      看起来像两条历史，`repo_check.py` 的「本地提交都已推送」也会误报。"
            "\n      确认远端文件与本地一致后，可用 `git push --force-with-lease` 让两边 sha 对齐。"
        )
    else:
        print("完成，远端提交与本地逐一对应（sha 完全相同）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
