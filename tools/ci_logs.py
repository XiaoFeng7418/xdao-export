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
    python tools/ci_logs.py --grep 超时 --context 3

退出码（这个工具的职责是「把日志摆出来」，所以只有下面这些情况才不是 0）：
    0  照办了；
    1  --job 没匹配上任何 job（拼错了？此时会把这次运行里真实的 job 名列出来，
       并且**不写** --save 指定的文件）、这次运行一个 job 都没有、
       --grep 一行都没匹配上、运行还没跑完该看的看不到。

两处容易看错的记号：
    - job 前面是 ✗ 的**不只** failure：cancelled / timed_out / action_required /
      startup_failure / stale 都算没成功（以前只认 failure，这几种都印 ✓，
      一次被取消的运行看起来跟全绿一样）；还在跑的 job 印 …。
    - 带 --grep 时也会把日志真的拉下来（默认只有失败或还在跑的 job 才拉），
      匹配不到一定说话 —— 「没搜到」不等于「CI 没问题」。
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

# GitHub 的结论里 failure 只是「没成功」的一种。以前只认 failure，
# 于是 cancelled / timed_out 这些也印 ✓ —— 被取消的运行看着跟全绿一样。
FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
)

# 值得重试的 HTTP 状态：服务端抽风或限流。4xx（除 429）是请求本身的问题，重试没用。
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def has_failed(conclusion: str | None) -> bool:
    """这个结论算不算「没成功」。"""
    return conclusion in FAILED_CONCLUSIONS


def conclusion_text(conclusion: str | None) -> str:
    """结论的显示文字。None 不是「没有结论」，是「还在跑」。"""
    return "还在跑" if conclusion is None else conclusion


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


def api(path: str, tok: str, *, retries: int = 4):
    url = path if path.startswith("http") else API + path
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "xdao-ci-logs")
        req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            if exc.code in RETRY_STATUS and attempt + 1 < retries:
                last = exc
                print(
                    f"    （GitHub API {exc.code}，第 {attempt + 1} 次：等一会儿再试）",
                    file=sys.stderr,
                )
                time.sleep(2 * (attempt + 1))
                continue
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


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """跟随时去掉 Authorization。

    日志真实地址 302 到 results-receiver 的带签名临时 URL；把
    Authorization 一起带过去，云存储会以「签名不匹配」回 401。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.headers.pop("Authorization", None)
        return new


def job_logs(tok: str, job_id: int, *, repo: str = REPO, retries: int = 5) -> str:
    """拿单个 job 的纯文本日志。地址是 302 到带签名的临时 URL。"""
    last: Exception | None = None
    for attempt in range(retries):
        try:
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
            print(
                f"    （下载被中断，第 {attempt + 1} 次：{type(exc).__name__}: {exc}）",
                file=sys.stderr,
            )
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
    if run.get("status") != "completed":
        print(
            f'× 这次运行还没跑完（{run.get("status")}）'
            "—— 下面的结论不是最终结果，等它结束了再看。"
        )
    print(
        f'运行 #{run["run_number"]}  {run["name"]}  {run["head_sha"][:8]}  '
        f'{run["status"]}/{conclusion_text(run.get("conclusion"))}'
    )
    print(f'说明：{(run.get("head_commit") or {}).get("message", "").splitlines()[0] if run.get("head_commit") else ""}')

    all_jobs = api(f'/repos/{repo}/actions/runs/{run["id"]}/jobs', tok)["jobs"]
    jobs = [j for j in all_jobs if args.job.lower() in j["name"].lower()] if args.job else list(all_jobs)
    if not jobs:
        if args.job:
            print(f'× 没有名字含 "{args.job}" 的 job —— 这次运行里的 job 是：')
            for job in all_jobs:
                print(f'    {job["name"]}')
            print("  一个都没看，别当成都绿了。")
        else:
            print("× 这次运行一个 job 都没有 —— 没什么可看的。")
        if args.save:
            print(f"  也没有写 {args.save}（没有日志可写）。")
        return 1

    chunks: list[str] = []  # 存盘用，每段前面带 job 名分隔线
    bodies: list[str] = []  # 搜索用，只有日志正文
    for job in jobs:
        conclusion = job.get("conclusion")
        if has_failed(conclusion):
            flag = "✗"
        elif conclusion is None:
            flag = "…"
        else:
            flag = "✓"
        print(f'\n=== {flag} {job["name"]} —— {job["status"]}/{conclusion_text(conclusion)} ===')
        for step in job.get("steps", []):
            if step.get("conclusion") == "success":
                mark = "OK  "
            elif has_failed(step.get("conclusion")):
                mark = "✗ 失败"
            else:
                mark = step.get("conclusion") or "还在跑"
            print(f'  [{mark}] {step["name"]}')
        if has_failed(conclusion) or conclusion is None or args.save or args.grep:
            print("  --- 日志 ---")
            text = job_logs(tok, job["id"], repo=repo)
            chunks.append(f"########## {job['name']} ##########\n{text}")
            bodies.append(text)

    if args.save:
        if chunks:
            with open(args.save, "w", encoding="utf-8") as fh:
                fh.write("\n".join(chunks))
            print(f"\n完整日志已写入 {args.save}")
        else:
            print(f"\n× 没拉到任何日志，{args.save} 没写。")
            return 1

    if args.grep:
        import re

        pattern = re.compile(args.grep)
        hits = 0
        lines_seen = 0
        for text in bodies:
            lines = text.splitlines()
            lines_seen += len(lines)
            for i, line in enumerate(lines):
                if pattern.search(line):
                    hits += 1
                    lo = max(0, i - args.context)
                    hi = min(len(lines), i + args.context + 1)
                    print("\n".join(lines[lo:hi]))
                    print("-" * 60)
        if hits == 0:
            print(
                f'× 没有一行匹配 "{args.grep}"：在 {len(bodies)} 个 job、{lines_seen} 行日志里找过，'
                "是真没有 —— 这不是「CI 没问题」。"
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
