"""维护仓库门面信息：描述（description）与话题（topics）。

为什么要有这个脚本：仓库描述里写着「支持哪些导出格式」，而格式是会变的
（v0.3.0 加了 PDF，描述却一直停在只有 HTML / TXT / Markdown / EPUB 的版本）。
靠人记着去改迟早会忘，所以把「描述该写什么」交给代码推导，改完一键同步。

描述与话题都从代码里的格式注册表推导：
    xdao.exporters.EXPORTERS  →  描述里列出全部格式名，话题里自动补上对应标签

这个脚本会往**公开仓库**写东西，所以两道护栏都在：
1. 写之前先验推导出来的值（空的、超长的、GitHub 不认的话题一律拦下，一个请求都不发）；
2. 写完**再读回来核对**，只有读回来确实一致了才说「已同步」—— 只说「发过请求了」
   等于把「远端没照办」也报成成功（GitHub 会规范化话题、截断描述）。

用法：
    python tools/repo_info.py --repo XiaoFeng7418/xdao-export            # 只检查
    python tools/repo_info.py --repo XiaoFeng7418/xdao-export --apply    # 检查并同步

退出码：0 = 已经一致，或者同步完读回来核对通过；1 = 发现不一致（没加 --apply）、
推导出来的值不合格、或者发过请求但读回来还是不一样。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from make_release import gh_token, request_json  # noqa: E402

from xdao.exporters import EXPORTERS  # noqa: E402

# 描述的固定部分（模板），``{formats}`` 由格式注册表填。
DESCRIPTION_TEMPLATE = (
    "把 X 岛（nmbxd1.com）的串完整导出为 {formats} 的 Windows 小工具，"
    "带现代两栏界面，支持断点续传、图片缓存与串更新监控，也能当命令行工具用"
)

# 格式键 → 描述里用的名字（顺序即 EXPORTERS 的顺序）
FORMAT_LABELS = {
    "html": "HTML",
    "pdf": "PDF",
    "txt": "TXT",
    "markdown": "Markdown",
    "epub": "EPUB",
}

# 格式键 → 对应的 GitHub 话题标签（没配的格式就跳过）
FORMAT_TOPICS = {
    "epub": "epub",
    "markdown": "markdown",
    "pdf": "pdf",
}

# 与格式无关、一直要有的话题
BASE_TOPICS = [
    "backup",
    "desktop-app",
    "exporter",
    "gui",
    "nmbxd1",
    "python",
    "scraper",
    "tkinter",
    "windows",
    "xdao",
]

DESCRIPTION_MAX = 350

# GitHub 对话题的限制：超过 20 个、单个超过 50 个字符、或者带非法字符，
# 都会在写入时被 422 拒绝，回一句看不出所以然的话。写之前自己先验一遍。
TOPIC_MAX = 20
TOPIC_NAME_MAX = 50
TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def format_names() -> list[str]:
    """按注册表顺序列出格式名（未知格式就退回大写键名）。"""
    return [FORMAT_LABELS.get(key, key.upper()) for key in EXPORTERS]


def expected_description() -> str:
    return DESCRIPTION_TEMPLATE.format(formats=" / ".join(format_names()))


def expected_topics() -> list[str]:
    """基准话题 + 格式话题，去重并按字母序（GitHub 自己也按字母序返回）。"""
    extra = [FORMAT_TOPICS[key] for key in EXPORTERS if key in FORMAT_TOPICS]
    return sorted(set(BASE_TOPICS) | set(extra))


def describe(repo: str, token: str) -> None:
    info = request_json("GET", f"/repos/{repo}", token)
    print(f"仓库 {info.get('full_name') or repo}")
    print(f"  描述：{info.get('description') or '（未设置）'}")
    print(f"  话题：{', '.join(info.get('topics') or []) or '（未设置）'}")


def facade_problems(description: str, topics: list[str]) -> list[str]:
    """推导出来的门面信息合格吗？不合格就一条都不许往远端写。

    这个脚本写的是**公开仓库**，而写坏的后果是「门面被清空」或「被 GitHub 挡回来
    只报一句含糊话」，所以宁可在这里拦住、一个请求都不发。
    """
    problems: list[str] = []
    if not description.strip():
        problems.append("描述是空的 —— 这会把仓库描述清掉")
    elif len(description) > DESCRIPTION_MAX:
        problems.append(f"描述太长（{len(description)} > {DESCRIPTION_MAX} 个字符），先改模板")
    if not topics:
        problems.append("话题是空的 —— 这会把仓库话题全清掉")
    elif len(topics) > TOPIC_MAX:
        problems.append(f"话题太多（{len(topics)} > {TOPIC_MAX} 个），GitHub 会拒绝")
    for name in topics:
        if len(name) > TOPIC_NAME_MAX:
            problems.append(f"话题太长（{name!r}，超过 {TOPIC_NAME_MAX} 个字符）")
        elif not TOPIC_PATTERN.match(name):
            problems.append(f"话题不合规（{name!r}：只能用小写字母、数字与连字符）")
    return problems


def apply(repo: str, token: str, *, patch: bool = True) -> list[str]:
    """把推导出来的描述与话题写到远端；返回实际做了哪些改动。"""
    info = request_json("GET", f"/repos/{repo}", token)
    changed: list[str] = []

    want_desc = expected_description().strip()
    have_desc = (info.get("description") or "").strip()
    if have_desc != want_desc:
        changed.append(f"描述：{have_desc or '（空）'}\n   →   {want_desc}")
    want_topics = expected_topics()
    have_topics = sorted(info.get("topics") or [])
    if have_topics != want_topics:
        added = [t for t in want_topics if t not in have_topics]
        removed = [t for t in have_topics if t not in want_topics]
        detail = []
        if added:
            detail.append("新增 " + "、".join(added))
        if removed:
            detail.append("移除 " + "、".join(removed))
        changed.append(f"话题：{len(have_topics)} 个 → {len(want_topics)} 个（{'；'.join(detail) or '顺序调整'}）")

    if not changed:
        return []

    problems = facade_problems(want_desc, want_topics)
    if problems:
        print("推导出来的仓库门面有问题，先改代码再同步（这次一个请求都没发）：")
        for problem in problems:
            print(f"  × {problem}")
        raise SystemExit(1)

    if patch:
        # 描述走仓库端点，话题必须走它自己的端点（仓库端点的 topics 字段是只读的）
        request_json("PATCH", f"/repos/{repo}", token, payload={"description": want_desc})
        request_json("PUT", f"/repos/{repo}/topics", token, payload={"names": want_topics})
    return changed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="检查/同步仓库描述与话题")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--apply", action="store_true", help="发现问题就同步到远端")
    args = parser.parse_args(argv)

    token = gh_token()
    describe(args.repo, token)

    changed = apply(args.repo, token, patch=args.apply)
    if not changed:
        print("\n结论：描述与话题都是最新的（与代码里的格式注册表一致）")
        return 0

    print("\n发现不一致：")
    for line in changed:
        print(f"  ! {line}")
    if not args.apply:
        print("\n加 --apply 即可同步。")
        return 1

    # 发过请求 ≠ 改成了：GitHub 会规范化话题（去重、大小写）、截断描述，写入也可能
    # 只回了个 200 却没落库。所以写完再读一遍，读回来一致才敢说「已同步」。
    still = apply(args.repo, token, patch=False)
    if still:
        print("\n× 已经发过同步请求，但重新读回来的还是不一样：")
        for line in still:
            print(f"  × {line}")
        print("  多半是远端把值规范化/截断了，或者这次写入没生效 —— 别当成同步好了。")
        return 1

    print("\n已同步到远端（写完之后重新读回来核对过）。")
    describe(args.repo, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
