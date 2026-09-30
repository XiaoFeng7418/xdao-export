"""下载 GitHub Actions 的运行日志（失败时辅助排查 CI）。

为什么不用 `gh run view --log`：日志真实地址在
results-receiver.actions.githubusercontent.com，带签名的临时 URL，
本机网络下用 gh 取经常被中途掐断（unexpected EOF）。
这里自己实现：先拿带签名的日志地址，再分段重试下载并解压。

用法：
    python tools/ci_logs.py                # 最新一次运行，打印失败步骤
    python tools/ci_logs.py --run 12345    # 指定运行 id
    python tools/ci_logs.py --job ubuntu   # 只看名字含 ubuntu 的 job
    python tools/ci_logs.py --save out.txt # 顺便存一份完整日志
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

REPO = "XiaoFeng7418/xdao-export"
API = "https://api.github.com"


def token() -> str:
    """优先取环境变量，其次复用 make_release 里的 gh 令牌读取逻辑。"""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import make_release  # type: ignore

        return make_release.gh_token()
    except Exception as exc:  # pragma: no cover - 仅本地辅助脚本
        raise SystemExit(f"拿不到 GitHub 令牌：{exc}")


def api(path: str, tok: str, *, raw: bool = False, retries: int = 4):
    url = path if path.startswith("http") else API + path
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "xdao-ci-logs")
        if not raw:
            req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
                return data if raw else json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            raise SystemExit(f"GitHub API {exc.code}: {body}") from exc
        except Exception as exc:  # 网络抖动，重试
            last = exc
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"请求失败：{url} -> {last}")


def latest_run(tok: str, repo: str = REPO) -> dict:
    runs = api(f"/repos/{repo}/actions/runs?per_page=1", tok)
    items = runs.get("workflow_runs") or []
    if not items:
        raise SystemExit("该仓库还没有任何 Actions 运行")
    return items[0]


def job_logs(tok: str, job_id: int, *, repo: str = REPO, retries: int = 5) -> str:
    """拿单个 job 的纯文本日志。地址是 302 到带签名的临时 URL。"""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            # 不跟随重定向手动处理：跟随重定向后不能带 Authorization，
            # 否则云存储会以签名不匹配回 401。
            class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    new = super().redirect_request(req, fp, code, msg, headers, newurl)
                    if new is not None:
                        new.headers.pop("Authorization", None)
                    return new

            opener = urllib.request.build_opener(NoAuthRedirect)
            req = urllib.request.Request(f"{API}/repos/{repo}/actions/jobs/{job_id}/logs")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("User-Agent", "xdao-ci-logs")
            req.add_header("Authorization", f"Bearer {tok}")
            with opener.open(req, timeout=180) as resp:
                data = resp.read()
            if data[:2] == b"PK":  # zip
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    return "\n".join(
                        zf.read(n).decode("utf-8", "replace") for n in zf.namelist()
                    )
            return data.decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            print(f"    （下载被中断，第 {attempt + 1} 次：{type(exc).__name__}）", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    raise SystemExit(f"日志下载失败：{last}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 xdao-export 的 CI 运行日志")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--run", type=int, default=None, help="运行 id，默认最新一次")
    parser.add_argument("--job", default=None, help="job 名筛选，子串匹配（如 ubuntu）")
    parser.add_argument("--save", default=None, help="把完整日志写到这个文件")
    parser.add_argument("--grep", default=None, help="只打印含该子串的行")
    parser.add_argument("--context", type=int, default=0, help="配合 --grep：额外打印前后行数")
    args = parser.parse_args(argv)
    repo = args.repo
    tok = token()

    run = (
        api(f"/repos/{repo}/actions/runs/{args.run}", tok)
        if args.run
        else latest_run(tok, repo)
    )
    print(f'运行 #{run["run_number"]}  {run["name"]}  {run["head_sha"][:8]}  {run["status"]}/{run["conclusion"]}')
    print(f'说明：{(run.get("head_commit") or {}).get("message", "").splitlines()[0] if run.get("head_commit") else ""}')

    jobs = api(f'/repos/{repo}/actions/runs/{run["id"]}/jobs', tok)["jobs"]
    if args.job:
        jobs = [j for j in jobs if args.job.lower() in j["name"].lower()]
    if not jobs:
        print("没有匹配的 job")
        return 0

    chunks: list[str] = []
    for job in jobs:
        flag = "✗" if job.get("conclusion") == "failure" else "✓"
        print(f'\n=== {flag} {job["name"]} —— {job["status"]}/{job.get("conclusion")} ===')
        for step in job.get("steps", []):
            mark = "OK  " if step.get("conclusion") == "success" else step.get("conclusion", "?")
            print(f'  [{mark}] {step["name"]}')
        if job.get("conclusion") in ("failure", None) or args.save:
            print("  --- 日志 ---")
            text = job_logs(tok, job["id"], repo=repo)
            chunks.append(f"########## {job['name']} ##########\n{text}")

    if args.save and chunks:
        with open(args.save, "w", encoding="utf-8") as fh:
            fh.write("\n".join(chunks))
        print(f"\n完整日志已写入 {args.save}")

    if args.grep:
        import re

        pattern = re.compile(args.grep)
        for text in chunks:
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if pattern.search(line):
                    lo = max(0, i - args.context)
                    hi = min(len(lines), i + args.context + 1)
                    print("\n".join(lines[lo:hi]))
                    print("-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
