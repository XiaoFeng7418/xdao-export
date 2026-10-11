"""公开材料里不许出现真实串号、本机个人目录、凭据，也不许把用户写成来源。

规则写在 ``MAINTENANCE.md`` 的「不许写什么」一节（2026-10-01 收紧的那一版）：
例子一律用 X岛官方的测试串 ``50000001``，或者明显编出来的 ``12345678``；
自己的目录写成 ``<工作目录>``、``<本机 Python>`` 这样的占位符；来源写成客观事实
（「2026-10-04 真机复验」这种），不写「按你……要的」「用户报过……」。以前靠人肉复查，
2026-10-02 起改成机器把关：谁把真串号、本机路径，或把用户当来源的句子抄进文档、
测试、发布说明里，CI 直接红，红的位置和改法都打在失败信息里。
（第二人称那几条是 2026-10-11 补的：v0.13.49 的发布说明里漏了一句「按你 2026-10-07
要的那份……」，线上正文照着它发出去了，事后用 ``gh release edit`` 才改回来。
同一天又补了一道只管 ``docs/RELEASE_NOTES_*.md`` 与 ``packaging/使用说明.txt`` 的：
v0.13.49/50/51 三版正文里还留着「你复验了一次」「你贴回来的那三行」「把那个文件也发我」
这类句子 —— 第二人称那几条拦不住它们，而 Release 正文与包里的说明是给所有读者看的，
谁验的、验过几次、下次贴什么，都不该写进去。）

被扫的是 ``git ls-files`` 那批文件 —— 那就是「公开材料」的定义（仓库外还有
GitHub 的 Release 正文与标签信息，那两处得另外扫，做法见 ``MAINTENANCE.md``）。

这个文件自己会写下这些禁词（禁词清单就是规则本身），所以扫到自己时跳过。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 这个文件自己：它写着禁词，扫自己只会自己撞自己。
SELF = "tests/test_public_material.py"

#: 允许出现的串号：X岛官方的测试串、岛方公开的使用指南与免责声明，以及编出来的占位编号。
ALLOWED_THREADS = frozenset({"50000001", "50577215", "11689471", "12345678"})

#: ``C:\Users\<这里>`` 允许的写法：占位符或明显编的。
ALLOWED_USER_DIRS = frozenset({"你", "<你", "...", "me"})

#: 不许出现的本机字样：工作目录名、解释器目录名、本机用户名。
FORBIDDEN_SUBSTRINGS = ("小玩意", "python3129", "14515")

#: 不许把用户写成来源、或用第二人称把「要求 / 报的 / 反馈」记进公开材料。
#: 规矩与 FORBIDDEN_SUBSTRINGS 同一节（「不许写什么」）：仓库是公开的，读者不需要知道
#: 哪句话是谁说的。凡是引述这条规矩本身的句子（「有用户问……」这类样本）都不在禁列 ——
#: 禁的是真的把用户当来源写出来，例如「按你 2026-10-07 要的那份报告」。
#: ``按你(?!设)``：放行 README 与说明里「下次启动仍按你设的位置试」这种对读者的称呼。
FORBIDDEN_ADDRESS = re.compile(
    "|".join(
        [
            r"按你(?!设)",
            r"你说",
            r"你要求",
            r"你报",
            r"你反馈",
            r"你提到",
            r"你反映",
            r"你提的",
            r"你发的",
            r"你给的",
            r"你要的",
            r"你要我",
            r"你的问题",
        ]
    )
)

#: 发布说明与打包里的使用说明额外禁的一类：把「某一位用户的经历」写进正文（复验了几次、
#: 贴回来的日志、下次再贴什么、发我）。第二条规矩（``FORBIDDEN_ADDRESS``）拦不住这些 ——
#: 「你复验了一次」既不是「按你……要的」，也没写「用户报过」，但同样是用户自己的事。
#: 2026-10-11 翻 v0.13.49/50/51 三版 Release 正文时逮到一批，于是单独加这一道。
RELEASE_NOTES = re.compile(r"^docs/RELEASE_NOTES_v[0-9.]+\.md$")
USER_STORY_FILES = re.compile(r"^(?:docs/RELEASE_NOTES_v[0-9.]+\.md|packaging/使用说明\.txt)$")
FORBIDDEN_USER_STORY = re.compile(
    "|".join(
        [
            r"你(?:上次|那次|那两|那三|复验|验证|贴|发|截|手上|多半)",
            r"发我",
            r"我这次",
            r"等我(?!们)",
            r"下次[^。\n]{0,8}复验",
        ]
    )
)

#: 只扫发布说明的一条：使用说明里「这取决于你把文件放在哪儿」是对读者的正常称呼，
#: 而发布说明是给所有读者看的项目文档，那里出现「你把……」就是把用户的事又写进去了。
FORBIDDEN_USER_STORY_EXTRA = re.compile(r"你把")

#: ``userhash=`` 后面这种 16 位小写十六进制看着就像真饼干；只放行经典假值。
ALLOWED_USERHASHES = frozenset({"deadbeefdeadbeef"})

#: 允许出现的邮箱：GitHub 的 noreply 地址，以及 example.* 这种示例域。
ALLOWED_EMAIL_DOMAINS = frozenset({"users.noreply.github.com", "example.com", "example.org", "example.net", "example.invalid"})

#: 一个 8 位十进制数（前后都不是单词字符：``0x80000003``、``2147483651`` 这类不算）。
BARE_ID = re.compile(r"(?<![0-9A-Za-z_])(\d{8})(?![0-9A-Za-z_])")

#: ``YYYYMMDD`` 这种日期（文档里记「备份目录带日期」时会写）。
DATE = re.compile(r"20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")

#: 串网址与 ``No.`` 后面的编号：4–9 位都看，用来挡住「7 位的真串号」这种漏网的。
THREAD_IN_TEXT = re.compile(r"nmbxd1\.com/t/(\d{4,9})|(?<![\w.])No\.(\d{4,9})")

#: 各种凭据的形状。
TOKEN = re.compile(r"ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|Bearer\s+[A-Za-z0-9._\-]{20,}")
USERHASH = re.compile(r"userhash=([0-9a-f]{16})(?![0-9a-f])")
EMAIL = re.compile(r"[\w.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
USER_DIR = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}([^\\/\s\"'|)>,;]+)")


def _obviously_fake(value: str) -> bool:
    """一眼假的编号：测试自用的 4 位（7001/1002），以及 7 位里 7、8 开头的（7001234、8000000）。"""
    return len(value) <= 4 or (len(value) == 7 and value[0] in "78")


def _tracked_text_files() -> list[tuple[str, str]]:
    """仓库里被 git 跟踪、又能当文本读的文件（读不了的当二进制跳过）。"""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - 没装 git 才会走到
        pytest.skip(f"拿不到 git 的文件清单，跳过公开材料检查：{exc}")
    files: list[tuple[str, str]] = []
    for relative in listed.stdout.decode("utf-8").split("\0"):
        if not relative:
            continue
        try:
            files.append((relative, (ROOT / relative).read_text(encoding="utf-8-sig")))
        except (UnicodeDecodeError, OSError):
            continue
    assert len(files) > 100, f"只读到 {len(files)} 个文件，这份清单不对，检查本身不可信"
    return files


def _lines(pattern: re.Pattern[str], *, skip_self: bool) -> list[tuple[str, int, str]]:
    """按行扫一遍被跟踪的文本文件，返回 ``(文件, 行号, 那一行)``。"""
    found: list[tuple[str, int, str]] = []
    for relative, text in _tracked_text_files():
        if skip_self and relative == SELF:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                found.append((relative, number, line.strip()))
    return found


def _where(found: list[tuple[str, int, str]]) -> str:
    return "\n".join(f"  {relative}:{number}: {line[:120]}" for relative, number, line in found)


def test_examples_use_the_official_test_thread_or_an_obviously_fake_id() -> None:
    """文档、测试、发布说明里的串号只有两个来源：官方测试串，或编出来的编号。"""
    bad: list[tuple[str, int, str]] = []
    for relative, text in _tracked_text_files():
        for number, line in enumerate(text.splitlines(), 1):
            for match in BARE_ID.finditer(line):
                value = match.group(1)
                if value in ALLOWED_THREADS:
                    continue
                if DATE.fullmatch(value):
                    continue  # 日期，例如 备份目录名里的 20261001
                if re.match(r"[`\s]{0,3}字节", line[match.end() : match.end() + 4]):
                    continue  # 字节数，例如 （12031049 字节 / 956 条目）
                if re.search(r"字节数\s*$", line[: match.start()]):
                    continue  # 另一种写法：字节数 12040398 与刚上传的附件一致
                bad.append((relative, number, line.strip()))
    assert not bad, (
        "公开材料里出现了没登记的 8 位数字：\n"
        f"{_where(bad)}\n"
        "例子请用 X岛官方的测试串 50000001 或编出来的 12345678；"
        "日期写 2026-10-01 这种带分隔符的、字节数后面跟「字节」两个字。"
    )


def test_thread_urls_and_no_numbers_are_official_or_fake() -> None:
    """串网址与 ``No.xxx`` 里的编号同样只能用那两个（外加测试自用的一眼假编号）。"""
    bad: list[tuple[str, int, str]] = []
    for relative, line_number, line in _lines(THREAD_IN_TEXT, skip_self=False):
        for match in THREAD_IN_TEXT.finditer(line):
            value = match.group(1) or match.group(2)
            if value in ALLOWED_THREADS or _obviously_fake(value):
                continue
            bad.append((relative, line_number, line))
            break
    assert not bad, (
        "串网址或 No. 后面的编号不是官方测试串/编的编号：\n"
        f"{_where(bad)}\n"
        "请改成 50000001 或 12345678。"
    )


def test_no_personal_directories_in_public_material() -> None:
    """不许出现本机个人目录、解释器目录名、用户名，也不许把用户写成来源。"""
    bad: list[tuple[str, int, str]] = []
    for relative, line_number, line in _lines(re.compile("|".join(map(re.escape, FORBIDDEN_SUBSTRINGS))), skip_self=True):
        bad.append((relative, line_number, line))
    for relative, text in _tracked_text_files():
        if relative == SELF:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in USER_DIR.finditer(line):
                name = match.group(1)
                if name not in ALLOWED_USER_DIRS:
                    bad.append((relative, number, line.strip()))
    for relative, text in _tracked_text_files():
        if relative == SELF or not USER_STORY_FILES.match(relative):
            continue
        patterns = [FORBIDDEN_USER_STORY]
        if RELEASE_NOTES.match(relative):
            patterns.append(FORBIDDEN_USER_STORY_EXTRA)
        for number, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in patterns):
                bad.append((relative, number, line.strip()))
    for relative, number, line in _lines(FORBIDDEN_ADDRESS, skip_self=True):
        bad.append((relative, number, line))
    assert not bad, (
        "公开材料里出现了本机个人目录/用户名，或把用户写成了来源：\n"
        f"{_where(bad)}\n"
        "路径写 <工作目录>、<本机 Python>，用户目录写 C:\\Users\\你 这种占位符；"
        "来源一律写成客观事实（「2026-10-04 真机复验」这种），别写「按你……要的」「你说」「用户报过」；"
        "发布说明里也别写「你复验了一次」「你贴回来的那三行」「把那个文件也发我」这类"
        "（谁验的、验过几次、下次贴什么，读者不需要知道）。"
    )


def test_no_credentials_in_public_material() -> None:
    """token、Bearer、真饼干值一律不许进公开材料。"""
    bad: list[tuple[str, int, str]] = []
    for pattern in (TOKEN, USERHASH):
        for relative, number, line in _lines(pattern, skip_self=True):
            for match in pattern.finditer(line):
                if pattern is USERHASH and match.group(1) in ALLOWED_USERHASHES:
                    continue
                bad.append((relative, number, line))
                break
    assert not bad, (
        "公开材料里出现了像凭据的东西：\n"
        f"{_where(bad)}\n"
        "测试用的假值请用 deadbeefdeadbeef 这种一眼假的写法。"
    )


def test_emails_are_noreply_or_example_ones() -> None:
    """邮箱只能是 GitHub 的 noreply 地址，或 example.* 示例域。"""
    bad: list[tuple[str, int, str]] = []
    for relative, line_number, line in _lines(EMAIL, skip_self=True):
        for match in EMAIL.finditer(line):
            if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
                bad.append((relative, line_number, line))
                break
    assert not bad, (
        "公开材料里出现了真实邮箱：\n"
        f"{_where(bad)}\n"
        "示例邮箱写 someone@example.com。"
    )
