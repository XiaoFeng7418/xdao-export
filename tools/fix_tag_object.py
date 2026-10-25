"""让本地附注标签与远端 tag 对象**同一串字节**（否则 git 认为两边不一致）。

GitHub 用接口建 tag 对象时，默认把 tagger 的时间写成 UTC 的 ISO（JSON 里看到 Z），
但重建对象字节时用的时区偏移要实测：v0.19.4 这版是 `+0800` 且 message 不带结尾换行。
所以这里不猜，直接把 (偏移 × 结尾换行) 四种组合都算一遍，命中的那串字节写进本地库。

用法：python fix_tag_object.py v0.19.4
"""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xdao.github_api import gh_token, request_bytes  # noqa: E402

REPO = "XiaoFeng7418/xdao-export"


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True)
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{done.stderr.decode('utf-8', 'replace')}")
    return done.stdout.decode("utf-8", "replace")


def get(path: str) -> dict:
    return json.loads(
        request_bytes(
            "GET",
            path,
            token=gh_token(),
            timeout=60.0,
            user_agent="xdao-tag",
            api_version="2022-11-28",
            attempts=4,
        )
    )


def local_offset(tag_name: str) -> str:
    raw = git("cat-file", "tag", tag_name)
    for line in raw.splitlines():
        if line.startswith("tagger "):
            return line.split()[-1]
    return "+0000"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("用法：python fix_tag_object.py <标签名>")
        return 2
    name = argv[0]
    ref = get(f"/repos/{REPO}/git/ref/tags/{name}")
    remote = ref["object"]["sha"]
    tag = get(f"/repos/{REPO}/git/tags/{remote}")
    commit = tag["object"]["sha"]
    tagger = tag["tagger"]
    stamp = int(datetime.datetime.fromisoformat(tagger["date"].replace("Z", "+00:00")).timestamp())
    message = tag["message"]
    local = git("rev-parse", f"refs/tags/{name}").strip()
    print(f"远端 {remote}  本地 {local}")
    if local == remote:
        print("两边已经一致，不用动")
        return 0

    offsets = [local_offset(name), "+0800", "+0000"]
    for offset in offsets:
        for body_msg in (message, message + "\n"):
            body = (
                f"object {commit}\n"
                "type commit\n"
                f"tag {name}\n"
                f"tagger {tagger['name']} <{tagger['email']}> {stamp} {offset}\n"
                "\n" + body_msg
            )
            digest = hashlib.sha1(b"tag %d\0" % len(body.encode()) + body.encode()).hexdigest()
            if digest != remote:
                continue
            written = subprocess.run(
                ["git", "hash-object", "-t", "tag", "-w", "--stdin"],
                input=body.encode("utf-8"),
                capture_output=True,
            ).stdout.decode().strip()
            if written != remote:
                print("命中组合但写库后哈希不同：", written[:12])
                return 1
            subprocess.run(["git", "update-ref", f"refs/tags/{name}", written], check=True)
            now = git("rev-parse", f"refs/tags/{name}").strip()
            print(f"命中组合：偏移 {offset}、message {'带' if body_msg.endswith(chr(10)) else '不带'}结尾换行")
            print("本地已重写：", now[:12], "一致" if now == remote else "仍不一致")
            return 0 if now == remote else 1
    print("四种组合都没命中，说明远端对象的拼法还有别的差异")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
