"""一键升级（:mod:`xdao.updater`）的离线用例。

这里刻意不碰真实网络、也不碰真实用户配置：下载用替身 opener，自检用真的
小脚本跑一遍 ``subprocess``（这条值得真跑，它是「先自检再换」的关键一环），
换文件则全在 ``tmp_path`` 里搭出「现场目录 + 升级包目录」。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import zipfile
from pathlib import Path

import pytest

from xdao import updater
from xdao.update_check import UpdateCheckError


# ---------------------------------------------------------------- 搭场景


def make_live_dir(root: Path, *, name: str = "xdao-export-v0.12.0-win64") -> Path:
    """搭一个「用户装好的程序目录」。"""
    live = root / name
    live.mkdir(parents=True)
    (live / updater.LAUNCHER_NAME).write_text("old exe", encoding="utf-8")
    (live / "_internal").mkdir()
    (live / "_internal" / "python312.dll").write_text("old dll", encoding="utf-8")
    return live


def make_payload_dir(root: Path, *, name: str = "xdao-export-v0.13.0-win64") -> Path:
    """搭一个「升级包解出来的新版本目录」。"""
    payload = root / "payload" / name
    payload.mkdir(parents=True)
    (payload / updater.LAUNCHER_NAME).write_text("new exe", encoding="utf-8")
    (payload / "_internal").mkdir()
    (payload / "_internal" / "python312.dll").write_text("new dll", encoding="utf-8")
    return payload


def make_zip(root: Path, *, top: str = "xdao-export-v0.13.0-win64", extra: list[str] | None = None) -> Path:
    """做一个结构正确的升级包（可以塞额外的坏路径进去）。"""
    archive = root / "update.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(f"{top}/xdao-export.exe", "new exe")
        zipped.writestr(f"{top}/_internal/python312.dll", "new dll")
        zipped.writestr(f"{top}/使用说明.txt", "说明")
        for name in extra or []:
            zipped.writestr(name, "坏东西")
    return archive


class _FakeResponse:
    def __init__(self, chunks: list[bytes], length: int | None = None) -> None:
        self._chunks = list(chunks)
        self.headers = {
            "Content-Length": str(length if length is not None else sum(map(len, chunks)))
        }

    def read(self, size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        if size is None or size < 0:
            blob = b"".join(self._chunks)
            self._chunks.clear()
            return blob
        blob = self._chunks.pop(0)
        return blob

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def fake_opener(response) -> object:
    """做一个 build_opener 的替身，返回的对象带 open()。"""

    class _Opener:
        def open(self, request, timeout=None):
            if isinstance(response, Exception):
                raise response
            return response

    def _build(*handlers):
        return _Opener()

    return _build


# ---------------------------------------------------------------- 下载


def test_download_asset_writes_the_file_and_reports_progress(tmp_path: Path) -> None:
    target = tmp_path / "update.zip"
    seen: list[tuple[int, int]] = []
    build = fake_opener(_FakeResponse([b"a" * 10, b"b" * 20]))

    updater.download_asset(
        "https://example.invalid/pkg.zip",
        target,
        opener_factory=build,
        progress=lambda written, total: seen.append((written, total)),
    )

    assert target.read_bytes() == b"a" * 10 + b"b" * 20
    assert seen[-1] == (30, 30)
    assert [written for written, _ in seen] == [10, 30]


def test_download_asset_cleans_up_when_the_network_dies(tmp_path: Path) -> None:
    target = tmp_path / "update.zip"
    build = fake_opener(urllib.error.URLError("连不上"))

    with pytest.raises(UpdateCheckError):
        updater.download_asset("https://example.invalid/pkg.zip", target, opener_factory=build)

    assert not target.exists(), "下载失败要在磁盘上留下半个文件"


def test_download_asset_refuses_without_a_url(tmp_path: Path) -> None:
    with pytest.raises(UpdateCheckError):
        updater.download_asset("", tmp_path / "update.zip")


def test_download_asset_stops_on_an_absurdly_large_package(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(updater, "MAX_DOWNLOAD_BYTES", 5)
    target = tmp_path / "update.zip"
    build = fake_opener(_FakeResponse([b"x" * 10]))

    with pytest.raises(UpdateCheckError):
        updater.download_asset("https://example.invalid/pkg.zip", target, opener_factory=build)

    assert not target.exists()


# ---------------------------------------------------------------- 解压


def test_extract_payload_returns_the_program_directory(tmp_path: Path) -> None:
    archive = make_zip(tmp_path)

    payload = updater.extract_payload(archive, tmp_path / "out")

    assert payload.name == "xdao-export-v0.13.0-win64"
    assert (payload / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "new exe"
    assert (payload / "使用说明.txt").exists()


def test_extract_payload_rejects_a_zip_without_our_folder(tmp_path: Path) -> None:
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("别的项目/readme.txt", "hi")

    with pytest.raises(UpdateCheckError):
        updater.extract_payload(archive, tmp_path / "out")


def test_extract_payload_rejects_paths_that_climb_out(tmp_path: Path) -> None:
    archive = make_zip(tmp_path, extra=["xdao-export-v0.13.0-win64/../坏.exe"])

    with pytest.raises(UpdateCheckError):
        updater.extract_payload(archive, tmp_path / "out")


def test_extract_payload_rejects_a_broken_zip(tmp_path: Path) -> None:
    archive = tmp_path / "update.zip"
    archive.write_bytes(b"this is not a zip at all")

    with pytest.raises(UpdateCheckError):
        updater.extract_payload(archive, tmp_path / "out")


# ---------------------------------------------------------------- 自检


def _fake_exe(tmp_path: Path, body: str, name: str = "xdao-export.exe") -> Path:
    """造一个「假装是 exe」的文件。

    真跑的时候在它前面加一个 python 前缀（见 ``_run``）：这样它是一个真的
    能被启动、能按自己的退出码退出的进程，而不是「文件里有几行字」。
    """
    exe = tmp_path / name
    exe.write_text(body, encoding="utf-8")
    return exe


def _run(exe: Path, **kwargs):
    """用 Python 解释器跑那个假 exe（参数照原样传进去）。"""
    return updater.run_selftest(exe, prefix=[sys.executable], **kwargs)


def test_run_selftest_accepts_a_healthy_candidate(tmp_path: Path) -> None:
    exe = _fake_exe(
        tmp_path,
        "import sys\nprint('本机自检全部通过。')\nsys.exit(0)\n",
    )

    passed, detail = _run(exe, timeout=60)

    assert passed is True
    assert "通过" in detail


def test_run_selftest_reports_the_last_line_when_it_fails(tmp_path: Path) -> None:
    exe = _fake_exe(
        tmp_path,
        "import sys\nprint('配置目录  [不行]')\nprint('有 1 项没过。')\nsys.exit(1)\n",
    )

    passed, detail = _run(exe, timeout=60)

    assert passed is False
    assert "有 1 项没过。" in detail


def test_run_selftest_passes_the_two_flags_the_candidate_expects(
    tmp_path: Path,
) -> None:
    """参数写错（少个 --offline 之类）会让候选去联网，这里钉住这两个开关。"""
    exe = _fake_exe(
        tmp_path,
        "import sys\n"
        "ok = sys.argv[1:] == ['--selftest', '--offline']\n"
        "print('flags ok' if ok else 'flags bad: %r' % (sys.argv[1:],))\n"
        "sys.exit(0 if ok else 3)\n",
    )

    passed, detail = _run(exe, timeout=60)

    assert passed is True
    assert "通过" in detail


def test_run_selftest_fails_when_there_is_no_candidate(tmp_path: Path) -> None:
    passed, detail = updater.run_selftest(tmp_path / "没有这个文件.exe")

    assert passed is False
    assert "没有" in detail


# ---------------------------------------------------------------- 能不能升


def test_plan_upgrade_says_no_when_running_from_source(tmp_path: Path) -> None:
    plan = updater.plan_upgrade(
        target=make_live_dir(tmp_path), frozen=False, writable=lambda path: True
    )

    assert plan.possible is False
    assert "源码" in plan.reason


def test_plan_upgrade_says_no_when_the_folder_has_no_exe(tmp_path: Path) -> None:
    empty = tmp_path / "xdao-export-v0.12.0-win64"
    empty.mkdir()

    plan = updater.plan_upgrade(target=empty, frozen=True, writable=lambda path: True)

    assert plan.possible is False
    assert updater.LAUNCHER_NAME in plan.reason


def test_plan_upgrade_says_no_when_the_parent_is_read_only(tmp_path: Path) -> None:
    live = make_live_dir(tmp_path)

    plan = updater.plan_upgrade(
        target=live, frozen=True, writable=lambda path: path != live.parent
    )

    assert plan.possible is False
    assert "上级目录" in plan.reason


def test_plan_upgrade_is_happy_for_a_normal_install(tmp_path: Path) -> None:
    live = make_live_dir(tmp_path)

    plan = updater.plan_upgrade(target=live, frozen=True, writable=lambda path: True)

    assert plan.possible is True
    assert plan.target == live
    assert plan.launcher == live / updater.LAUNCHER_NAME
    assert plan.pid == os.getpid()


def test_backup_dir_keeps_the_old_name_and_adds_a_stamp(tmp_path: Path) -> None:
    live = make_live_dir(tmp_path)

    plan = updater.plan_upgrade(target=live, frozen=True, writable=lambda path: True)

    back = plan.backup_dir
    assert back.parent == live.parent
    assert back.name.startswith(live.name + updater.BACKUP_SUFFIX)


# ---------------------------------------------------------------- 换文件


def test_swap_in_moves_the_old_install_aside_and_puts_the_new_one_in(
    tmp_path: Path,
) -> None:
    live = make_live_dir(tmp_path)
    payload = make_payload_dir(tmp_path)
    plan = updater.plan_upgrade(target=live, frozen=True, writable=lambda path: True)

    backup = updater.swap_in(payload, plan)

    assert (live / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "new exe"
    assert (live / "_internal" / "python312.dll").read_text(encoding="utf-8") == "new dll"
    assert (backup / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "old exe"
    assert not payload.exists()


def test_swap_in_refuses_a_plan_that_was_never_possible(tmp_path: Path) -> None:
    live = make_live_dir(tmp_path)
    payload = make_payload_dir(tmp_path)
    plan = updater.plan_upgrade(target=live, frozen=False, writable=lambda path: True)

    with pytest.raises(UpdateCheckError):
        updater.swap_in(payload, plan)

    assert (live / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "old exe"


# ---------------------------------------------------------------- 帮手


def test_spawn_helper_hands_over_four_arguments(tmp_path: Path) -> None:
    live = make_live_dir(tmp_path)
    payload = make_payload_dir(tmp_path)
    plan = updater.plan_upgrade(target=live, frozen=True, writable=lambda path: True)
    calls: list[list[str]] = []

    class _Fake:
        pass

    def popen(args, **kwargs):
        calls.append(list(args))
        return _Fake()

    updater.spawn_helper(plan, payload, popen=popen)

    args = calls[0]
    assert args[0] == str(payload / updater.LAUNCHER_NAME), "帮手用升级包里的 exe 最稳"
    assert args[1] == "--apply-update"
    assert args[2] == str(live)
    assert args[3] == str(payload)
    assert args[4] == str(os.getpid())
    assert args[5] == str(live / updater.LAUNCHER_NAME)


def test_spawn_helper_leaves_a_log_next_to_the_staging_dir(tmp_path: Path) -> None:
    """帮手的输出要有地方看：它是窗口程序、没有控制台，print 等于扔了。"""
    live = make_live_dir(tmp_path)
    payload = make_payload_dir(tmp_path)
    plan = updater.plan_upgrade(target=live, frozen=True, writable=lambda path: True)
    captured: dict[str, object] = {}

    class _Fake:
        pass

    def popen(args, **kwargs):
        captured.update(kwargs)
        return _Fake()

    updater.spawn_helper(plan, payload, popen=popen)

    assert captured.get("stdout") is not None, "帮手的输出得接到文件上"
    assert captured.get("stderr") is captured.get("stdout"), "两股都进同一个日志"
    assert (tmp_path / "xdao-update.log").exists(), "日志放在暂存目录旁边"


def test_spawn_helper_complains_when_the_package_has_no_exe(tmp_path: Path) -> None:
    live = make_live_dir(tmp_path)
    payload = make_payload_dir(tmp_path)
    (payload / updater.LAUNCHER_NAME).unlink()
    plan = updater.plan_upgrade(target=live, frozen=True, writable=lambda path: True)

    with pytest.raises(UpdateCheckError):
        updater.spawn_helper(plan, payload)


def make_staged_payload(root: Path, *, name: str = "xdao-export-v0.13.0-win64") -> Path:
    """搭一个「解压到暂存目录里」的新版本（真机上就是 ``%TEMP%/xdao-export-update``）。

    和 :func:`make_payload_dir` 的区别只在路径：``apply_update`` 成功后会
    给**暂存目录**改名留记号，所以这里必须先摆出那个真名字。
    """
    bundle = root / updater.STAGING_DIR_NAME
    payload = bundle / name
    payload.mkdir(parents=True)
    (payload / updater.LAUNCHER_NAME).write_text("new exe", encoding="utf-8")
    (payload / "_internal").mkdir()
    (payload / "_internal" / "python312.dll").write_text("new dll", encoding="utf-8")
    return payload


def test_apply_update_waits_then_swaps_and_launches(
    tmp_path: Path, monkeypatch
) -> None:
    live = make_live_dir(tmp_path)
    payload = make_staged_payload(tmp_path)
    bundle = payload.parent
    launched: list[list[str]] = []
    waited: list[int] = []

    def spawn(args, **kwargs):
        launched.append(list(args))

    def fake_wait(pid, timeout=0):
        # 真等的话会等自己（传进来的就是本进程号），60 秒起步 —— 用例里钉住。
        waited.append(pid)
        return True

    monkeypatch.setattr(updater, "wait_for_exit", fake_wait)

    code = updater.apply_update(
        live,
        payload,
        os.getpid(),
        live / updater.LAUNCHER_NAME,
        spawn=spawn,
    )

    assert code == 0
    assert waited == [os.getpid()], "要等的是老进程"
    assert (live / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "new exe"
    assert launched == [[str(live / updater.LAUNCHER_NAME)]]
    backups = [p for p in tmp_path.iterdir() if updater.BACKUP_SUFFIX in p.name]
    assert len(backups) == 1
    assert (backups[0] / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "old exe"
    # 帮手这时正住在暂存目录里（它跑的就是升级包里的 exe），Windows 删不掉自己，
    # 所以必须**改名留记号**走人，让新版下次启动来收 —— 不能原地硬留。
    assert not bundle.exists(), "换了目录之后不该还留着 xdao-export-update"
    markers = [p for p in tmp_path.iterdir() if updater.LEFTOVER_SUFFIX in p.name]
    assert len(markers) == 1, f"应当留下一个记号目录，实际 {markers}"
    # 记号里就是刚才那个暂存目录的空壳：`shutil.move` 把整个包目录搬去现场了，
    # 所以这里只剩空的暂存目录 —— 关键是「它还叫那个名字、还在那个位置」，
    # 新版下一次启动照记号就能收掉。
    assert markers[0].is_dir()
    assert list(markers[0].iterdir()) == [], "包已经搬走了，记号里只该剩空壳"
    assert (live / "_internal" / "python312.dll").read_text(encoding="utf-8") == "new dll"


def test_apply_update_leaves_everything_alone_when_the_old_process_stays(
    tmp_path: Path, monkeypatch
) -> None:
    live = make_live_dir(tmp_path)
    payload = make_payload_dir(tmp_path)
    monkeypatch.setattr(updater, "wait_for_exit", lambda pid, timeout=0: False)

    code = updater.apply_update(live, payload, 4242, live / updater.LAUNCHER_NAME)

    assert code == 1
    assert (live / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "old exe"
    assert (payload / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "new exe"
    assert not [p for p in tmp_path.iterdir() if updater.BACKUP_SUFFIX in p.name]


def test_apply_update_gives_up_when_the_package_vanished(
    tmp_path: Path, monkeypatch
) -> None:
    live = make_live_dir(tmp_path)
    payload = make_payload_dir(tmp_path)
    monkeypatch.setattr(updater, "wait_for_exit", lambda pid, timeout=0: True)
    import shutil

    shutil.rmtree(payload)

    code = updater.apply_update(live, payload, os.getpid(), live / updater.LAUNCHER_NAME)

    assert code == 1
    assert (live / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "old exe"


def test_apply_update_says_so_when_the_new_version_will_not_start(
    tmp_path: Path, monkeypatch
) -> None:
    live = make_live_dir(tmp_path)
    payload = make_payload_dir(tmp_path)
    monkeypatch.setattr(updater, "wait_for_exit", lambda pid, timeout=0: True)

    def spawn(args, **kwargs):
        raise OSError("起不来")

    code = updater.apply_update(
        live, payload, os.getpid(), live / updater.LAUNCHER_NAME, spawn=spawn
    )

    assert code == 1, "换了但没启动起来要如实报错"
    assert (live / updater.LAUNCHER_NAME).read_text(encoding="utf-8") == "new exe"


# ---------------------------------------------------------------- 收尾


def test_cleanup_backups_removes_only_old_leftovers(tmp_path: Path) -> None:
    live = make_live_dir(tmp_path)
    fresh = tmp_path / f"{live.name}{updater.BACKUP_SUFFIX}20261001-090000"
    stale = tmp_path / f"{live.name}{updater.BACKUP_SUFFIX}20260901-090000"
    unrelated = tmp_path / "别动我"
    for path in (fresh, stale, unrelated):
        path.mkdir()
        (path / "x").write_text("x", encoding="utf-8")
    old = time.time() - updater.BACKUP_KEEP_SECONDS - 60
    os.utime(stale, (old, old))

    removed = updater.cleanup_backups(live)

    assert removed == [stale]
    assert fresh.exists(), "刚换下来的备份要留着，用户还能回退"
    assert unrelated.exists()


def test_cleanup_backups_survives_a_missing_parent(tmp_path: Path) -> None:
    assert updater.cleanup_backups(tmp_path / "没有这个目录") == []


def test_staging_cleanup_leaves_a_marker_when_it_cannot_delete(
    tmp_path: Path, monkeypatch
) -> None:
    """帮手住在暂存目录里，删不掉自己的时候要改名留记号，别硬留原地。"""
    staging = tmp_path / updater.STAGING_DIR_NAME
    payload = staging / "payload"
    payload.mkdir(parents=True)
    monkeypatch.setattr(updater, "_remove_tree", lambda path, wait=0.0: False)

    updater._cleanup_staging(payload)

    left = [p.name for p in tmp_path.iterdir()]
    assert len(left) == 1, f"暂存目录应当只剩一个记号目录，实际 {left}"
    assert left[0].startswith(f"{updater.STAGING_DIR_NAME}{updater.LEFTOVER_SUFFIX}")


def test_staging_leftovers_removes_old_ones_and_keeps_fresh(tmp_path: Path) -> None:
    fresh = tmp_path / updater.STAGING_DIR_NAME
    stale = tmp_path / f"{updater.STAGING_DIR_NAME}{updater.LEFTOVER_SUFFIX}1790000000"
    unrelated = tmp_path / "用户自己的东西"
    for path in (fresh, stale, unrelated):
        path.mkdir()
        (path / "x").write_text("x", encoding="utf-8")
    old = time.time() - updater.STAGING_STALE_SECONDS - 60
    os.utime(stale, (old, old))

    removed = updater.cleanup_staging_leftovers(tmp_path / "tmp")

    assert removed == 1, "只该清掉那个明显放旧了的记号目录"
    assert fresh.exists(), "正在进行的升级不能被误删"
    assert unrelated.exists()
    assert not stale.exists()


def test_staging_cleanup_is_a_noop_for_other_directories(tmp_path: Path) -> None:
    """别的地方的同名 payload 不能被误删（只认暂存目录这一层）。"""
    payload = tmp_path / "别处的payload"
    payload.mkdir()

    updater._cleanup_staging(payload)

    assert payload.exists()


def test_remove_quietly_ignores_a_missing_file(tmp_path: Path) -> None:
    updater.remove_quietly(tmp_path / "根本没有这个文件.zip")


def test_retire_staging_renames_without_trying_to_delete(tmp_path: Path, monkeypatch) -> None:
    """换完目录那一刻，帮手住在暂存目录里 —— 不是「删不掉再说」，而是根本不试删。"""
    staging = tmp_path / updater.STAGING_DIR_NAME
    (staging / "payload").mkdir(parents=True)

    def boom(*args, **kwargs):
        raise AssertionError("住在里面的时候不该白费力气去删")

    monkeypatch.setattr(updater, "_remove_tree", boom)

    marker = updater._retire_staging(staging)

    assert marker is not None and marker.exists()
    assert marker.name.startswith(f"{updater.STAGING_DIR_NAME}{updater.LEFTOVER_SUFFIX}")
    assert (marker / "payload").is_dir(), "改名不该动里面的东西"
    # 不叫暂存目录的名字了就不碰（免得误删别处同名目录）
    assert updater._retire_staging(tmp_path / "别的东西") is None


def test_startup_cleanup_keeps_trying_until_the_helper_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    """新版刚起来的那几秒帮手往往还在，一次删不掉不能就此认输。

    实测（真机升级）：第一次试删必然失败（帮手住在那目录里），于是用户
    ``%TEMP%`` 里会一直躺着几十 MB —— 所以要隔一会儿再试，帮手一走立刻收。
    """
    # 两个记号目录，名字里都是「早就没了的进程号」：按规矩都能立刻清。
    first = tmp_path / f"{updater.STAGING_DIR_NAME}{updater.LEFTOVER_SUFFIX}1790000001-100"
    second = tmp_path / f"{updater.STAGING_DIR_NAME}{updater.LEFTOVER_SUFFIX}1790000002-100"
    for path in (first, second):
        path.mkdir()
        (path / "x").write_text("x", encoding="utf-8")
    monkeypatch.setattr(updater, "_pid_running", lambda pid: False)

    attempts: list[int] = []
    real_remove = updater._remove_tree

    def flaky(path, wait=0.0):
        attempts.append(1)
        if len(attempts) == 1:
            return False  # 第一次：帮手还占着
        return real_remove(path, wait)

    slept: list[float] = []
    monkeypatch.setattr(updater, "_remove_tree", flaky)

    removed = updater.cleanup_staging_leftovers_at_startup(
        tmp_path / "tmp", gap=0.01, sleep=slept.append
    )

    assert removed == 2, "两次清干净，才算这两轮没白等"
    assert not first.exists() and not second.exists()
    assert len(attempts) == 3, "第一轮 1 次失败 + 成功 1 次，第二轮再成功 1 次"
    assert 0.01 in slept, "第一轮没清完，要睡一会儿再试"


def test_pid_running_tells_live_processes_from_dead_ones() -> None:
    """Windows 上 ``os.kill(pid, 0)`` 对已退出的进程号也照样返回，
    这里必须用 ``OpenProcess`` 判活，否则「帮手走了没有」永远问不出来。"""
    assert updater._pid_running(0) is False
    assert updater._pid_running(2_000_000_000) is False, "这个进程号不可能有人用"

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert updater._pid_running(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert updater._pid_running(proc.pid) is False, "退出了就该说没了"


def test_wait_for_exit_returns_true_for_a_pid_that_is_already_gone() -> None:
    # 一个几乎不可能存在的进程号（Windows 上也不会有）
    assert updater.wait_for_exit(999_999_998, timeout=1.0) is True


def test_wait_for_exit_sees_a_live_process() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert updater.wait_for_exit(proc.pid, timeout=0.6) is False
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_is_frozen_is_false_when_running_from_source() -> None:
    assert updater.is_frozen() is False
    assert updater.app_dir() == Path(updater.__file__).resolve().parent.parent


# ---------------------------------------------------------------- 能不能写
#
# 覆盖率体检（2026-10-18）：``can_write_dir`` 一行都没走过。它的结论决定「能不能
# 原地升级」，判断错一次的代价是升级到一半写不进去。三条分支各钉一条。


def test_can_write_dir_returns_true_and_leaves_no_probe(tmp_path: Path) -> None:
    """能写就返回真，并且把探针文件删掉（别在用户目录里留垃圾）。"""
    assert updater.can_write_dir(tmp_path) is True
    assert list(tmp_path.glob(".xdao-write-probe-*")) == []


def test_can_write_dir_returns_false_when_the_probe_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读介质、系统目录：写探针会抛 OSError，只能返回假。"""

    def refuse(self, *args, **kwargs):  # noqa: ANN001
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(Path, "write_text", refuse)

    assert updater.can_write_dir(tmp_path) is False


def test_can_write_dir_returns_true_even_when_the_probe_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探针删不掉（被占用、权限怪）不算「不能写」：写得进去才是结论。"""

    def refuse(self, *args, **kwargs):  # noqa: ANN001
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(Path, "unlink", refuse)

    assert updater.can_write_dir(tmp_path) is True


def test_can_write_dir_returns_false_for_a_missing_directory(tmp_path: Path) -> None:
    """目录本身都不存在时写探针会报「找不到路径」，同样返回假。"""
    assert updater.can_write_dir(tmp_path / "还没有这个目录") is False


# ---------- 覆盖率体检补的分支（v0.19.6）----------


def test_app_dir_points_at_the_exe_when_frozen(tmp_path: Path, monkeypatch) -> None:
    """打包版里「程序目录」是 exe 所在目录，不是源码树。"""
    exe = tmp_path / updater.LAUNCHER_NAME
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(exe))

    assert updater.app_dir() == tmp_path.resolve()
    assert updater.launcher_path() == tmp_path.resolve() / updater.LAUNCHER_NAME


def test_launcher_path_sits_next_to_the_program(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(updater, "app_dir", lambda: tmp_path)

    assert updater.launcher_path() == tmp_path / updater.LAUNCHER_NAME


@pytest.mark.parametrize("size,expected", [(0, "大小未知"), (-3, "大小未知"), (5 * 1024 * 1024, "5.0 MB")])
def test_download_size_labels_what_it_knows(size: int, expected: str) -> None:
    assert updater._download_size(size) == expected


def test_extract_payload_wraps_an_oserror_from_the_zip_reader(tmp_path: Path, monkeypatch) -> None:
    """解压时被占住（杀毒软件、资源管理器）要变成一句人话，而不是抛 OSError。"""

    class _StuckZip:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

        def namelist(self) -> list[str]:
            return [f"{updater.PAYLOAD_PREFIX}1.0.0/xdao-export.exe"]

        def extract(self, *_args) -> None:
            raise OSError("文件被占用")

    archive = tmp_path / "package.zip"
    archive.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(updater.zipfile, "ZipFile", _StuckZip)

    with pytest.raises(UpdateCheckError) as caught:
        updater.extract_payload(archive, tmp_path / "staging")

    assert "解压失败" in str(caught.value)


def test_cleanup_backups_keeps_going_when_a_directory_cannot_be_removed(
    tmp_path: Path, monkeypatch
) -> None:
    install = tmp_path / "xdao-export-v1.0.0"
    install.mkdir()
    old = tmp_path / f"{install.name}{updater.BACKUP_SUFFIX}20200101-000000"
    old.mkdir()
    stamp = time.time() - updater.BACKUP_KEEP_SECONDS - 60
    os.utime(old, (stamp, stamp))

    def refuse(_path) -> None:
        raise OSError("被占用")

    monkeypatch.setattr(updater.shutil, "rmtree", refuse)

    assert updater.cleanup_backups(install, keep_seconds=updater.BACKUP_KEEP_SECONDS) == []
    assert old.exists()


def test_cleanup_backups_returns_nothing_when_the_parent_is_gone(tmp_path: Path) -> None:
    gone = tmp_path / "gone" / "xdao-export-v1.0.0"

    assert updater.cleanup_backups(gone) == []


@pytest.mark.parametrize("pid", [0, -1])
def test_pid_running_is_false_for_non_positive_numbers(pid: int) -> None:
    assert updater._pid_running(pid) is False


def test_pid_running_uses_os_kill_where_there_is_no_win32(monkeypatch) -> None:
    """非 Windows 上退回 os.kill(pid, 0)：三种 errno 要分别对待。"""
    monkeypatch.setattr(updater.os, "name", "posix")

    monkeypatch.setattr(updater.os, "kill", lambda *_a: None)
    assert updater._pid_running(4321) is True

    def gone(*_a):
        raise ProcessLookupError("没有这个进程")

    monkeypatch.setattr(updater.os, "kill", gone)
    assert updater._pid_running(4321) is False

    def denied(*_a):
        raise PermissionError("没权限")

    monkeypatch.setattr(updater.os, "kill", denied)
    assert updater._pid_running(4321) is True

    def broken(*_a):
        raise OSError("别的错")

    monkeypatch.setattr(updater.os, "kill", broken)
    assert updater._pid_running(4321) is False


def test_swap_in_puts_the_old_folder_back_when_the_new_one_cannot_move(
    tmp_path: Path, monkeypatch
) -> None:
    """换不动就把名字改回去 —— 现场不能停在「旧的没了、新的没来」。"""
    target = tmp_path / "xdao-export-v1.0.0"
    target.mkdir()
    (target / updater.LAUNCHER_NAME).write_bytes(b"MZ")
    payload = tmp_path / "staging" / "xdao-export-v2.0.0"
    payload.mkdir(parents=True)
    plan = updater.UpgradePlan(True, "", target, target / updater.LAUNCHER_NAME, os.getpid())

    def refuse(*_args, **_kwargs):
        raise OSError("搬不过去")

    monkeypatch.setattr(updater.shutil, "move", refuse)

    with pytest.raises(OSError):
        updater.swap_in(payload, plan)

    assert target.is_dir()
    assert (target / updater.LAUNCHER_NAME).exists()
    assert not plan.backup_dir.exists()


def test_discard_staging_leaves_directories_that_are_not_ours(tmp_path: Path) -> None:
    other = tmp_path / "别人的目录"
    other.mkdir()

    updater.discard_staging(other)

    assert other.is_dir()


def test_retire_staging_refuses_a_directory_that_is_not_ours(tmp_path: Path) -> None:
    other = tmp_path / "别人的目录"
    other.mkdir()

    assert updater._retire_staging(other) is None
    assert other.is_dir()


def test_spawn_helper_still_launches_when_the_log_cannot_be_opened(
    tmp_path: Path, monkeypatch
) -> None:
    """日志写不成也照样升级 —— 别为了留痕把正事耽误了。"""
    payload = tmp_path / "staging" / "xdao-export-v2.0.0"
    payload.mkdir(parents=True)
    exe = payload / updater.LAUNCHER_NAME
    exe.write_bytes(b"MZ")
    target = tmp_path / "xdao-export-v1.0.0"
    target.mkdir()
    plan = updater.UpgradePlan(True, "", target, target / updater.LAUNCHER_NAME, os.getpid())
    calls: list[tuple[tuple, dict]] = []

    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        return "帮手"

    real_open = open

    def picky_open(file, *args, **kwargs):
        if str(file).endswith("xdao-update.log"):
            raise OSError("不让写")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", picky_open)

    assert updater.spawn_helper(plan, payload, exe=exe, popen=popen) == "帮手"
    assert "stdout" not in calls[0][1]


def test_cleanup_staging_leftovers_is_zero_when_the_parent_is_missing(tmp_path: Path) -> None:
    assert updater.cleanup_staging_leftovers(tmp_path / "gone" / updater.STAGING_DIR_NAME) == 0


def test_remove_tree_reports_failure_after_the_wait_expires(tmp_path: Path, monkeypatch) -> None:
    """删不掉就返回 False（调用方会改名留记号），别假装删掉了。"""
    parked = tmp_path / "parked"
    parked.mkdir()
    monkeypatch.setattr(updater.shutil, "rmtree", lambda *_a, **_k: None)

    assert updater._remove_tree(parked, wait=0.0) is False


def test_staging_leftovers_are_removed_at_once_once_the_helper_is_gone(tmp_path: Path) -> None:
    """记号目录名字里记着帮手进程号：那个进程没了就能立刻删，不用等时辰。"""
    dead_pid = 999999
    leftover = tmp_path / f"{updater.STAGING_DIR_NAME}{updater.LEFTOVER_SUFFIX}{dead_pid}-1"
    leftover.mkdir()
    (leftover / "junk.bin").write_bytes(b"x")

    removed = updater.cleanup_staging_leftovers(tmp_path / updater.STAGING_DIR_NAME)

    assert removed == 1
    assert not leftover.exists()
