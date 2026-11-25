"""Check tracked public text for private identifiers and conversational residue.

The checks cover personal paths, credentials, email addresses, chat references,
and language that attributes project decisions to private conversations. Narrative
scans are limited to documentation and source comments/docstrings so normal GUI,
CLI, and log messages remain available to users.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SELF = "tests/test_public_material.py"

ALLOWED_THREADS = frozenset({"50000001", "50577215", "11689471", "12345678"})
ALLOWED_USER_DIRS = frozenset({"你", "<你", "...", "me"})
ALLOWED_USERHASHES = frozenset({"deadbeefdeadbeef"})
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"users.noreply.github.com", "example.com", "example.org", "example.net", "example.invalid"}
)

# Join sensitive literals so the guard does not report its own rule definitions.
_PRIVATE_MARKERS = (
    "".join(("\u6653", "\u98ce")),
    "".join(("Phillip", " ", "Deng")),
    "".join(("145", "15")),
    "".join(("python", "3129")),
    "".join(("\u5c0f", "\u73a9", "\u610f")),
    "".join(("gh", "cli")),
    "".join(("1920", "x", "1080")),
    "".join(("1920", "\u00d7", "1080")),
)
PRIVATE_MARKER = re.compile(
    r"\b" + "".join(("D", "S", "H")) + r"\b|" + "|".join(map(re.escape, _PRIVATE_MARKERS)),
    re.IGNORECASE,
)
USERNAME_VARIANT = re.compile(r"\b" + "Xiao" + "Feng" + r"(?!7418)", re.IGNORECASE)

_CONVERSATION_PARTS = (
    ("更换", "模型"), ("夜", "班"), ("我替", "用户"),
    ("这", "一轮"), ("上", "一轮"), ("那", "一轮"),
    ("刚", "才"), ("顺", "手"), ("顺", "带"), ("人", "肉"),
    ("拍", "脑袋"), ("冤", "案"), ("踩", "坑"), ("踩", "雷"),
    ("岛" + "友", "原话"), ("使用者", "问"), ("有", "用户"), ("某位", "用户"),
    ("投诉", "过"), ("贴", "回来"), ("发", "我"), ("发给", "我"), ("发给", "维护者"),
    ("等有", "结果再说"), ("用户", "决定"),
    ("我", "们"), ("咱", "们"), ("我", "把"), ("我", "事后"),
    ("用户", "复验"), ("踩", "过的坑"), ("一眼", "假"), ("撞", "自己"),
    ("捅", "破"), ("审", "死"), ("逮", "到"), ("带", "偏"), ("补", "一道"),
)
CONVERSATION_TERMS = tuple("".join(parts) for parts in _CONVERSATION_PARTS)
CONVERSATION = re.compile("|".join(map(re.escape, CONVERSATION_TERMS)), re.IGNORECASE)
HANDOFF_RESIDUE = re.compile(r"(?<!提)" + "交" + "接" + r"(?!口)")

_ADDRESS_PARTS = (
    ("使用者", "问"), ("岛" + "友", "原话"), ("有", "用户"), ("某位", "用户"),
    ("用户", "决定"), ("按你", "要求"), ("你", "说"), ("你", "要求"),
    ("你", "报"), ("你", "反馈"), ("你", "提到"), ("你", "反映"),
    ("你", "提的"), ("你", "发的"), ("你", "给的"), ("你", "要的"),
    ("你要", "我"), ("你的", "问题"), ("投诉", "过"),
    ("贴", "回来"), ("发", "我"), ("发给", "维护者"),
    ("你", "刚"), ("你", "那次"), ("你", "上次"), ("你", "复验"),
    ("你", "贴的"), ("你", "截的"), ("我", "这次"),
)
ADDRESS = re.compile(
    "|".join(map(re.escape, ("".join(p) for p in _ADDRESS_PARTS)))
    + "|按" + "你" + r"(?!设)"
)
FIRST_PERSON = re.compile(r"(?<!自)" + "我" + r"(?!的饼干)")
USER_STORY = re.compile(
    "你" + r"(?:上次|那次|那两|那三|复验|验证|贴|发|截|手上|多半)"
    + "|等" + "我" + r"(?!们)"
    + "|下次" + r"[^。\n]{0,8}复验"
)
USER_STORY_FILES = re.compile(r"^(?:docs/RELEASE_NOTES_v[0-9.]+\.md|packaging/使用说明\.txt)$")

BARE_ID = re.compile(r"(?<![0-9A-Za-z_])(\d{8})(?![0-9A-Za-z_])")
DATE = re.compile(r"20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")
THREAD_IN_TEXT = re.compile(r"nmbxd1\.com/t/(\d{4,9})|(?<![\w.])No\.(\d{4,9})")
CHAT_ID = re.compile(r"\bm" + r"\d{4,6}\b", re.IGNORECASE)
TOKEN = re.compile(r"ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|Bearer\s+[A-Za-z0-9._\-]{20,}")
USERHASH = re.compile(r"userhash=([0-9a-f]{16})(?![0-9a-f])")
EMAIL = re.compile(r"[\w.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
USER_DIR = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}([^\\/\s\"'|)>,;]+)")

DOC_EXTENSIONS = {".md", ".txt", ".rst"}
COMMENT_EXTENSIONS = {".py", ".ps1", ".cmd", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".spec", ".sh", ".bat"}
TECHNICAL_PHASES = re.compile(
    "本轮" + r"(?!抓取|请求|循环|检查|监控|运行|下载|查询)"
    + "|这" + "轮"
    + "|下一" + "轮" + r"(?!\s*(?:retarget|程序|会自己|重试|重新|轮询|检查|读取|才|就|还|再|会))"
    + "|接手" + r"(?!窗口|浏览器|句柄)"
)


def _tracked_text_files() -> list[tuple[str, str]]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"Unable to read the tracked-file list: {exc}")
    files: list[tuple[str, str]] = []
    for relative in listed.stdout.decode("utf-8").split("\0"):
        if not relative:
            continue
        path = ROOT / relative
        try:
            data = path.read_bytes()
            if b"\0" in data[:8192]:
                continue
            files.append((relative, data.decode("utf-8-sig")))
        except (UnicodeDecodeError, OSError):
            continue
    assert len(files) > 100, f"Only {len(files)} tracked text files were read"
    return files


def _narrative_surfaces(relative: str, text: str) -> list[tuple[int, str]]:
    """Return documentation prose and non-runtime source commentary."""
    suffix = Path(relative).suffix.lower()
    if suffix in DOC_EXTENSIONS:
        surfaces = []
        in_code_block = False
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(("```", "~~~")):
                in_code_block = not in_code_block
            elif not in_code_block:
                surfaces.append((number, line))
        return surfaces
    if suffix not in COMMENT_EXTENSIONS:
        return [(number, line) for number, line in enumerate(text.splitlines(), 1)]
    if suffix == ".py":
        surfaces: list[tuple[int, str]] = []
        try:
            tree = ast.parse(text, filename=relative)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                    first = node.body[0]
                    if (
                        isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)
                    ):
                        surfaces.append((first.value.lineno, first.value.value))
                if isinstance(node, ast.Assert) and node.msg is not None:
                    message = ast.get_source_segment(text, node.msg)
                    if message:
                        surfaces.append((node.msg.lineno, message))
        except SyntaxError:
            return []
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.COMMENT:
                    surfaces.append((token.start[0], token.string))
        except (IndentationError, tokenize.TokenError):
            pass
        return surfaces
    marker = "#"
    return [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), 1)
        if line.lstrip().startswith(marker)
    ]


def _display(found: list[tuple[str, int, str]]) -> str:
    return "\n".join(f"  {path}:{line}: {text[:180]}" for path, line, text in found)


def _obviously_synthetic(value: str) -> bool:
    return len(value) <= 4 or (len(value) == 7 and value[0] in "78")


def _has_private_marker(text: str) -> bool:
    return bool(PRIVATE_MARKER.search(text) or USERNAME_VARIANT.search(text))


def _has_community_address(text: str) -> bool:
    return "岛" + "友" in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("岛" + "友", True),
        ("面向岛" + "友的说明", True),
        ("欢迎岛" + "友", True),
        ("用户说明", False),
        ("岛屿与朋友", False),
    ],
)
def test_community_address_guard_catches_standalone_and_embedded_terms(
    text: str, expected: bool
) -> None:
    assert _has_community_address(text) is expected


def _question_residue(text: str) -> bool:
    without_urls = re.sub(r"https?://[^\s)>`]+", "", text, flags=re.IGNORECASE)
    without_url_params = re.sub(r"\?[A-Za-z_][\w-]*=", "", without_urls)
    without_inline_code = re.sub(r"(?<!`)(`{1,3}).*?\1(?!`)", "", without_url_params, flags=re.DOTALL)
    if "？" in without_inline_code:
        return True
    without_regex_groups = re.sub(r"\(\?[!:<=>A-Za-z-]+", "", without_inline_code)
    without_escaped_question = re.sub(r"\\\?", "", without_regex_groups)
    return "?" in without_escaped_question


def test_public_material_has_no_private_identifiers_or_conversation_artifacts() -> None:
    bad: list[tuple[str, int, str]] = []
    for relative, text in _tracked_text_files():
        for number, line in enumerate(text.splitlines(), 1):
            if (
                _has_private_marker(line)
                or _has_community_address(line)
                or CHAT_ID.search(line)
                or re.search("发" + "给我" + "|发" + "我" + "|我替" + "用户", line)
            ):
                bad.append((relative, number, line.strip()))
        for number, surface in _narrative_surfaces(relative, text):
            if (
                CONVERSATION.search(surface)
                or HANDOFF_RESIDUE.search(surface)
                or TECHNICAL_PHASES.search(surface)
                or ADDRESS.search(surface)
                or FIRST_PERSON.search(surface)
                or (USER_STORY_FILES.match(relative) and USER_STORY.search(surface))
            ):
                bad.append((relative, number, surface.strip()))
    assert not bad, "Private identifiers or conversation artifacts remain:\n" + _display(bad)


def test_narrative_text_has_no_rhetorical_questions() -> None:
    bad: list[tuple[str, int, str]] = []
    for relative, text in _tracked_text_files():
        for number, surface in _narrative_surfaces(relative, text):
            if re.search(r"为什么|到底", surface) or _question_residue(surface):
                bad.append((relative, number, surface.strip()))
    assert not bad, "Question-style wording remains in documentation or source commentary:\n" + _display(bad)


def test_question_guard_preserves_url_queries_and_regex_syntax() -> None:
    assert not _question_residue("See https://example.org/items?page=1&name=sample")
    assert not _question_residue("Use ``pattern (?:alpha|beta)`` in the expression.")
    assert not _question_residue(r"Pattern: (?:alpha|beta)(?!gamma)(?=delta) and a literal \?")
    assert _question_residue("Why keep this section?")
    assert _question_residue("这个标题为什么保留？")

    for text in (
        "这" + "轮没改", "下" + "一轮处理", "我" + "们已决定",
        "踩" + "过的坑", "用户" + "复验的截图", "我" + "事后手动补的",
        "你" + "刚贴的日志", "你" + "那次复验", "撞" + "自己",
    ):
        assert CONVERSATION.search(text) or TECHNICAL_PHASES.search(text) or ADDRESS.search(text)
    assert not TECHNICAL_PHASES.search("下一轮 retarget 会重连")
    assert not TECHNICAL_PHASES.search("本轮抓取已完成")
    assert FIRST_PERSON.search("我" + "事后补齐附件")
    assert not FIRST_PERSON.search("打开站点的我的饼干页面")
    assert USER_STORY.search("下次" + "真机复验时提供结果")
    runtime = 'from tkinter import messagebox\nmessagebox.askyesno("升级", "现在下载并安装吗？")'
    assert _narrative_surfaces("xdao/example.py", runtime) == []


def test_example_thread_identifiers_are_registered_or_synthetic() -> None:
    bad: list[tuple[str, int, str]] = []
    for relative, text in _tracked_text_files():
        for number, line in enumerate(text.splitlines(), 1):
            for match in BARE_ID.finditer(line):
                value = match.group(1)
                if value in ALLOWED_THREADS or DATE.fullmatch(value):
                    continue
                if re.match(r"[`\s]{0,3}字节", line[match.end() : match.end() + 4]):
                    continue
                if re.search(r"字节数\s*$", line[: match.start()]):
                    continue
                bad.append((relative, number, line.strip()))
    for relative, text in _tracked_text_files():
        for number, line in enumerate(text.splitlines(), 1):
            for match in THREAD_IN_TEXT.finditer(line):
                value = match.group(1) or match.group(2)
                if value not in ALLOWED_THREADS and not _obviously_synthetic(value):
                    bad.append((relative, number, line.strip()))
                    break
    assert not bad, "Unregistered thread identifiers remain:\n" + _display(bad)


def test_no_personal_directories_credentials_or_real_email_addresses() -> None:
    bad: list[tuple[str, int, str]] = []
    for relative, text in _tracked_text_files():
        for number, line in enumerate(text.splitlines(), 1):
            if _has_private_marker(line):
                bad.append((relative, number, line.strip()))
            for match in USER_DIR.finditer(line):
                if match.group(1) not in ALLOWED_USER_DIRS:
                    bad.append((relative, number, line.strip()))
            if TOKEN.search(line):
                bad.append((relative, number, line.strip()))
            for match in USERHASH.finditer(line):
                if match.group(1) not in ALLOWED_USERHASHES:
                    bad.append((relative, number, line.strip()))
            for match in EMAIL.finditer(line):
                if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
                    bad.append((relative, number, line.strip()))
    assert not bad, "Personal paths, credentials, or unapproved email addresses remain:\n" + _display(bad)
