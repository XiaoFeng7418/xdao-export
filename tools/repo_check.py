"""仓库体检：一条命令看清 GitHub 仓库与本地仓库是否健康、是否同步。

维护这个仓库时先跑它，比逐个翻网页快得多。检查项：

1. 本地与远端的提交是否对应（本地有没有还没推的提交）；
2. 本地 HEAD 的树与远端分支的树是否逐文件一致；
3. 版本号是否处处一致（``xdao/__init__.py`` ↔ 最新 Release 标签 ↔ 附件名）；
4. 最新 Release 是否具备预期的附件，说明里有没有提到免安装包；
5. 有没有积压的 issue / PR；
6. 仓库基础设置（描述、话题、许可、默认分支）是否齐全。

用法：
    python tools/repo_check.py --repo XiaoFeng7418/xdao-export
    python tools/repo_check.py --repo ... --json     # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_release import ApiError, gh_token, request_json  # noqa: E402
from push_via_api import find_pushed_prefix, local_commits, remote_chain  # noqa: E402
from repo_info import expected_description, expected_topics  # noqa: E402

OK = "✓"
WARN = "!"
BAD = "✗"

# `push_via_api.remote_chain()` 一次只往回取这么多条远端提交（每条一个 API 请求）。
# 本地历史比它长时，远端链的窗口装不下最早的提交，靠比对链就判断不了同步状态。
REMOTE_CHAIN_LIMIT = 100


class Report:
    def __init__(self) -> None:
        self.findings: list[dict] = []

    def add(self, level: str, area: str, message: str) -> None:
        self.findings.append({"level": level, "area": area, "message": message})

    def show(self) -> None:
        for level, area, message in (
            (f["level"], f["area"], f["message"]) for f in self.findings
        ):
            print(f"  {level} [{area}] {message}")

    @property
    def problems(self) -> list[dict]:
        return [f for f in self.findings if f["level"] in (WARN, BAD)]


def local_tree(repo_dir: Path) -> dict[str, str]:
    """本地 HEAD 的 (路径 -> blob sha)。"""
    out = subprocess.run(
        ["git", "ls-tree", "-r", "HEAD"],
        cwd=repo_dir, capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        raise ApiError(f"读取本地树失败：{out.stderr.strip()}")
    result = {}
    for line in out.stdout.strip().splitlines():
        head, _, path = line.partition("\t")
        result[path] = head.split()[2]
    return result


def git_subjects(repo_dir: Path, *args: str) -> list[str] | None:
    """跑一条 git 命令，按行返回；失败时返回 None（例如本地没有那个对象）。"""
    out = subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.strip().splitlines() if line.strip()]


def remote_tree(repo: str, token: str, ref: str) -> dict[str, str]:
    data = request_json("GET", f"/repos/{repo}/git/trees/{ref}?recursive=1", token)
    if data.get("truncated"):
        raise ApiError("远端树被截断，无法完整比对")
    return {i["path"]: i["sha"] for i in data["tree"] if i["type"] == "blob"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="GitHub 仓库体检")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    repo_dir = Path(__file__).resolve().parent.parent
    token = gh_token()
    report = Report()

    # ---------- 1. 仓库基础信息 ----------
    info = request_json("GET", f"/repos/{args.repo}", token)
    branch = info["default_branch"]
    print(f"仓库 {info['full_name']}（{info['visibility']}，默认分支 {branch}）")
    if info.get("description"):
        report.add(OK, "设置", f"描述：{info['description'][:40]}…")
    else:
        report.add(WARN, "设置", "仓库没有描述")
    if (info.get("description") or "").strip() != expected_description():
        report.add(
            WARN, "设置",
            "描述与代码里的导出格式不一致："
            f"跑 python tools/repo_info.py --repo {args.repo} --apply",
        )
    else:
        report.add(OK, "设置", "描述与代码里的导出格式一致")
    topics = info.get("topics") or []
    missing_topics = [t for t in expected_topics() if t not in topics]
    topic_line = f"话题 {len(topics)} 个" + (f"：{', '.join(topics[:6])}" if topics else "（未设置）")
    if missing_topics:
        topic_line += f"（缺少 {', '.join(missing_topics)}，用 tools/repo_info.py --apply 补）"
    report.add(OK if topics and not missing_topics else WARN, "设置", topic_line)
    license_id = (info.get("license") or {}).get("spdx_id")
    report.add(OK if license_id else WARN, "设置", f"许可：{license_id or '未识别'}")

    # ---------- 2. 提交同步状态 ----------
    #
    # 别只靠 API 走远端历史：remote_chain 一次只取最近 100 条，本地提交数一超过它，
    # 「最早的本地提交在不在远端链里」就永远找不到 → find_pushed_prefix 返回 0，
    # 于是整份历史被报成「都没推送」（2026-10-01 本地到第 102 个提交时真的发生了）。
    # 先比 HEAD，再用本地的 git 判断祖先关系；只有本地没有远端那个对象时才回退到 API。
    ref = request_json("GET", f"/repos/{args.repo}/git/ref/heads/{branch}", token)
    remote_head = ref["object"]["sha"]
    commits = local_commits()
    head = commits[-1]["sha"] if commits else ""
    report.add(OK, "提交", f"远端 {branch} = {remote_head[:8]}，本地 HEAD = {head[:8]}（共 {len(commits)} 个提交）")
    if head and remote_head == head:
        report.add(OK, "提交", "本地 HEAD 就是远端分支的顶端，没有未推送的提交")
    else:
        subjects = git_subjects(repo_dir, "log", "--pretty=%s", "--reverse", f"{remote_head}..HEAD")
        if subjects is not None:
            if subjects:
                report.add(
                    WARN, "提交",
                    f"本地有 {len(subjects)} 个提交未推送到远端："
                    + "、".join(s[:30] for s in subjects[:20]),
                )
            else:
                report.add(OK, "提交", "没有未推送的提交")
        else:
            chain = remote_chain(args.repo, token, remote_head)
            pushed = find_pushed_prefix(commits, chain)
            pending = len(commits) - pushed
            if pending and pushed == 0 and len(chain) >= REMOTE_CHAIN_LIMIT:
                report.add(
                    WARN, "提交",
                    f"远端历史只取到最近 {REMOTE_CHAIN_LIMIT} 条（本地 {len(commits)} 个提交），"
                    "判断不了哪些已经推过；这一项没查成，多半是历史已经比窗口长",
                )
            elif pending:
                report.add(
                    WARN, "提交",
                    f"本地有 {pending} 个提交未推送到远端："
                    + "、".join(c["message"].splitlines()[0][:30] for c in commits[pushed:]),
                )
            else:
                report.add(OK, "提交", "本地所有提交都已在远端")

    # ---------- 3. 文件内容一致性 ----------
    mine = local_tree(repo_dir)
    theirs = remote_tree(args.repo, token, remote_head)
    only_local = sorted(set(mine) - set(theirs))
    only_remote = sorted(set(theirs) - set(mine))
    changed = sorted(k for k in set(mine) & set(theirs) if mine[k] != theirs[k])
    if not (only_local or only_remote or changed):
        report.add(OK, "文件", f"本地与远端逐文件一致（{len(mine)} 个文件）")
    else:
        if only_local:
            report.add(WARN, "文件", f"仅本地有：{', '.join(only_local[:5])}")
        if only_remote:
            report.add(WARN, "文件", f"仅远端有：{', '.join(only_remote[:5])}")
        if changed:
            report.add(WARN, "文件", f"内容不同：{', '.join(changed[:5])}")

    # ---------- 4. 版本号一致性 ----------
    version = ""
    try:
        sys.path.insert(0, str(repo_dir))
        from xdao import __version__ as version  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        report.add(BAD, "版本", f"读不到 xdao.__version__：{exc}")
    releases = request_json("GET", f"/repos/{args.repo}/releases", token)
    latest = releases[0] if releases else None
    if latest:
        tag = latest["tag_name"].lstrip("v")
        if version and tag != version:
            report.add(WARN, "版本", f"代码里是 {version}，最新 Release 是 {latest['tag_name']}")
        else:
            report.add(OK, "版本", f"{version} 与最新 Release {latest['tag_name']} 一致")

        # ---------- 5. 附件检查 ----------
        # v0.5.1 起只发免安装包：单文件版的启动器在中文路径下会直接打不开
        # （Could not create temporary directory!），且这取决于用户把文件放哪儿。
        names = [a["name"] for a in latest.get("assets", [])]
        has_zip = any(n.endswith(".zip") for n in names)
        has_exe = any(n.endswith(".exe") for n in names)
        if has_zip:
            report.add(OK, "发布", "提供免安装包（zip）")
        else:
            report.add(BAD, "发布", "最新 Release 没有免安装包，受限环境下用户会打不开")
        if has_exe:
            report.add(WARN, "发布", "还带着单文件版（exe）——它在中文路径下打不开，建议撤掉")
        if "免安装包" in (latest.get("body") or ""):
            report.add(OK, "发布", "发布说明里解释了下哪个附件")
        else:
            report.add(WARN, "发布", "发布说明没有说明该下哪个附件")
        for name in names:
            if any(ord(ch) > 127 for ch in name):
                report.add(BAD, "发布", f"附件名含非 ASCII 字符（会被 GitHub 截断）：{name}")
        report.add(OK, "发布", f"最新 Release {latest['tag_name']} 附件：" + "、".join(names))
    else:
        report.add(WARN, "发布", "仓库还没有 Release")

    # ---------- 6. 待办事项 ----------
    issues = request_json("GET", f"/repos/{args.repo}/issues?state=open", token)
    prs = [i for i in issues if "pull_request" in i]
    plain = [i for i in issues if "pull_request" not in i]
    report.add(
        OK if not plain else WARN, "待办",
        f"开放 issue {len(plain)} 条" + (f"：{', '.join('#' + str(i['number']) for i in plain[:5])}" if plain else ""),
    )
    report.add(
        OK if not prs else WARN, "待办",
        f"开放 PR {len(prs)} 条" + (f"：{', '.join('#' + str(i['number']) for i in prs[:5])}" if prs else ""),
    )

    print("\n检查结果：")
    report.show()
    problems = report.problems
    print(
        f"\n结论：{'一切正常' if not problems else f'{len(problems)} 项需要处理'}"
        f"（共 {len(report.findings)} 项检查）"
    )

    if args.json:
        print("\n" + json.dumps(report.findings, ensure_ascii=False, indent=2))
    return 1 if any(f["level"] == BAD for f in report.findings) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
