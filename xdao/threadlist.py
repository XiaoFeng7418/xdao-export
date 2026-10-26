"""从文本文件里读「要导出哪些串」的清单（v0.20.0）。

给命令行 ``--input`` 与界面的「从文件读取」共用。规则刻意做得可预期：

* 一行一个串号或网址，空行跳过，``#`` 或 ``//`` 开头的是注释；
* 解析规则与界面一致（:func:`xdao.cache.parse_thread_id`：网址、纯数字都认）；
* 同一条串号只保留第一次出现，导出顺序就是文件里的顺序；
* **有认不出的行就整体报错并指出行号**——静默跳过最容易出事：用户以为
  二十个串都导了，实际只导了十八个，还没人告诉他。
"""

from __future__ import annotations

from pathlib import Path

from .cache import parse_thread_id

#: 报错时最多列出几行（再多就把信息淹没了）。
MAX_REPORTED_LINES = 5


class ThreadListError(Exception):
    """清单文件读不了、或者里面有不认识的行。"""


def parse_thread_list(text: str) -> list[str]:
    """把清单文本解析成串号列表（去重、保序、跳过空行与注释）。"""
    entries: list[str] = []
    seen: set[int] = set()
    problems: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        thread_id = parse_thread_id(line)
        if thread_id is None:
            problems.append(f"第 {lineno} 行：{line}")
            continue
        if thread_id in seen:
            continue
        seen.add(thread_id)
        entries.append(str(thread_id))

    if problems:
        shown = "\n".join(problems[:MAX_REPORTED_LINES])
        more = len(problems) - len(shown.splitlines())
        tail = f"\n……还有 {more} 行也认不出。" if more > 0 else ""
        raise ThreadListError(
            "清单里有认不出的行（每行要写串号或网址，空行与 # 开头的注释可以）：\n"
            f"{shown}{tail}"
        )
    if not entries:
        raise ThreadListError("清单里没有串号：一行写一个串号或网址（空行与 # 开头的注释不算）。")
    return entries


def read_thread_list(path: str | Path) -> list[str]:
    """读清单文件并解析；读不到时抛 :class:`ThreadListError`。"""
    target = Path(path)
    # Windows 上 open() 一个目录抛的是 PermissionError，不是 IsADirectoryError，
    # 所以先自己看一眼，免得用户看到「拒绝访问」这种不知所云的理由。
    if target.is_dir():
        raise ThreadListError(f"这是个文件夹，不是清单文件：{target}")
    try:
        # utf-8-sig：记事本存下的 UTF-8 常带 BOM，别把它粘到第一个串号上。
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ThreadListError(f"找不到清单文件：{target}") from None
    except IsADirectoryError:
        raise ThreadListError(f"这是个文件夹，不是清单文件：{target}") from None
    except UnicodeDecodeError:
        raise ThreadListError(
            f"清单文件不是 UTF-8 编码，读不出来：{target}\n"
            "用记事本打开后「另存为」→ 编码选 UTF-8 再试。"
        ) from None
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise ThreadListError(f"读不了清单文件：{target}\n{reason}") from None
    return parse_thread_list(text)


__all__ = ["MAX_REPORTED_LINES", "ThreadListError", "parse_thread_list", "read_thread_list"]
