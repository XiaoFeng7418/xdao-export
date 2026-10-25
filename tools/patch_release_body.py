# -*- coding: utf-8 -*-
"""把 GitHub Release 的正文改成仓库里那份发布说明（按 tag 找 id 再 PATCH）。

用法：python tools/patch_release_body.py <tag> <发布说明文件>

为什么需要它：`POST` 建 Release 只在第一次；之后改正文只能 `PATCH /releases/{id}`，
而 `PATCH /releases/tags/{tag}` 是 404（GitHub 只给了 GET 的 tag 形式）。

什么时候用：发布说明改了一句话（比如忘了写「免安装包」），不想删了 Release 重建，
就改仓库里的 `docs/RELEASE_NOTES_<版本>.md`，commit + push，再跑这个脚本。
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import xdao.github_api as api  # noqa: E402

REPO = "XiaoFeng7418/xdao-export"
AGENT = "xdao-export-release"


def read_notes(path: pathlib.Path) -> str:
    """发布说明是 UTF-8（无 BOM）；读不出来就把路径报出来，别让人猜。"""
    if not path.is_file():
        raise FileNotFoundError(f"发布说明不存在：{path}")
    return path.read_text(encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法：python tools/patch_release_body.py <tag> <发布说明文件>")
        return 1
    tag, notes = argv[1], pathlib.Path(argv[2])
    body = read_notes(notes)
    token = api.gh_token()
    release = api.request_bytes(
        "GET", f"/repos/{REPO}/releases/tags/{tag}", token=token, user_agent=AGENT, parse=json.loads
    )
    out = api.request_bytes(
        "PATCH",
        f"/repos/{REPO}/releases/{release['id']}",
        token=token,
        payload={"body": body},
        user_agent=AGENT,
        parse=json.loads,
    )
    print(f"已更新 Release {out['tag_name']}（id {out['id']}），正文 {len(out.get('body') or '')} 字符")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
