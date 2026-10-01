"""`tools/ci_logs.py` 的用例：取 CI 日志这一步不许「看着没问题」。

这个工具是排查 CI 的第一入口（`MAINTENANCE.md` 里明确写了「取日志用它，
不要用 `gh run view --log`」）。它以前有四处会让人把红的看成绿的：

- job 前面那个记号只认 `failure`，`cancelled` / `timed_out` / `action_required` /
  `startup_failure` / `stale` 全都印 ✓ —— 一次被取消的运行看着跟全绿一样；
- `--job` 拼错（比如把矩阵里的 `ubuntu / py3.10` 写成 `ubuntu3.10`）时只印一行
  「没有匹配的 job」并返回 0，什么都不看却像「没什么可看的」；
- `--grep` 默认只在**失败或还在跑**的 job 里搜，所以全绿的运行上 `--grep 超时`
  一行都没搜、一声不吭地返回 0 —— 你以为搜过了；
- `--grep` 匹配不到时同样完全沉默，看起来就像「日志里没这个错」。

下面用假接口跑 `main()`（`api` / `job_logs` / `token` 全换成测试自己的），
`api()` / `job_logs()` 的重试与重定向另用假的 `urlopen` / `build_opener` 单测。
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ci_logs  # noqa: E402

REPO = "owner/name"


# ---------------------------------------------------------------- 假接口


class _Resp:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, body: bytes = b'{"message":"boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com/x", code, "boom", {}, io.BytesIO(body))


def run_obj(**over) -> dict:
    run = {
        "id": 900,
        "run_number": 12,
        "name": "测试",
        "head_sha": "abcdef1234567890",
        "status": "completed",
        "conclusion": "success",
        "head_commit": {"message": "第一次提交\n\n正文"},
    }
    run.update(over)
    return run


def job_obj(name: str, conclusion, *, job_id: int, steps=None, status="completed") -> dict:
    if steps is None:
        steps = [{"name": "跑测试", "conclusion": conclusion}]
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "steps": steps,
    }


class _Fake:
    """顶掉 `ci_logs.api` / `ci_logs.job_logs` 的最小替身。"""

    def __init__(self, run=None, jobs=None, logs=None):
        self.run = run if run is not None else run_obj()
        self.jobs = list(jobs or [])
        self.logs = dict(logs or {})
        self.urls: list[str] = []
        self.log_calls: list[int] = []

    def api(self, path: str, tok: str, **kw):
        self.urls.append(path)
        if path.endswith("/jobs"):
            return {"jobs": self.jobs}
        if "actions/runs?per_page=1" in path:
            return {"workflow_runs": [self.run]}
        if "/actions/runs/" in path and "/jobs" not in path:
            return self.run
        raise AssertionError(f"没准备的接口：{path}")

    def job_logs(self, tok: str, job_id: int, **kw) -> str:
        self.log_calls.append(job_id)
        return self.logs.get(job_id, f"job {job_id} 的日志\n")


def use(monkeypatch, fake: _Fake) -> None:
    monkeypatch.setattr(ci_logs, "api", fake.api)
    monkeypatch.setattr(ci_logs, "job_logs", fake.job_logs)


def run_main(monkeypatch, capsys, fake: _Fake, argv=None):
    use(monkeypatch, fake)
    code = ci_logs.main(argv if argv is not None else ["--repo", REPO])
    return code, capsys.readouterr()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """没顶掉就走真网络的那条用例，必须立刻失败而不是真去发请求。"""

    def refuse(*a, **kw):
        raise AssertionError(
            "这条用例要真的发请求了：用 use() 换掉 ci_logs.api / job_logs，"
            "或顶掉 urllib.request.urlopen / build_opener"
        )

    monkeypatch.setattr(ci_logs.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(ci_logs.urllib.request, "build_opener", refuse)
    monkeypatch.setattr(ci_logs.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ci_logs, "token", lambda: "fake-token")


# ---------------------------------------------------------------- 「没成功」的判定


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"],
)
def test_not_success_conclusions_count_as_failed(conclusion):
    assert ci_logs.has_failed(conclusion) is True


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral", None])
def test_success_like_conclusions_do_not_count_as_failed(conclusion):
    assert ci_logs.has_failed(conclusion) is False


def test_none_conclusion_is_shown_as_running_not_as_a_missing_value():
    assert ci_logs.conclusion_text(None) == "还在跑"
    assert ci_logs.conclusion_text("cancelled") == "cancelled"


# ---------------------------------------------------------------- main() 的记号


def test_cancelled_and_timed_out_jobs_are_not_marked_green(monkeypatch, capsys):
    jobs = [
        job_obj("红了", "failure", job_id=1),
        job_obj("被取消", "cancelled", job_id=2),
        job_obj("超时", "timed_out", job_id=3),
        job_obj("绿的", "success", job_id=4),
        job_obj("还在跑", None, job_id=5, status="in_progress"),
    ]
    code, out = run_main(monkeypatch, capsys, _Fake(jobs=jobs))

    assert code == 0
    assert "=== ✗ 红了" in out.out
    assert "=== ✗ 被取消" in out.out
    assert "=== ✗ 超时" in out.out
    assert "=== ✓ 绿的" in out.out
    assert "=== … 还在跑" in out.out
    assert "in_progress/还在跑" in out.out


def test_logs_are_fetched_only_for_bad_or_running_jobs(monkeypatch, capsys):
    jobs = [
        job_obj("红的", "failure", job_id=1),
        job_obj("取消的", "cancelled", job_id=2),
        job_obj("还在跑的", None, job_id=3, status="in_progress"),
        job_obj("绿的", "success", job_id=4),
    ]
    fake = _Fake(jobs=jobs)
    run_main(monkeypatch, capsys, fake)
    assert fake.log_calls == [1, 2, 3]
    assert fake.log_calls.count(4) == 0


def test_a_fully_green_run_fetches_no_logs(monkeypatch, capsys):
    fake = _Fake(jobs=[job_obj("绿的", "success", job_id=1)])
    code, out = run_main(monkeypatch, capsys, fake)
    assert code == 0
    assert fake.log_calls == []
    assert "--- 日志 ---" not in out.out


def test_step_marks_cover_success_failure_and_running(monkeypatch, capsys):
    jobs = [
        job_obj(
            "多步",
            "failure",
            job_id=1,
            steps=[
                {"name": "装依赖", "conclusion": "success"},
                {"name": "跑测试", "conclusion": "failure"},
                {"name": "上传", "conclusion": "skipped"},
                {"name": "收尾", "conclusion": None},
            ],
        )
    ]
    _, out = run_main(monkeypatch, capsys, _Fake(jobs=jobs))
    assert "[OK  ] 装依赖" in out.out
    assert "[✗ 失败] 跑测试" in out.out
    assert "[skipped] 上传" in out.out
    assert "[还在跑] 收尾" in out.out


# ---------------------------------------------------------------- --job 筛选


def test_unknown_job_lists_the_real_names_and_fails(monkeypatch, capsys):
    jobs = [
        job_obj("测试 / windows / py3.12", "success", job_id=1),
        job_obj("测试 / ubuntu / py3.12", "success", job_id=2),
    ]
    fake = _Fake(jobs=jobs)
    code, out = run_main(monkeypatch, capsys, fake, ["--repo", REPO, "--job", "ubuntu3.10"])

    assert code == 1
    assert "ubuntu3.10" in out.out
    assert "测试 / windows / py3.12" in out.out
    assert "测试 / ubuntu / py3.12" in out.out
    assert "别当成都绿了" in out.out
    assert fake.log_calls == []


def test_unknown_job_does_not_write_the_save_file(monkeypatch, capsys, tmp_path):
    target = tmp_path / "logs.txt"
    fake = _Fake(jobs=[job_obj("测试 / ubuntu / py3.12", "success", job_id=2)])
    code, out = run_main(
        monkeypatch, capsys, fake, ["--repo", REPO, "--job", "nope", "--save", str(target)]
    )
    assert code == 1
    assert not target.exists()
    assert "也没有写" in out.out


def test_a_run_without_jobs_is_not_a_success(monkeypatch, capsys, tmp_path):
    target = tmp_path / "logs.txt"
    code, out = run_main(monkeypatch, capsys, _Fake(jobs=[]), ["--repo", REPO, "--save", str(target)])
    assert code == 1
    assert "一个 job 都没有" in out.out
    assert not target.exists()


def test_job_filter_still_matches_substrings(monkeypatch, capsys):
    jobs = [
        job_obj("测试 / windows / py3.12", "success", job_id=1),
        job_obj("测试 / ubuntu / py3.12", "success", job_id=2),
    ]
    code, out = run_main(monkeypatch, capsys, _Fake(jobs=jobs), ["--repo", REPO, "--job", "ubuntu"])
    assert code == 0
    assert "ubuntu / py3.12" in out.out
    assert "windows / py3.12" not in out.out


# ---------------------------------------------------------------- --grep


def test_grep_pulls_logs_even_when_the_run_is_green(monkeypatch, capsys):
    job = job_obj("绿的", "success", job_id=1)
    fake = _Fake(jobs=[job], logs={1: "第一步\n读取超时了\n第三步\n"})
    code, out = run_main(monkeypatch, capsys, fake, ["--repo", REPO, "--grep", "超时"])

    assert code == 0
    assert fake.log_calls == [1], "全绿时也真的把日志拉下来搜，才叫搜过"
    assert "读取超时了" in out.out


def test_grep_without_a_match_says_so_and_fails(monkeypatch, capsys):
    fake = _Fake(
        jobs=[job_obj("红的", "failure", job_id=1)],
        logs={1: "第一行\n第二行\n第三行\n"},
    )
    code, out = run_main(monkeypatch, capsys, fake, ["--repo", REPO, "--grep", "找不到的东西"])

    assert code == 1
    assert "没有一行匹配" in out.out
    assert "1 个 job" in out.out
    assert "3 行" in out.out
    assert "这不是「CI 没问题」" in out.out


def test_grep_reports_how_many_jobs_it_searched(monkeypatch, capsys):
    jobs = [
        job_obj("甲", "failure", job_id=1),
        job_obj("乙", "cancelled", job_id=2),
    ]
    fake = _Fake(jobs=jobs, logs={1: "甲一\n甲二\n", 2: "乙一\n"})
    code, out = run_main(monkeypatch, capsys, fake, ["--repo", REPO, "--grep", "zzz"])
    assert code == 1
    assert "2 个 job" in out.out
    assert "3 行" in out.out


def test_grep_context_prints_neighbouring_lines(monkeypatch, capsys):
    fake = _Fake(
        jobs=[job_obj("红的", "failure", job_id=1)],
        logs={1: "第一行\n中间那行\n第三行\n第四行\n"},
    )
    code, out = run_main(
        monkeypatch, capsys, fake, ["--repo", REPO, "--grep", "中间", "--context", "1"]
    )
    assert code == 0
    assert "第一行" in out.out
    assert "第三行" in out.out
    assert "第四行" not in out.out


# ---------------------------------------------------------------- --save


def test_save_writes_every_job_with_its_name(monkeypatch, capsys, tmp_path):
    target = tmp_path / "logs.txt"
    jobs = [
        job_obj("红的", "failure", job_id=1),
        job_obj("绿的", "success", job_id=2),
    ]
    fake = _Fake(jobs=jobs, logs={1: "红日志", 2: "绿日志"})
    code, out = run_main(monkeypatch, capsys, fake, ["--repo", REPO, "--save", str(target)])

    assert code == 0
    assert fake.log_calls == [1, 2]
    text = target.read_text(encoding="utf-8")
    assert "########## 红的 ##########\n红日志" in text
    assert "########## 绿的 ##########\n绿日志" in text
    assert "完整日志已写入" in out.out


# ---------------------------------------------------------------- 运行本身


def test_a_run_that_is_still_going_gets_a_warning(monkeypatch, capsys):
    run = run_obj(status="in_progress", conclusion=None)
    fake = _Fake(run=run, jobs=[job_obj("还在跑", None, job_id=1, status="in_progress")])
    code, out = run_main(monkeypatch, capsys, fake)

    assert code == 0
    assert "还没跑完" in out.out
    assert "in_progress/还在跑" in out.out


def test_explicit_run_id_is_used(monkeypatch, capsys):
    fake = _Fake(jobs=[job_obj("绿的", "success", job_id=1)])
    run_main(monkeypatch, capsys, fake, ["--repo", REPO, "--run", "4321"])
    assert fake.urls[0].endswith("/actions/runs/4321")


def test_latest_run_is_used_by_default(monkeypatch, capsys):
    fake = _Fake(jobs=[job_obj("绿的", "success", job_id=1)])
    run_main(monkeypatch, capsys, fake)
    assert "per_page=1" in fake.urls[0]
    assert fake.urls[1].endswith("/actions/runs/900/jobs")


def test_no_runs_at_all_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(ci_logs, "api", lambda path, tok, **kw: {"workflow_runs": []})
    with pytest.raises(SystemExit) as exc:
        ci_logs.latest_run("fake-token", REPO)
    assert "还没有任何 Actions 运行" in str(exc.value)


# ---------------------------------------------------------------- api() 的重试


def test_api_retries_server_errors(monkeypatch, capsys):
    calls: list[str] = []
    answers = [http_error(502), http_error(503), _Resp(json.dumps({"ok": 1}).encode("utf-8"))]

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ci_logs.urllib.request, "urlopen", urlopen)
    assert ci_logs.api("/x", "tok") == {"ok": 1}
    assert len(calls) == 3
    assert "502" in capsys.readouterr().err


def test_api_retries_rate_limit(monkeypatch):
    calls: list[str] = []
    answers = [http_error(429), _Resp(b'{"ok": 2}')]

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ci_logs.urllib.request, "urlopen", urlopen)
    assert ci_logs.api("/x", "tok") == {"ok": 2}
    assert len(calls) == 2


def test_api_does_not_retry_a_404(monkeypatch):
    calls: list[str] = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise http_error(404, b'{"message":"Not Found"}')

    monkeypatch.setattr(ci_logs.urllib.request, "urlopen", urlopen)
    with pytest.raises(SystemExit) as exc:
        ci_logs.api("/x", "tok")
    assert "GitHub API 404" in str(exc.value)
    assert len(calls) == 1


def test_api_gives_up_after_retries_on_network_errors(monkeypatch):
    calls: list[str] = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError("temporary failure")

    monkeypatch.setattr(ci_logs.urllib.request, "urlopen", urlopen)
    with pytest.raises(SystemExit) as exc:
        ci_logs.api("/x", "tok", retries=3)
    assert "请求失败" in str(exc.value)
    assert len(calls) == 3


def test_api_sends_the_token(monkeypatch):
    seen: list[dict] = []

    def urlopen(req, timeout=None):
        seen.append(dict(req.headers))
        return _Resp(b'{"ok": 3}')

    monkeypatch.setattr(ci_logs.urllib.request, "urlopen", urlopen)
    ci_logs.api("/x", "secret-token")
    assert seen[0]["Authorization"] == "Bearer secret-token"


# ---------------------------------------------------------------- job_logs()


class _Opener:
    def __init__(self, answer):
        self.answer = answer
        self.seen: list[dict] = []

    def open(self, req, timeout=None):
        self.seen.append(dict(req.headers))
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Resp(self.answer)


def test_job_logs_unpacks_a_zip(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("0_第一步.txt", "第一步\n")
        zf.writestr("1_第二步.txt", "第二步\n")
    opener = _Opener(buf.getvalue())
    monkeypatch.setattr(ci_logs.urllib.request, "build_opener", lambda *h: opener)

    text = ci_logs.job_logs("tok", 7, repo=REPO)
    assert text == "第一步\n\n第二步\n"
    assert opener.seen[0]["Authorization"] == "Bearer tok"


def test_job_logs_accepts_plain_text(monkeypatch):
    opener = _Opener("纯文本日志".encode("utf-8"))
    monkeypatch.setattr(ci_logs.urllib.request, "build_opener", lambda *h: opener)
    assert ci_logs.job_logs("tok", 7, repo=REPO) == "纯文本日志"


def test_job_logs_retries_then_gives_up(monkeypatch, capsys):
    opener = _Opener(OSError("unexpected EOF"))
    monkeypatch.setattr(ci_logs.urllib.request, "build_opener", lambda *h: opener)
    with pytest.raises(SystemExit) as exc:
        ci_logs.job_logs("tok", 7, repo=REPO, retries=2)
    assert "日志下载失败" in str(exc.value)
    assert "OSError: unexpected EOF" in capsys.readouterr().err


def test_redirect_drops_the_authorization_header():
    """日志真实地址 302 到带签名的临时 URL；带着令牌过去会被判签名不匹配。"""
    req = urllib.request.Request(
        "https://api.github.com/repos/o/n/actions/jobs/7/logs",
        headers={"Authorization": "Bearer tok", "Accept": "application/json"},
    )
    handler = ci_logs.NoAuthRedirect()
    new = handler.redirect_request(
        req, None, 302, "Found", {}, "https://results-receiver.example/log?sig=x"
    )
    assert new is not None
    assert "Authorization" not in new.headers
    assert new.headers["Accept"] == "application/json"
