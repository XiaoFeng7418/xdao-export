"""``tools/repo_info.py`` 要对着**公开仓库**写东西，所以两件事都要有用例兜着。

一、写之前得先验：描述空了、话题空了、话题超过 20 个或含非法字符，GitHub 要么
直接拒绝（422，只回一句看不出所以然的话），要么照单全收 —— 收下的就是把仓库门面
清空。仓库门面是给人看的第一眼，坏在这里没人会立刻发现。

二、写完得再读回来核对：发过请求 **不等于** 改成了。GitHub 会规范化话题（去重、
大小写）、截断描述，写入也可能只回了个 200 却没落库。旧代码只要请求没抛异常就印
「已同步到远端。」并 ``return 0`` —— 于是「远端根本没照办」被报成了成功。这正是
这一串轮次一直在补的同一类毛病：看着同步完了，其实没对齐。

用例给 ``main()`` 喂一份假 API（``gh_token`` / ``request_json`` 都换成测试自己的），
只有被测的判断逻辑是真的；假 API 还能装成「收下请求但没照办」，用来验第二件事。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import repo_info  # noqa: E402
from repo_info import expected_description, expected_topics  # noqa: E402

REPO = "owner/name"


class _FakeApi:
    """假的 GitHub：GET 回一份仓库信息，PATCH / PUT 记下请求。

    ``obey=False`` 表示「收下了请求但没照办」（远端规范化了值，或者这次写入没生效），
    现实里真会发生 —— 用例靠它验「写完读回来核对」这一步。
    """

    def __init__(self, *, description: str = "", topics: list[str] | None = None,
                 full_name: str | None = REPO, obey: bool = True) -> None:
        self.state: dict = {
            "description": description,
            "topics": list(topics or []),
        }
        if full_name is not None:
            self.state["full_name"] = full_name
        self.obey = obey
        self.calls: list[tuple[str, str, dict | None]] = []

    # 签名与 tools/make_release.py 的 request_json 一致
    def __call__(self, method: str, path: str, token: str,
                 payload: dict | None = None, retries: int = 4) -> dict:
        self.calls.append((method, path, payload))
        if method == "GET":
            return dict(self.state)
        if method == "PATCH":
            if self.obey:
                self.state["description"] = payload["description"]
            return dict(self.state)
        if method == "PUT":
            if self.obey:
                self.state["topics"] = list(payload["names"])
            return dict(self.state)
        raise AssertionError(f"用例没准备这个请求：{method} {path}")

    def methods(self) -> list[str]:
        return [method for method, _path, _payload in self.calls]

    def writes(self) -> list[tuple[str, str, dict | None]]:
        return [call for call in self.calls if call[0] != "GET"]

    def payload_of(self, method: str) -> dict:
        for got_method, _path, payload in self.calls:
            if got_method == method:
                return payload or {}
        raise AssertionError(f"没有发出 {method} 请求：{self.methods()}")


def use_api(monkeypatch, api: _FakeApi) -> _FakeApi:
    monkeypatch.setattr(repo_info, "request_json", api)
    monkeypatch.setattr(repo_info, "gh_token", lambda: "fake-token")
    return api


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """没准备好假 API 的用例必须炸在本地，而不是真去请求 GitHub。"""
    def refuse(*_args, **_kwargs):
        raise AssertionError("这个用例没准备假 API，别真去请求 GitHub")

    monkeypatch.setattr(repo_info, "request_json", refuse)
    monkeypatch.setattr(repo_info, "gh_token", lambda: "fake-token")


def current_state() -> tuple[str, list[str]]:
    return expected_description().strip(), expected_topics()


# ------------------------------ 推导出来的值 ------------------------------

def test_description_mentions_every_export_format():
    text = expected_description()
    for key in repo_info.EXPORTERS:
        label = repo_info.FORMAT_LABELS.get(key, key.upper())
        assert label in text


def test_unknown_format_falls_back_to_its_upper_key(monkeypatch):
    monkeypatch.setattr(repo_info, "FORMAT_LABELS", {"html": "HTML"})
    assert "PDF" in expected_description()
    assert expected_description().count("HTML") == 1


def test_topics_are_sorted_and_without_duplicates():
    topics = expected_topics()
    assert topics == sorted(topics)
    assert len(topics) == len(set(topics))


def test_topics_are_the_base_set_plus_the_format_tags():
    topics = set(expected_topics())
    assert set(repo_info.BASE_TOPICS) <= topics
    for key, tag in repo_info.FORMAT_TOPICS.items():
        if key in repo_info.EXPORTERS:
            assert tag in topics


# ------------------------------ 写之前先验 ------------------------------

def test_the_real_derived_values_pass_the_check():
    description, topics = current_state()
    assert repo_info.facade_problems(description, topics) == []


@pytest.mark.parametrize("bad", ["", "   ", "x" * 400])
def test_unusable_descriptions_are_reported(bad):
    assert repo_info.facade_problems(bad, ["xdao"]) != []


@pytest.mark.parametrize("bad", ["Hello", "with space", "-leading", "under_score", "中文"])
def test_illegal_topic_names_are_reported(bad):
    problems = repo_info.facade_problems("描述", [bad])
    assert any("不合规" in problem for problem in problems), problems


@pytest.mark.parametrize("case", ["empty", "too_many", "too_long"])
def test_unusable_topic_lists_are_reported(case):
    if case == "empty":
        topics = []
    elif case == "too_many":
        topics = [f"topic-{i}" for i in range(repo_info.TOPIC_MAX + 1)]
    else:
        topics = ["a" * (repo_info.TOPIC_NAME_MAX + 1)]
    assert repo_info.facade_problems("描述", topics) != []


def test_every_problem_is_listed_not_just_the_first(monkeypatch):
    monkeypatch.setattr(repo_info, "DESCRIPTION_TEMPLATE", "")
    monkeypatch.setattr(repo_info, "BASE_TOPICS", [])
    monkeypatch.setattr(repo_info, "FORMAT_TOPICS", {})
    problems = repo_info.facade_problems("", [])
    assert len(problems) == 2
    assert any("描述" in problem for problem in problems)
    assert any("话题" in problem for problem in problems)


def test_an_empty_description_is_refused_before_any_write(monkeypatch, capsys):
    monkeypatch.setattr(repo_info, "DESCRIPTION_TEMPLATE", "")
    api = use_api(monkeypatch, _FakeApi(description="旧的描述", topics=["old"]))
    with pytest.raises(SystemExit) as caught:
        repo_info.apply(REPO, "fake-token")
    assert caught.value.code == 1
    out = capsys.readouterr().out
    assert "一个请求都没发" in out
    assert "描述是空的" in out
    assert api.writes() == []


def test_too_many_topics_never_reach_the_api(monkeypatch, capsys):
    monkeypatch.setattr(repo_info, "BASE_TOPICS", [f"topic-{i}" for i in range(25)])
    monkeypatch.setattr(repo_info, "FORMAT_TOPICS", {})
    api = use_api(monkeypatch, _FakeApi(description=expected_description(), topics=["old"]))
    with pytest.raises(SystemExit):
        repo_info.apply(REPO, "fake-token")
    assert "话题太多" in capsys.readouterr().out
    assert api.writes() == []


def test_an_illegal_topic_never_reaches_the_api(monkeypatch, capsys):
    monkeypatch.setattr(repo_info, "BASE_TOPICS", ["xdao", "Not-A-Topic"])
    monkeypatch.setattr(repo_info, "FORMAT_TOPICS", {})
    api = use_api(monkeypatch, _FakeApi(description=expected_description(), topics=["old"]))
    with pytest.raises(SystemExit):
        repo_info.apply(REPO, "fake-token")
    assert "不合规" in capsys.readouterr().out
    assert api.writes() == []


def test_a_consistent_repo_is_never_checked_for_problems(monkeypatch):
    """已经一致时不该再挑推导值的毛病 —— 否则每次例行都会红一条无从下手的告警。"""
    monkeypatch.setattr(repo_info, "DESCRIPTION_TEMPLATE", "")
    topics = expected_topics()
    api = use_api(monkeypatch, _FakeApi(description="", topics=topics))
    assert repo_info.apply(REPO, "fake-token") == []
    assert api.writes() == []


# ------------------------------ 只检查 ------------------------------

def test_check_mode_reports_drift_and_returns_one(monkeypatch, capsys):
    api = use_api(monkeypatch, _FakeApi(description="老描述", topics=["xdao"]))
    assert repo_info.main(["--repo", REPO]) == 1
    out = capsys.readouterr().out
    assert "发现不一致" in out
    assert "! 描述" in out
    assert "加 --apply 即可同步" in out
    assert api.writes() == []


def test_check_mode_says_everything_is_current(monkeypatch, capsys):
    description, topics = current_state()
    api = use_api(monkeypatch, _FakeApi(description=description, topics=topics))
    assert repo_info.main(["--repo", REPO]) == 0
    assert "结论：描述与话题都是最新的" in capsys.readouterr().out
    assert api.writes() == []


def test_check_mode_ignores_the_order_topics_come_back_in(monkeypatch):
    description, topics = current_state()
    api = use_api(monkeypatch, _FakeApi(description=description, topics=list(reversed(topics))))
    assert repo_info.main(["--repo", REPO]) == 0
    assert api.writes() == []


def test_extra_topics_on_the_remote_are_reported_as_removals(monkeypatch, capsys):
    description, topics = current_state()
    api = use_api(monkeypatch, _FakeApi(description=description, topics=topics + ["stale-tag"]))
    assert repo_info.main(["--repo", REPO]) == 1
    out = capsys.readouterr().out
    assert "移除" in out
    assert "stale-tag" in out
    assert api.writes() == []


def test_missing_topics_alone_are_enough_to_report_drift(monkeypatch, capsys):
    description, topics = current_state()
    api = use_api(monkeypatch, _FakeApi(description=description, topics=topics[:-1]))
    assert repo_info.main(["--repo", REPO]) == 1
    assert "话题：" in capsys.readouterr().out
    assert api.writes() == []


# ------------------------------ 同步 ------------------------------

def test_apply_writes_the_description_and_the_topics(monkeypatch, capsys):
    description, topics = current_state()
    api = use_api(monkeypatch, _FakeApi(description="老描述", topics=["xdao"]))
    assert repo_info.main(["--repo", REPO, "--apply"]) == 0
    assert api.payload_of("PATCH") == {"description": description}
    assert api.payload_of("PUT") == {"names": topics}
    assert "已同步到远端（写完之后重新读回来核对过）" in capsys.readouterr().out


def test_apply_rereads_the_repo_after_writing(monkeypatch):
    api = use_api(monkeypatch, _FakeApi(description="老描述", topics=["xdao"]))
    assert repo_info.main(["--repo", REPO, "--apply"]) == 0
    methods = api.methods()
    assert methods.count("GET") >= 3  # 打印、比对、写完核对
    assert methods.index("PATCH") < methods.index("PUT")
    last_get = max(i for i, method in enumerate(methods) if method == "GET")
    assert last_get > methods.index("PUT")  # 核对发生在写完之后的重新读取里


def test_apply_does_not_write_twice_when_verifying(monkeypatch):
    api = use_api(monkeypatch, _FakeApi(description="老描述", topics=["xdao"]))
    assert repo_info.main(["--repo", REPO, "--apply"]) == 0
    assert api.methods().count("PATCH") == 1
    assert api.methods().count("PUT") == 1


def test_a_write_that_did_not_stick_is_reported_as_a_failure(monkeypatch, capsys):
    """发过请求 ≠ 改成了：远端收下但没照办时，绝不能说「已同步」。"""
    api = use_api(monkeypatch, _FakeApi(description="老描述", topics=["xdao"], obey=False))
    assert repo_info.main(["--repo", REPO, "--apply"]) == 1
    out = capsys.readouterr().out
    assert "重新读回来的还是不一样" in out
    assert "别当成同步好了" in out
    assert "已同步到远端" not in out
    assert api.writes() != []  # 请求确实发过，是核对把它拦下来的


def test_apply_with_nothing_to_do_does_not_write(monkeypatch, capsys):
    description, topics = current_state()
    api = use_api(monkeypatch, _FakeApi(description=description, topics=topics))
    assert repo_info.main(["--repo", REPO, "--apply"]) == 0
    assert "结论：描述与话题都是最新的" in capsys.readouterr().out
    assert api.writes() == []


def test_apply_refuses_to_wipe_a_repo_when_the_derived_values_are_broken(monkeypatch, capsys):
    monkeypatch.setattr(repo_info, "DESCRIPTION_TEMPLATE", "")
    api = use_api(monkeypatch, _FakeApi(description="老描述", topics=["xdao"]))
    with pytest.raises(SystemExit) as caught:
        repo_info.main(["--repo", REPO, "--apply"])
    assert caught.value.code == 1
    assert "一个请求都没发" in capsys.readouterr().out
    assert api.writes() == []


def test_trailing_spaces_on_the_remote_are_not_drift(monkeypatch):
    description, topics = current_state()
    api = use_api(monkeypatch, _FakeApi(description=description + "   ", topics=topics))
    assert repo_info.main(["--repo", REPO]) == 0
    assert api.writes() == []


def test_the_written_description_has_no_stray_whitespace(monkeypatch):
    monkeypatch.setattr(repo_info, "DESCRIPTION_TEMPLATE", repo_info.DESCRIPTION_TEMPLATE + "  ")
    api = use_api(monkeypatch, _FakeApi(description="老描述", topics=[]))
    assert repo_info.main(["--repo", REPO, "--apply"]) == 0
    assert api.payload_of("PATCH")["description"] == expected_description().strip()
    assert not api.payload_of("PATCH")["description"].endswith(" ")


# ------------------------------ 打印与入口 ------------------------------

def test_describe_falls_back_to_the_repo_argument(monkeypatch, capsys):
    api = use_api(monkeypatch, _FakeApi(description="d", topics=[], full_name=None))
    repo_info.describe(REPO, "fake-token")
    assert REPO in capsys.readouterr().out
    assert api.methods() == ["GET"]


def test_describe_says_when_nothing_is_set(monkeypatch, capsys):
    use_api(monkeypatch, _FakeApi(description="", topics=[]))
    repo_info.describe(REPO, "fake-token")
    assert capsys.readouterr().out.count("（未设置）") == 2


def test_repo_argument_is_required():
    with pytest.raises(SystemExit) as caught:
        repo_info.main([])
    assert caught.value.code == 2


def test_the_repo_argument_is_the_one_being_talked_to(monkeypatch):
    api = use_api(monkeypatch, _FakeApi(description="d", topics=["xdao"]))
    repo_info.main(["--repo", "someone/else"])
    assert api.calls[0][1] == "/repos/someone/else"
