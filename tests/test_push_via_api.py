"""tools/push_via_api.py：对象字节、已推过的判定，以及演练/排除项的边界。

这是代理不可用时的备用推送通道：它自己拼 git 对象、自己算 sha，再交给 Git Data API。
所以它的头号风险是「拼出来的字节与 git 不一致」—— 那样远端历史看起来就像被改写，
本地 sha 又复算不回来；另一半风险来自**静默做错事**：

- 提交说明若用 `git log --pretty=%B` 取，会给没有尾换行的说明补一个 `\\n`（本地历史里
  确实有这种提交），拼出的对象多一个字节，sha 就与本地对不上（只有
  `git cat-file commit` 是逐字节原样取法）；
- `--exclude` 写错一个字母时以前一声不响，本该留在本地的大文件照样会被传上去；
- `--dry-run` 的说明写着「只校验，不写远端」，其实它会为了核对树而在远端建 blob/tree
  对象 —— 现在三处都写清楚了。

下面拿真 git 当尺子（`git hash-object` / `git cat-file` / `git mktree`），跑 main() 时
换成假 API —— 一个请求都不出网。
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import push_via_api  # noqa: E402

GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_SYSTEM=os.devnull,
    GIT_AUTHOR_NAME="测试",
    GIT_AUTHOR_EMAIL="tester@example.com",
    GIT_COMMITTER_NAME="测试",
    GIT_COMMITTER_EMAIL="tester@example.com",
)

# 手写提交对象时用的身份（与 git_date 的取值无关，方便逐字节比对）
HANDMADE = {
    "author_name": "T",
    "author_email": "t@example.com",
    "author_date_git": "1788619463 +0800",
    "committer_name": "T",
    "committer_email": "t@example.com",
    "committer_date_git": "1788619463 +0800",
}

# 假 API 里唯一的内容：拿它算 blob sha，保证 upload_blobs 的校验能通过
CONTENT = "内容".encode("utf-8")


def git(*args: str, cwd: Path, text: bool = True):
    result = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8") if text else result.stdout


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def entry(sha: str, path: str, mode: str = "100644") -> dict:
    return {"mode": mode, "type": "blob", "sha": sha, "path": path}


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("第一版\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "第一条", cwd=repo)
    (repo / "dir").mkdir()
    (repo / "dir" / "b.txt").write_text("嵌套\n", encoding="utf-8")
    (repo / "z.txt").write_text("后面加的\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "第二条", cwd=repo)
    return repo


def parse_commit(repo: Path, sha: str) -> dict:
    """按 push_via_api 需要的字段读一条真提交（时间用 git 自己的写法）。"""
    raw = git("cat-file", "commit", sha, cwd=repo, text=False)
    header, _, body = raw.partition(b"\n\n")
    fields: dict[str, list[str]] = {}
    for line in header.decode("utf-8").splitlines():
        key, _, value = line.partition(" ")
        fields.setdefault(key, []).append(value)

    def split_ident(value: str) -> tuple[str, str, str]:
        name, _, rest = value.partition(" <")
        email, _, date = rest.partition("> ")
        return name, email, date.strip()

    author = split_ident(fields["author"][0])
    committer = split_ident(fields["committer"][0])
    return {
        "sha": sha,
        "tree": fields["tree"][0],
        "parents": fields.get("parent", []),
        "author_name": author[0],
        "author_email": author[1],
        "author_date_git": author[2],
        "committer_name": committer[0],
        "committer_email": committer[1],
        "committer_date_git": committer[2],
        "message": body.decode("utf-8"),
    }


def handmade_commit(repo: Path, tree: str, parents: list[str], message: str) -> str:
    """手写一个提交对象交给 git 算 sha。

    这样能造出 git 命令行不方便造的形状：说明没有尾换行、或者父提交不止一个。
    """
    text = (
        f"tree {tree}\n"
        + "".join(f"parent {parent}\n" for parent in parents)
        + "author T <t@example.com> 1788619463 +0800\n"
        + "committer T <t@example.com> 1788619463 +0800\n"
        + f"\n{message}"
    ).encode("utf-8")
    result = subprocess.run(
        ["git", "hash-object", "-w", "-t", "commit", "--stdin"],
        cwd=repo,
        env=GIT_ENV,
        input=text,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode().strip()


def remote_of(commit: dict, *, message: str | None = None, date: str | None = None) -> dict:
    return {
        "message": commit["message"] if message is None else message,
        "author": {
            "email": commit["author_email"],
            "date": commit["author_date"] if date is None else date,
        },
    }


def local_of(message: str, *, email: str = "tester@example.com",
             date: str = "2026-01-01T00:00:00+08:00", sha: str = "a" * 40) -> dict:
    return {
        "sha": sha,
        "message": message,
        "author_email": email,
        "author_date": date,
        "author_name": "测试",
        "committer_name": "测试",
        "committer_email": email,
        "committer_date": date,
        "entries": [],
    }


@pytest.fixture
def back_to_root():
    """main() 里会 os.chdir 到仓库根，跑完得把工作目录放回去。"""
    before = Path.cwd()
    yield
    os.chdir(before)


# --------------------------------------------------------------------------- 时间写法


@pytest.mark.parametrize(
    ("iso", "expected"),
    [
        ("2026-09-05T22:44:23+08:00", "1788619463 +0800"),
        ("2026-09-05T14:44:23Z", "1788619463 +0000"),
        ("2026-09-05T09:44:23-05:00", "1788619463 -0500"),
        ("2026-09-05T20:14:23+05:30", "1788619463 +0530"),
    ],
)
def test_git_date_writes_the_offset_the_way_git_does(iso, expected):
    """同一个时刻、四种偏移写法：epoch 一样，偏移必须原样带进对象。"""
    assert push_via_api.git_date(iso) == expected


def test_the_author_date_matches_what_git_writes(tmp_path):
    """让真 git 自己写一个 +05:30 的提交，对照我们的写法（不是对着注释抄）。"""
    repo = make_repo(tmp_path)
    tree = git("rev-parse", "HEAD^{tree}", cwd=repo).strip()
    env = dict(GIT_ENV, GIT_AUTHOR_DATE="2026-09-05T20:14:23+05:30",
               GIT_COMMITTER_DATE="2026-09-05T20:14:23+05:30")
    sha = subprocess.run(["git", "commit-tree", tree, "-m", "偏移"], cwd=repo, env=env,
                         capture_output=True).stdout.decode().strip()
    line = [line for line in git("cat-file", "commit", sha, cwd=repo).splitlines()
            if line.startswith("author ")][0]
    assert line == (
        "author 测试 <tester@example.com> "
        + push_via_api.git_date("2026-09-05T20:14:23+05:30")
    )


# --------------------------------------------------------------------------- 对象字节


def test_a_rebuilt_commit_object_is_byte_identical(tmp_path):
    """拿真提交复算：拼出来的字节与 git 的一模一样，sha 才可能对上。"""
    repo = make_repo(tmp_path)
    head = git("rev-parse", "HEAD", cwd=repo).strip()
    raw = git("cat-file", "commit", head, cwd=repo, text=False)
    commit = parse_commit(repo, head)
    built = push_via_api.git_commit_object(
        commit, tree=commit["tree"], parents=commit["parents"], message=commit["message"]
    )
    assert built == raw
    assert push_via_api._sha_of(built) == head


def test_a_message_without_a_trailing_newline_survives(tmp_path, monkeypatch):
    """本地历史里确实有说明不带尾换行的提交，取的时候一个字节都不能变。"""
    repo = make_repo(tmp_path)
    head = git("rev-parse", "HEAD", cwd=repo).strip()
    tree = git("rev-parse", "HEAD^{tree}", cwd=repo).strip()
    sha = handmade_commit(repo, tree, [head], "没有尾换行的说明")
    monkeypatch.chdir(repo)
    assert push_via_api.commit_message(sha) == "没有尾换行的说明"
    built = push_via_api.git_commit_object(
        HANDMADE, tree=tree, parents=[head], message="没有尾换行的说明"
    )
    assert push_via_api._sha_of(built) == sha


def test_percent_b_would_have_changed_the_object(tmp_path):
    """根因的机器证据：`%B` 会补一个换行，用它拼出来的对象 sha 与本地不同。"""
    repo = make_repo(tmp_path)
    head = git("rev-parse", "HEAD", cwd=repo).strip()
    tree = git("rev-parse", "HEAD^{tree}", cwd=repo).strip()
    sha = handmade_commit(repo, tree, [head], "没有尾换行的说明")
    raw = git("cat-file", "commit", sha, cwd=repo, text=False).partition(b"\n\n")[2]
    percent = subprocess.run(["git", "log", "-1", "--pretty=%B", sha], cwd=repo,
                             env=GIT_ENV, capture_output=True).stdout
    assert raw == "没有尾换行的说明".encode("utf-8")
    assert percent == raw + b"\n"
    built = push_via_api.git_commit_object(
        HANDMADE, tree=tree, parents=[head], message=percent.decode("utf-8")
    )
    assert push_via_api._sha_of(built) != sha


def test_the_parents_keep_their_order(tmp_path):
    """父提交的顺序也是对象的一部分，拼反了 sha 就变。"""
    repo = make_repo(tmp_path)
    tree = git("rev-parse", "HEAD^{tree}", cwd=repo).strip()
    first, second = "1" * 40, "2" * 40
    sha = handmade_commit(repo, tree, [first, second], "合并两条父\n")
    built = push_via_api.git_commit_object(
        HANDMADE, tree=tree, parents=[first, second], message="合并两条父\n"
    )
    assert push_via_api._sha_of(built) == sha
    swapped = push_via_api.git_commit_object(
        HANDMADE, tree=tree, parents=[second, first], message="合并两条父\n"
    )
    assert push_via_api._sha_of(swapped) != sha


def test_local_commits_reads_history_in_order(tmp_path, monkeypatch):
    """local_commits 要从旧到新、树与说明都原样取。"""
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    commits = push_via_api.local_commits()
    assert [commit["message"] for commit in commits] == ["第一条\n", "第二条\n"]
    assert [commit["sha"] for commit in commits] == git("rev-list", "--reverse", "HEAD", cwd=repo).split()
    assert commits[-1]["tree"] == git("rev-parse", "HEAD^{tree}", cwd=repo).strip()
    assert {item["path"] for item in commits[-1]["entries"]} == {"a.txt", "dir/b.txt", "z.txt"}
    assert commits[0]["parents"] == []
    assert commits[1]["parents"] == [commits[0]["sha"]]


# --------------------------------------------------------------------------- 已推过的判定


def test_the_longest_match_wins():
    """远端链里常有同名旧副本：要取「能连续对上最多条」的那个起点。"""
    local = [local_of(f"第 {i} 条\n") for i in range(3)]
    stale = {"message": "别人的提交\n",
             "author": {"email": "other@example.com", "date": "2020-01-01T00:00:00+08:00"}}
    remote = [remote_of(local[0]), stale] + [remote_of(commit) for commit in local]
    assert push_via_api.find_pushed_prefix(local, remote) == 3


def test_the_match_ignores_the_timezone_writing():
    """接口读回来的日期一律是 UTC 写法，判定要按同一时刻算。"""
    local = [local_of("同一条\n", date="2026-01-01T00:00:00+08:00")]
    remote = [remote_of(local[0], date="2025-12-31T16:00:00Z")]
    assert push_via_api.find_pushed_prefix(local, remote) == 1


def test_the_match_needs_the_same_author():
    """邮箱不同、或者时间差一秒，就不算同一条 —— 宁可重推，也不要误判成推过了。"""
    local = [local_of("同一条\n")]
    assert push_via_api.find_pushed_prefix(
        local, [remote_of(local[0], message="同一条\n") | {"author": {"email": "other@example.com",
                                                                     "date": local[0]["author_date"]}}]
    ) == 0
    assert push_via_api.find_pushed_prefix(
        local, [remote_of(local[0], date="2026-01-01T00:00:01+08:00")]
    ) == 0


def test_the_match_tolerates_a_trailing_newline_on_the_remote_side():
    """判定按 strip 后的文字比：这正是 `%B` 那种差一个换行不会导致重推的原因。"""
    local = [local_of("同一条\n")]
    assert push_via_api.find_pushed_prefix(local, [remote_of(local[0], message="同一条\n\n")]) == 1


def test_the_match_stops_at_the_first_difference():
    local = [local_of(f"第 {i} 条\n") for i in range(4)]
    remote = [remote_of(local[0]), remote_of(local[1]), remote_of(local[3])]
    assert push_via_api.find_pushed_prefix(local, remote) == 2


def test_no_match_when_either_side_is_empty():
    assert push_via_api.find_pushed_prefix([], [remote_of(local_of("x\n"))]) == 0
    assert push_via_api.find_pushed_prefix([local_of("x\n")], []) == 0


# --------------------------------------------------------------------------- 差异诊断


def test_diagnose_says_when_the_local_object_cannot_be_recomputed():
    """本地这份都复算不出来时必须直说：别再拿「时区」当挡箭牌。"""
    commit = local_of("本地说明\n", sha="f" * 40)
    reason = push_via_api.diagnose_commit_mismatch(
        commit, "t" * 40, {"message": "本地说明\n", "author": {}, "committer": {}, "parents": []}, "c" * 40
    )
    assert "本地提交对象就无法复算" in reason


def _full_commit(**overrides) -> dict:
    commit = {
        "sha": "",
        "tree": "t" * 40,
        "parents": [],
        "author_name": "测试",
        "author_email": "tester@example.com",
        "author_date_git": "1788619463 +0800",
        "committer_name": "测试",
        "committer_email": "tester@example.com",
        "committer_date_git": "1788619463 +0800",
        "message": "本地说明\n",
    }
    commit.update(overrides)
    built = push_via_api.git_commit_object(
        commit, tree=commit["tree"], parents=commit["parents"], message=commit["message"]
    )
    commit["sha"] = push_via_api._sha_of(built)
    return commit


def _remote_view(commit: dict, *, message: str | None = None,
                 date: str = "2026-09-05T14:44:23Z") -> dict:
    return {
        "message": commit["message"] if message is None else message,
        "author": {"name": commit["author_name"], "email": commit["author_email"], "date": date},
        "committer": {"name": commit["committer_name"], "email": commit["committer_email"],
                      "date": date},
        "parents": [],
    }


def test_diagnose_points_at_the_message_when_that_is_the_difference():
    commit = _full_commit()
    remote = _remote_view(commit, message="远端说明\n")
    created = push_via_api._sha_of(
        push_via_api.git_commit_object(
            {**commit, "author_date_git": push_via_api._api_date_to_git(remote["author"]["date"]),
             "committer_date_git": push_via_api._api_date_to_git(remote["committer"]["date"])},
            tree=commit["tree"], parents=[], message="远端说明\n",
        )
    )
    reason = push_via_api.diagnose_commit_mismatch(commit, commit["tree"], remote, created)
    assert "差异在：提交说明" in reason


def test_diagnose_points_at_the_metadata_when_the_message_is_the_same():
    """说明一样、sha 还是不一样 → 差在作者/提交时间/父提交（时区被改写成 UTC）。"""
    commit = _full_commit()
    remote = _remote_view(commit)
    created = push_via_api._sha_of(
        push_via_api.git_commit_object(
            {**commit, "author_date_git": push_via_api._api_date_to_git(remote["author"]["date"]),
             "committer_date_git": push_via_api._api_date_to_git(remote["committer"]["date"])},
            tree=commit["tree"], parents=[], message=commit["message"],
        )
    )
    assert created != commit["sha"]
    reason = push_via_api.diagnose_commit_mismatch(commit, commit["tree"], remote, created)
    assert "差异在：作者/提交时间/父提交" in reason


def test_diagnose_explains_the_lost_offset():
    """复算不出来时要说清是「接口把偏移改写成 UTC」，并指出只有 git push 能对齐。"""
    commit = _full_commit()
    reason = push_via_api.diagnose_commit_mismatch(
        commit, commit["tree"], _remote_view(commit), "0" * 40
    )
    assert "时区偏移" in reason and "git push" in reason


# --------------------------------------------------------------------------- 树与 blob


def test_the_tree_items_are_sorted_the_way_git_sorts_them(monkeypatch, tmp_path):
    """目录名视同带一个 "/"，`a.txt` 要排在 `a/` 前面 —— 让真 git 当尺子。"""
    payloads: list[dict] = []

    def fake(token, method, path, payload=None, retries=4):
        payloads.append(payload)
        return {"sha": "c" * 40}  # 目录对象的 sha 得是能当对象名的十六进制

    monkeypatch.setattr(push_via_api, "api", fake)
    entries = [entry("1" * 40, "a.txt"), entry("2" * 40, "ab.txt"), entry("3" * 40, "a/x.txt")]
    push_via_api.build_path_tree(entries, "owner/name", "t", {})
    child, root = payloads[0]["tree"], payloads[1]["tree"]
    assert [item["path"] for item in child] == ["x.txt"]

    repo = tmp_path / "sort"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    lines = b"".join(
        f'{("040000 tree" if item["type"] == "tree" else item["mode"] + " blob")} '
        f'{item["sha"]}\t{item["path"]}'.encode("utf-8") + b"\0"
        for item in root
    )
    sha = subprocess.run(["git", "mktree", "-z", "--missing"], cwd=repo, env=GIT_ENV,
                         input=lines, capture_output=True)
    assert sha.returncode == 0, sha.stderr.decode("utf-8", "replace")
    by_git = [line.split("\t", 1)[1]
              for line in git("ls-tree", sha.stdout.decode().strip(), cwd=repo).splitlines()]
    assert [item["path"] for item in root] == by_git == ["a.txt", "a", "ab.txt"]


def test_build_path_tree_reproduces_a_real_tree(monkeypatch):
    """拿本仓库 HEAD 的整棵树逐层重建，根 sha 必须与 git 算出来的一模一样。"""
    raw = subprocess.run(["git", "ls-tree", "-r", "-z", "HEAD"], cwd=ROOT,
                         capture_output=True).stdout
    entries = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        head, _, name = item.partition(b"\t")
        mode, otype, sha = head.decode("utf-8").split()
        entries.append({"mode": mode, "type": otype, "sha": sha, "path": name.decode("utf-8")})
    assert len(entries) > 100

    built: list[int] = []

    def fake(token, method, path, payload=None, retries=4):
        built.append(len(payload["tree"]))
        lines = b"".join(
            f'{("040000 tree" if item["type"] == "tree" else item["mode"] + " blob")} '
            f'{item["sha"]}\t{item["path"]}'.encode("utf-8") + b"\0"
            for item in payload["tree"]
        )
        result = subprocess.run(["git", "mktree", "-z"], cwd=ROOT, input=lines,
                                capture_output=True)
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        return {"sha": result.stdout.decode().strip()}

    monkeypatch.setattr(push_via_api, "api", fake)
    tree = push_via_api.build_path_tree(entries, "owner/name", "t", {})
    assert tree == git("rev-parse", "HEAD^{tree}", cwd=ROOT).strip()

    # 每个文件与每个目录都要在某一层里恰好出现一次（目录本身也是一条 entry）
    dirs = set()
    for item in entries:
        parts = item["path"].split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:depth]))
    assert sum(built) == len(entries) + len(dirs)


def test_upload_blobs_skips_known_and_excluded(monkeypatch):
    contents = {"keep.txt": b"keep\n", "skip.txt": b"skip\n", "known.txt": b"known\n"}
    entries = [entry(blob_sha(text), path) for path, text in contents.items()]
    posted: list[dict] = []

    def fake(token, method, path, payload=None, retries=4):
        posted.append(payload)
        return {"sha": blob_sha(base64.b64decode(payload["content"]))}

    monkeypatch.setattr(push_via_api, "api", fake)
    monkeypatch.setattr(
        push_via_api, "blob_content", lambda sha: {blob_sha(t): t for t in contents.values()}[sha]
    )
    uploaded = push_via_api.upload_blobs(
        [{"entries": entries}], "owner/name", "t", {blob_sha(contents["known.txt"])}, {"skip.txt"}
    )
    assert uploaded == 1
    assert [base64.b64decode(item["content"]) for item in posted] == [b"keep\n"]


def test_upload_blobs_refuses_a_wrong_sha(monkeypatch):
    """接口回来的 sha 与本地不同就报错：传错内容的后果比中断严重得多。"""
    monkeypatch.setattr(push_via_api, "blob_content", lambda sha: b"x")
    monkeypatch.setattr(push_via_api, "api", lambda *a, **k: {"sha": "9" * 40})
    with pytest.raises(push_via_api.ApiError, match="blob sha 不一致"):
        push_via_api.upload_blobs(
            [{"entries": [entry("1" * 40, "a.txt")]}], "owner/name", "t", set(), set()
        )


# --------------------------------------------------------------------------- 排除项


def test_unknown_excludes_finds_typos():
    commits = [{"entries": [entry("1" * 40, "a.txt"), entry("2" * 40, "dir/b.txt")]}]
    assert push_via_api.unknown_excludes(commits, {"a.txt", "dir/b.txt"}) == []
    assert push_via_api.unknown_excludes(commits, {"a.txt", "dir/b.txtx"}) == ["dir/b.txtx"]


def test_unknown_excludes_looks_at_the_whole_history():
    """只出现在更早提交里的路径是正常的，不该被当成写错。"""
    commits = [
        {"entries": [entry("1" * 40, "old.txt")]},
        {"entries": [entry("1" * 40, "new.txt")]},
    ]
    assert push_via_api.unknown_excludes(commits, {"old.txt", "new.txt"}) == []


# --------------------------------------------------------------------------- 跑一遍 main()


class _FakeApi:
    """记下每个请求的假 API；没准备的路径直接报错，避免用例悄悄放过。"""

    def __init__(self, *, head: str = "", remote_tree: dict | None = None):
        self.head = head
        self.remote_tree = {"tree": []} if remote_tree is None else remote_tree
        self.calls: list[tuple[str, str]] = []

    def __call__(self, token, method, path, payload=None, retries=4):
        self.calls.append((method, path))
        if method == "GET" and "/git/ref/heads/" in path:
            return {"object": {"sha": self.head}}
        if method == "GET" and "/git/trees/" in path:
            return self.remote_tree
        if method == "GET" and "/git/commits/" in path:
            return {"sha": path.rsplit("/", 1)[-1], "parents": [], "message": "",
                    "author": {}, "committer": {}}
        if method == "POST" and path.endswith("/git/blobs"):
            content = base64.b64decode(payload["content"])
            return {"sha": blob_sha(content)}
        if method == "POST" and path.endswith("/git/trees"):
            return {"sha": "t" * 40}
        if method == "POST" and path.endswith("/git/commits"):
            return {"sha": "c" * 40}
        if method in ("PATCH", "POST") and "/git/refs" in path:
            return {}
        raise AssertionError(f"用例没准备这个请求：{method} {path}")

    def ref_calls(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if "/git/refs" in call[1]]


def fake_commit(message: str, tree: str, *, sha: str = "a" * 40,
                path: str = "a.txt") -> dict:
    return {
        "sha": sha,
        "tree": tree,
        "parents": [],
        "author_name": "测试",
        "author_email": "tester@example.com",
        "author_date": "2026-01-01T00:00:00+08:00",
        "committer_name": "测试",
        "committer_email": "tester@example.com",
        "committer_date": "2026-01-01T00:00:00+08:00",
        "author_date_git": "1767196800 +0800",
        "committer_date_git": "1767196800 +0800",
        "message": message,
        "entries": [entry(blob_sha(CONTENT), path)],
    }


def run_main(monkeypatch, capsys, *, api: _FakeApi, commits: list[dict], args: list[str]):
    monkeypatch.setattr(push_via_api, "gh_token", lambda: "t")
    monkeypatch.setattr(push_via_api, "local_commits", lambda: commits)
    monkeypatch.setattr(push_via_api, "blob_content", lambda sha: CONTENT)
    monkeypatch.setattr(push_via_api, "api", api)
    code = push_via_api.main(args)
    return code, capsys.readouterr().out


def test_a_dry_run_creates_no_commits_and_moves_no_ref(monkeypatch, capsys, back_to_root):
    api = _FakeApi(head="d" * 40)
    commits = [fake_commit("第一条\n", "t" * 40, sha="a" * 40),
               fake_commit("第二条\n", "t" * 40, sha="b" * 40, path="b.txt")]
    code, out = run_main(
        monkeypatch, capsys, api=api, commits=commits,
        args=["--repo", "owner/name", "--branch", "master", "--dry-run"],
    )
    assert code == 0
    assert not [path for _, path in api.calls if path.endswith("/git/commits")]
    assert api.ref_calls() == []
    assert "演练" in out
    # 演练会为了核对树而建 blob/tree 对象，说明里不能再写「未改动远端」。
    assert "没有创建提交" in out and "未引用的" in out


def test_a_typo_in_exclude_stops_before_any_request(monkeypatch, capsys, back_to_root):
    api = _FakeApi(head="")
    commits = [fake_commit("第一条\n", "t" * 40)]
    code, out = run_main(
        monkeypatch, capsys, api=api, commits=commits,
        args=["--repo", "owner/name", "--branch", "master", "--exclude", "dir/b.txtx"],
    )
    assert code == 1
    assert api.calls == []
    assert "根本不存在" in out and "dir/b.txtx" in out


def test_an_up_to_date_branch_needs_no_push(monkeypatch, capsys, back_to_root):
    head = "e" * 40
    api = _FakeApi(head=head)
    commits = [fake_commit("第一条\n", "t" * 40, sha=head)]
    code, out = run_main(
        monkeypatch, capsys, api=api, commits=commits,
        args=["--repo", "owner/name", "--branch", "master"],
    )
    assert code == 0
    assert "没有需要推送的新提交" in out
    # 只读两次：先看分支指向，再数一遍远端已有的 blob（既没建 blob 也没动引用）
    assert api.calls == [
        ("GET", "/repos/owner/name/git/ref/heads/master"),
        ("GET", f"/repos/owner/name/git/trees/{head}?recursive=1"),
    ]


# --------------------------------------------------------------------------- API 重试


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _http_error(code: int, detail: bytes = b'{"message":"boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com/x", code, "boom", {}, io.BytesIO(detail))


def test_the_api_retries_a_server_error(monkeypatch, capsys):
    answers = [_http_error(502), _Response(b'{"ok": true}')]

    def fake_urlopen(request, timeout=None):
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(push_via_api.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(push_via_api.time, "sleep", lambda seconds: None)
    assert push_via_api.api("t", "GET", "/x") == {"ok": True}
    assert "重试" in capsys.readouterr().out


def test_the_api_does_not_retry_a_404(monkeypatch):
    attempts = []

    def fake_urlopen(request, timeout=None):
        attempts.append(request)
        raise _http_error(404, b'{"message":"Not Found"}')

    monkeypatch.setattr(push_via_api.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(push_via_api.time, "sleep", lambda seconds: None)
    with pytest.raises(push_via_api.ApiError, match="404"):
        push_via_api.api("t", "GET", "/x")
    assert len(attempts) == 1


def test_the_api_gives_up_after_the_last_retry(monkeypatch):
    attempts = []

    def fake_urlopen(request, timeout=None):
        attempts.append(request)
        raise _http_error(503)

    monkeypatch.setattr(push_via_api.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(push_via_api.time, "sleep", lambda seconds: None)
    with pytest.raises(push_via_api.ApiError, match="连续 3 次失败"):
        push_via_api.api("t", "GET", "/x", retries=2)
    assert len(attempts) == 3
