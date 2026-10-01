"""`tools/sync_from_api.py` 的离线用例：拿真 git 与假 API 一起跑重建。

这个脚本的用途是「本机只能走 API 时，把远端历史在本地逐字节重建，再把分支指过去」，
所以它最容易出的毛病是**看着同步完了、其实没对齐**：

* 提交链只跟着第一个父提交往回走 —— 远端有过合并提交时，另一条支线的提交不会被重建，
  引用照样指过去，本地仓库却缺对象，而且 `git status` 看不出来；
* 超过 1 MB 的 blob，JSON 接口只给一个空壳（`encoding` 不是 base64），
  旧写法会 `b64decode("")` 得到空内容，最后报一句莫名其妙的「blob 不一致」；
* 引用指过去了、工作区却没对齐，旧写法只说一句「可执行 git status 确认」就收工。

这里用真 git 造历史（含一条合并提交）、用假 API 喂回 GitHub 形状的数据，
把上面的护栏逐条钉住。跑 `main()` 时用 `XDAO_REPO_DIR` 把「仓库根」指到临时仓库，
免得动到真仓库。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import sync_from_api  # noqa: E402

REPO = "owner/name"

GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_SYSTEM=os.devnull,
    GIT_AUTHOR_NAME="测试",
    GIT_AUTHOR_EMAIL="tester@example.com",
    GIT_COMMITTER_NAME="测试",
    GIT_COMMITTER_EMAIL="tester@example.com",
)


def git(*args: str, cwd: Path, data: bytes | None = None, env: dict | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env or GIT_ENV, input=data, capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


def sha_of(repo: Path, ref: str = "HEAD") -> str:
    return git("rev-parse", ref, cwd=repo).decode().strip()


def commit(repo: Path, message: str, when: str, *, extra: dict | None = None) -> str:
    env = dict(GIT_ENV, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    if extra:
        env.update(extra)
    git("commit", "-q", "-m", message, cwd=repo, env=env)
    return sha_of(repo)


def make_repo(tmp_path: Path, *, offset: str = "+0800") -> Path:
    """两条提交的临时仓库：a.txt，再加 dir/b.txt。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "master", cwd=repo)
    (repo / "a.txt").write_text("第一份\n", encoding="utf-8")
    git("add", "a.txt", cwd=repo)
    commit(repo, "第一条", f"2026-09-05T20:14:23{offset}")
    (repo / "dir").mkdir()
    (repo / "dir" / "b.txt").write_text("第二份\n", encoding="utf-8")
    git("add", "dir/b.txt", cwd=repo)
    commit(repo, "第二条", f"2026-09-05T21:14:23{offset}")
    return repo


def _utc(value: str) -> str:
    """GitHub 的提交接口给的是 UTC + Z 写法。

    注意：结尾这个 `Z` 只有 Python 3.11 起的 `fromisoformat` 认得，3.10 会直接抛
    ValueError —— CI 里就有 py3.10 的 job（本地 3.12 跑绿不代表 CI 绿），所以这里
    先自己把 `Z` 换成 `+00:00`。
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def api_commit(repo: Path, sha: str) -> dict:
    """把本地真实提交整成 GitHub 提交接口的形状。"""
    raw = git("cat-file", "commit", sha, cwd=repo)
    _, _, body = raw.partition(b"\n\n")
    fields = git(
        "show", "-s", "--format=%T%n%P%n%an%n%ae%n%aI%n%cn%n%ce%n%cI", sha, cwd=repo
    ).decode("utf-8").split("\n")
    tree, parents, an, ae, ad, cn, ce, cd = fields[:8]
    return {
        "sha": sha,
        "message": body.decode("utf-8"),
        "tree": {"sha": tree},
        "parents": [{"sha": item} for item in parents.split()],
        "author": {"name": an, "email": ae, "date": _utc(ad)},
        "committer": {"name": cn, "email": ce, "date": _utc(cd)},
    }


def api_tree(repo: Path, sha: str) -> dict:
    """非递归的树：条目字段是 `path`（就是名字），与 build_tree 读的接口一致。"""
    items = []
    for line in git("ls-tree", sha, cwd=repo).decode("utf-8").splitlines():
        head, _, name = line.partition("\t")
        mode, otype, item_sha = head.split()
        items.append({"mode": mode, "type": otype, "sha": item_sha, "path": name})
    return {"sha": sha, "tree": items}


def api_blob(repo: Path, sha: str) -> dict:
    content = git("cat-file", "blob", sha, cwd=repo)
    return {
        "sha": sha,
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode(),
    }


class _Api:
    """假的 GitHub API：默认拿真 git 里的对象作答，可以按 sha 顶掉某几条。"""

    def __init__(self, repo: Path, *, head: str, blobs: dict | None = None,
                 trees: dict | None = None):
        self.repo = repo
        self.head = head
        self.blobs = blobs or {}
        self.trees = trees or {}
        self.calls: list[str] = []

    def __call__(self, token: str, path: str, retries: int = 5) -> dict:
        self.calls.append(path)
        prefix = f"/repos/{REPO}/git/"
        assert path.startswith(prefix), f"用例没准备这个请求：{path}"
        kind, _, sha = path[len(prefix):].partition("/")
        if kind == "ref":
            return {"object": {"sha": self.head}}
        if kind == "commits":
            return api_commit(self.repo, sha)
        if kind == "trees":
            return self.trees.get(sha) or api_tree(self.repo, sha)
        if kind == "blobs":
            return self.blobs.get(sha) or api_blob(self.repo, sha)
        raise AssertionError(f"用例没准备这个请求：{path}")

    def asked(self, kind: str) -> list[str]:
        return [path.rsplit("/", 1)[1] for path in self.calls if f"/{kind}/" in path]


@pytest.fixture
def back_to_root():
    """`main()` 会 chdir，用例结束后切回来。"""
    before = Path.cwd()
    yield
    os.chdir(before)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """没顶掉 `api` / `gh_token` 就直接失败 —— 免得用例真的打到 GitHub 上去。"""

    def refuse(*args, **kwargs):
        raise AssertionError("用例没有顶掉 sync_from_api.api，这一下会打到真网络")

    monkeypatch.setattr(sync_from_api, "api", refuse)
    monkeypatch.setattr(sync_from_api, "gh_token", refuse)


def use_api(monkeypatch, api: "_Api") -> "_Api":
    monkeypatch.setattr(sync_from_api, "api", api)
    return api


def run_main(monkeypatch, capsys, repo: Path, api: _Api, args: list[str] | None = None):
    monkeypatch.setenv("XDAO_REPO_DIR", str(repo))
    monkeypatch.setattr(sync_from_api, "gh_token", lambda: "t")
    monkeypatch.setattr(sync_from_api, "api", api)
    code = sync_from_api.main(args or ["--repo", REPO, "--branch", "master"])
    return code, capsys.readouterr().out


# --------------------------------------------------------------------------- blob 与树


def test_write_blob_matches_git_hash_object(tmp_path):
    repo = make_repo(tmp_path)
    sha = sync_from_api.write_blob("内容".encode("utf-8"))
    assert sha == git("hash-object", "--stdin", cwd=repo,
                     data="内容".encode("utf-8")).decode().strip()


def test_write_tree_rebuilds_a_real_tree(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)  # mktree 要能在仓库里找到那些 blob 对象
    items = [{"mode": item["mode"], "type": item["type"], "sha": item["sha"],
              "name": item["path"]}
             for item in api_tree(repo, sha_of(repo, "HEAD^{tree}"))["tree"]]
    assert sync_from_api.write_tree(items) == sha_of(repo, "HEAD^{tree}")


def test_build_tree_reproduces_a_real_tree(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    api = use_api(monkeypatch, _Api(repo, head=sha_of(repo)))
    built = sync_from_api.build_tree("t", REPO, sha_of(repo, "HEAD^{tree}"))
    assert built == sha_of(repo, "HEAD^{tree}")
    # 叶子是文件的树要逐个 blob 取回来核对，不能只看顶层 sha
    assert len(api.asked("blobs")) == 2


def test_a_truncated_tree_is_refused(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    tree_sha = sha_of(repo, "HEAD^{tree}")
    use_api(monkeypatch, _Api(repo, head=sha_of(repo),
                              trees={tree_sha: {"sha": tree_sha, "truncated": True, "tree": []}}))
    with pytest.raises(sync_from_api.ApiError) as excinfo:
        sync_from_api.build_tree("t", REPO, tree_sha)
    assert "树被截断" in str(excinfo.value)


def test_a_blob_without_content_is_refused_clearly(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    blob = git("rev-parse", "HEAD:a.txt", cwd=repo).decode().strip()
    use_api(monkeypatch, _Api(repo, head=sha_of(repo), blobs={
        blob: {"sha": blob, "size": 2_000_000, "encoding": "none", "content": ""},
    }))
    with pytest.raises(sync_from_api.ApiError) as excinfo:
        sync_from_api.build_tree("t", REPO, sha_of(repo, "HEAD^{tree}"))
    message = str(excinfo.value)
    assert "取不到 blob 内容" in message and "raw" in message and "1 MB" in message


def test_a_blob_that_rebuilds_to_another_sha_is_refused(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    blob = git("rev-parse", "HEAD:a.txt", cwd=repo).decode().strip()
    use_api(monkeypatch, _Api(repo, head=sha_of(repo), blobs={
        blob: {"sha": blob, "size": 3, "encoding": "base64",
               "content": base64.b64encode(b"xxx").decode()},
    }))
    with pytest.raises(sync_from_api.ApiError) as excinfo:
        sync_from_api.build_tree("t", REPO, sha_of(repo, "HEAD^{tree}"))
    assert "blob 不一致" in str(excinfo.value)


def test_an_empty_file_is_not_mistaken_for_a_missing_blob(monkeypatch, tmp_path):
    """空文件的 content 就是空串（encoding 仍是 base64），不能当成「接口没给内容」。"""
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / "empty.txt").write_text("", encoding="utf-8")
    git("add", "empty.txt", cwd=repo)
    commit(repo, "第三条", "2026-09-05T22:14:23+0800")
    api = use_api(monkeypatch, _Api(repo, head=sha_of(repo)))
    built = sync_from_api.build_tree("t", REPO, sha_of(repo, "HEAD^{tree}"))
    assert built == sha_of(repo, "HEAD^{tree}")
    assert len(api.asked("blobs")) == 3


# --------------------------------------------------------------------------- 提交对象


def test_git_ident_writes_the_epoch_and_the_offset():
    assert sync_from_api.git_ident(
        "2026-09-05T22:44:23+08:00", "T", "t@example.com", "+0800"
    ) == "T <t@example.com> 1788619463 +0800"


def test_write_commit_reproduces_a_real_commit(tmp_path):
    repo = make_repo(tmp_path)
    head = sha_of(repo)
    created = sync_from_api.write_commit(api_commit(repo, head), sha_of(repo, "HEAD^{tree}"))
    assert created == head


def test_write_commit_reproduces_a_utc_commit(tmp_path):
    repo = make_repo(tmp_path, offset="+0000")
    head = sha_of(repo)
    assert sync_from_api.write_commit(api_commit(repo, head),
                                      sha_of(repo, "HEAD^{tree}")) == head


def test_write_commit_accepts_a_message_without_a_trailing_newline(tmp_path):
    """本地历史里确实有没有结尾换行的提交，重建时不许给它补一个。"""
    repo = make_repo(tmp_path)
    tree = sha_of(repo, "HEAD^{tree}")
    parent = sha_of(repo)
    body = (
        f"tree {tree}\nparent {parent}\n"
        f"author 手工 <hand@example.com> 1788619463 +0800\n"
        f"committer 手工 <hand@example.com> 1788619463 +0800\n\n"
        "没有结尾换行的说明"
    ).encode("utf-8")
    handmade = git("hash-object", "-w", "-t", "commit", "--stdin", cwd=repo,
                   data=body).decode().strip()
    assert sync_from_api.write_commit(api_commit(repo, handmade), tree) == handmade


def test_write_commit_raises_when_the_message_was_changed(tmp_path):
    repo = make_repo(tmp_path)
    remote = api_commit(repo, sha_of(repo))
    remote["message"] = remote["message"] + "（被人改过）"
    with pytest.raises(sync_from_api.ApiError) as excinfo:
        sync_from_api.write_commit(remote, sha_of(repo, "HEAD^{tree}"))
    assert "提交不一致" in str(excinfo.value)


def test_write_commit_raises_for_an_offset_it_does_not_try(tmp_path):
    """只试 +0800 / +0000 两种偏移：别的偏移复现不出来时要**报错**，不能悄悄换一个。"""
    repo = make_repo(tmp_path, offset="+0530")
    with pytest.raises(sync_from_api.ApiError) as excinfo:
        sync_from_api.write_commit(api_commit(repo, sha_of(repo)),
                                   sha_of(repo, "HEAD^{tree}"))
    assert "提交不一致" in str(excinfo.value)


# --------------------------------------------------------------------------- 提交链


def test_commit_chain_returns_parents_before_children(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    use_api(monkeypatch, _Api(repo, head=sha_of(repo)))
    chain = sync_from_api.commit_chain("t", REPO, sha_of(repo))
    shas = [item["sha"] for item in chain]
    assert shas == [sha_of(repo, "HEAD~1"), sha_of(repo)]
    assert len(shas) == 2


def test_commit_chain_handles_a_single_commit(monkeypatch, tmp_path):
    repo = tmp_path / "one"
    repo.mkdir()
    git("init", "-q", "-b", "master", cwd=repo)
    (repo / "a.txt").write_text("就一份\n", encoding="utf-8")
    git("add", "a.txt", cwd=repo)
    commit(repo, "唯一一条", "2026-09-05T20:14:23+0800")
    use_api(monkeypatch, _Api(repo, head=sha_of(repo)))
    chain = sync_from_api.commit_chain("t", REPO, sha_of(repo))
    assert [item["sha"] for item in chain] == [sha_of(repo)]


def test_commit_chain_follows_every_parent_of_a_merge(monkeypatch, tmp_path):
    """合并提交的另一条支线也得重建 —— 旧写法只跟第一个父提交，会漏掉整条支线。"""
    repo = make_repo(tmp_path)
    git("checkout", "-q", "-b", "side", cwd=repo)
    (repo / "side.txt").write_text("支线\n", encoding="utf-8")
    git("add", "side.txt", cwd=repo)
    side = commit(repo, "支线上的提交", "2026-09-05T22:14:23+0800")

    git("checkout", "-q", "master", cwd=repo)
    (repo / "main.txt").write_text("主线\n", encoding="utf-8")
    git("add", "main.txt", cwd=repo)
    main = commit(repo, "主线上的提交", "2026-09-05T23:14:23+0800")

    git("merge", "-q", "--no-ff", "-m", "合并", "side", cwd=repo,
        env=dict(GIT_ENV, GIT_AUTHOR_DATE="2026-09-06T00:14:23+0800",
                 GIT_COMMITTER_DATE="2026-09-06T00:14:23+0800"))
    head = sha_of(repo)
    assert len(git("show", "-s", "--format=%P", head, cwd=repo).decode().split()) == 2

    api = use_api(monkeypatch, _Api(repo, head=head))
    chain = sync_from_api.commit_chain("t", REPO, head)
    shas = [item["sha"] for item in chain]
    assert shas[-1] == head
    assert side in shas and main in shas
    for item in chain:  # 每个父提交都必须排在它前面
        for parent in item["parents"]:
            assert shas.index(parent["sha"]) < shas.index(item["sha"])
    # 每条提交只读一次
    assert len(api.asked("commits")) == len(set(api.asked("commits"))) == len(shas)


# --------------------------------------------------------------------------- 工作区核对


def test_worktree_changes_lists_modified_files(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    assert sync_from_api.worktree_changes() == []
    (repo / "a.txt").write_text("改过了\n", encoding="utf-8")
    assert sync_from_api.worktree_changes() == ["a.txt"]


def test_worktree_changes_ignores_untracked_files(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / "新文件.txt").write_text("没人跟踪\n", encoding="utf-8")
    assert sync_from_api.worktree_changes() == []


# --------------------------------------------------------------------------- main


def _staged_repo(tmp_path: Path) -> tuple[Path, str]:
    """造一个「远端比本地分支快一条」的现场，返回 (仓库, 远端 sha)。

    工作区与索引都停在第二条提交上，只是把本地分支指回第一条上，
    所以重建并指过去之后工作区应当是干净的。
    """
    repo = make_repo(tmp_path)
    remote = sha_of(repo)
    git("update-ref", "refs/heads/master", sha_of(repo, "HEAD~1"), cwd=repo)
    return repo, remote


def test_main_dry_run_does_not_move_the_branch(monkeypatch, capsys, tmp_path, back_to_root):
    repo, remote = _staged_repo(tmp_path)
    before = sha_of(repo, "refs/heads/master")
    api = _Api(repo, head=remote)
    code, out = run_main(monkeypatch, capsys, repo, api,
                         ["--repo", REPO, "--branch", "master", "--dry-run"])
    assert code == 0
    assert sha_of(repo, "refs/heads/master") == before
    assert "一个都没动" in out and "未引用" in out


def test_main_moves_the_branch_and_confirms_the_worktree(monkeypatch, capsys, tmp_path,
                                                         back_to_root):
    repo, remote = _staged_repo(tmp_path)
    api = _Api(repo, head=remote)
    code, out = run_main(monkeypatch, capsys, repo, api)
    assert code == 0
    assert sha_of(repo, "refs/heads/master") == remote
    assert "工作区里 2 个已跟踪文件也逐一对得上" in out


def test_main_reports_a_worktree_that_does_not_match(monkeypatch, capsys, tmp_path,
                                                     back_to_root):
    repo, remote = _staged_repo(tmp_path)
    (repo / "a.txt").write_text("同步之前被人改过\n", encoding="utf-8")
    api = _Api(repo, head=remote)
    code, out = run_main(monkeypatch, capsys, repo, api)
    assert code == 1
    assert sha_of(repo, "refs/heads/master") == remote  # 引用还是指过去了，所以要报出来
    assert "与新的 HEAD 不一样" in out and "a.txt" in out


def test_main_ignores_untracked_files_but_still_finishes(monkeypatch, capsys, tmp_path,
                                                         back_to_root):
    repo, remote = _staged_repo(tmp_path)
    (repo / "临时.txt").write_text("没人跟踪\n", encoding="utf-8")
    api = _Api(repo, head=remote)
    code, out = run_main(monkeypatch, capsys, repo, api)
    assert code == 0
    assert "完成" in out


def test_main_does_not_move_the_branch_when_a_blob_is_wrong(monkeypatch, capsys, tmp_path,
                                                            back_to_root):
    repo, remote = _staged_repo(tmp_path)
    before = sha_of(repo, "refs/heads/master")
    # 引用已经被挪回 HEAD~1，dir/b.txt 只在远端那条提交里，所以按 remote 取
    blob = git("rev-parse", f"{remote}:dir/b.txt", cwd=repo).decode().strip()
    api = _Api(repo, head=remote, blobs={
        blob: {"sha": blob, "size": 3, "encoding": "base64",
               "content": base64.b64encode(b"xxx").decode()},
    })
    with pytest.raises(sync_from_api.ApiError) as excinfo:
        run_main(monkeypatch, capsys, repo, api)
    assert "blob 不一致" in str(excinfo.value)
    assert sha_of(repo, "refs/heads/master") == before
