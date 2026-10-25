# -*- coding: utf-8 -*-
"""把本地附注标签推到 GitHub（走 API，绕开不通的 443 直连）。

用法：python tools/push_tag_via_api.py v0.19.7

说明：先从本地 `git cat-file -p <tag>` 里取 tagger 与正文，按 GitHub 的
`POST /git/tags` 建标签对象，再建 `refs/tags/<name>`；回读核对一次。

注意：GitHub 建出来的标签对象字节和本地 `git tag -a` 的不一样（它把 message
结尾的换行去掉了），所以推完还要跑一次 `tools/fix_tag_object.py` 让本地对象
对齐远端 sha，否则 `repo_check` 会报标签对不上。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xdao.github_api import ApiError, gh_token, linear_backoff, request_bytes  # noqa: E402

REPO = "XiaoFeng7418/xdao-export"


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return done.stdout.decode("utf-8")


def api(token: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = request_bytes(
        method,
        path,
        token=token,
        payload=payload,
        timeout=120.0,
        user_agent="xdao-push-tag",
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
    """`名字 <邮箱> 时间戳 +0800` → GitHub 要的 `{name, email, date}`。"""
    who, stamp, offset = line.rsplit(" ", 2)
    name, _, email = who.partition(" <")
    sign = 1 if offset[0] == "+" else -1
    tz = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])))
    when = datetime.fromtimestamp(int(stamp), tz=tz).isoformat()
    return {"name": name, "email": email.rstrip(">"), "date": when}


def split_tag(raw: str) -> tuple[str, dict, str]:
    """把 `git cat-file tag` 的输出拆成（指向的提交、tagger、message）。"""
    headers, _, message = raw.partition("\n\n")
    target = tagger = None
    for line in headers.splitlines():
        if line.startswith("object "):
            target = line.split(" ", 1)[1]
        elif line.startswith("tagger "):
            tagger = parse_ident(line[len("tagger ") :])
    if not target or tagger is None:
        raise ValueError("标签头解析失败：缺 object 或 tagger")
    return target, tagger, message.rstrip("\n")


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit("用法：python tools/push_tag_via_api.py <tag>")
    name = argv[0]
    token = gh_token()

    target, tagger, message = split_tag(git("cat-file", "tag", name))

    existing = None
    try:
        existing = api(token, "GET", f"/repos/{REPO}/git/ref/tags/{name}")
    except ApiError:
        existing = None
    if existing:
        print(f"远端已有标签 {name} → {existing['object']['sha'][:12]}")
        return 0

    created = api(
        token,
        "POST",
        f"/repos/{REPO}/git/tags",
        {"tag": name, "message": message, "object": target, "type": "commit", "tagger": tagger},
    )
    print(f"标签对象 {created['sha'][:12]}")
    api(token, "POST", f"/repos/{REPO}/git/refs", {"ref": f"refs/tags/{name}", "sha": created["sha"]})

    check = api(token, "GET", f"/repos/{REPO}/git/ref/tags/{name}")
    actual = check["object"]["sha"]
    print(f"回读 {name}：{actual[:12]}")
    local = git("rev-parse", name).strip()
    print(f"本地标签对象：{local[:12]}")
    return 0 if actual == local else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
