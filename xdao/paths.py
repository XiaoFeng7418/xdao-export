"""目录与写权限：导出目录、缓存目录能不能写，以及写不进去时的兜底。

这个模块是**基础设施**，故意不放在 ``xdao.exporters`` 里：缓存层
（``xdao.cache``）与自检层（``xdao.preflight``）都要用它，而它们不该为了一个
目录探测去依赖「导出格式」子包（2026-10-07 架构评审记录的四条倒挂里，有两条
就是 ``cache → exporters._shared`` 与 ``preflight → exporters._shared``）。

历史沿革（判断行为时别只看代码）：

- 0.5.0 之前探针失败就等于目录不可写，结果用户明明能写却被告知「导出目录不可写」；
- 0.5.1 起：探针改用与成品同类的普通文件名，且探针失败**不再拦下导出**，
  只有「目录连创建都做不到」才提前失败；
- 0.5.2 起：探测要**往下一层**探（``.cache`` 建得出、``.cache/pages`` 拒绝访问的
  真机教训），并且第二个探针名（``xdao-write-test.txt``）能写就不算不可写；
- 0.5.3 起：``choose_writable_dir`` 在写不进去时自动换到 ``%LOCALAPPDATA%`` 或
  ``%TEMP%``，并把「为什么换」写成说明交给调用方去告诉用户。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class OutputDirNotWritable(Exception):
    """导出目录不可写。提前抛出，避免白抓一遍再失败。"""


# 写权限探针的文件名：故意用**普通文件名**而不是隐藏文件。
# 2026-09-30 的教训：原来的探针叫 ``.xdao-write-probe``，被安全软件/策略挡掉后
# 程序就拒绝导出，而用户真正的导出文件明明写得进去（0.1.0 能做的事 0.5.0 反而
# 做不了）。探针要和真实导出物同类，结论才有意义。
PROBE_NAME = "xdao-write-test.tmp"

# 探针的第二个名字：第一个名字被安全软件/策略单独挡掉时，换一个再探一次。
# 2026-10-02 真机：用户的导出目录（文档下的自建文件夹）与 %LOCALAPPDATA% 兜底
# **同时**被判「写不进去」，可磁盘、权限、盘符都正常，导出也照常跑完 ——
# 一个名字被拦不等于目录不能写，所以不能只凭一次失败就下结论。
PROBE_ALT_NAME = "xdao-write-test.txt"


def ensure_writable(output_dir: Path | str, kind: str = "导出") -> Path:
    """确认导出目录可写，返回规范化后的目录。

    抓一个长串可能要几分钟，如果最后才发现目录写不进去，那一趟就白跑了，
    所以开始抓之前先探一次。目录不存在时会尝试创建。

    **探针失败不再等于目录不可写**：0.5.0 之前只要有一步写不进去就直接拦下
    导出，结果用户明明能正常写这个目录（用记事本、用旧版本都行），程序却弹出
    「导出目录不可写」拒绝开工 —— 那是探针文件（``.xdao-write-probe``）自己被
    安全软件/策略/只读介质挡了，跟真正的导出结果文件不是一回事。现在：

    * 探针文件改成**和导出结果同类的普通文件**（``xdao-write-test.tmp``，不留
      隐藏属性、不带前导点），写完立刻删掉 —— 它写不动往往意味着真的写不了；
    * 即使这样探针还是失败，也**不拦下导出**：真实写盘失败会带着真实文件名和
      真实 errno 报出来；
    * 只有"目录连创建都做不到"才提前失败（那才是真的没法用）。
    """
    target = Path(output_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputDirNotWritable(
            f"{kind}目录无法创建：{target}\n{exc}\n"
            "请换一个可写的目录，或检查该位置的权限。"
        ) from exc

    probe = target / PROBE_NAME
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        # 探针写不动 —— 只是少了一层提前预警，不代表导出会失败
        # （不同名的文件、已有的文件往往照样能写）。
        probe.unlink(missing_ok=True)
    return target


def _probe_once(target: Path, name: str) -> str:
    """用 ``name`` 在 ``target`` 里探一次：能写返回空串，不能写返回「哪一步 + 为什么」。

    两步都要过：先在目录里写一个文件，再建一个临时子目录并在里面写文件
    （真实导出/缓存都会往下一层建目录，只探表层会把不可写的目录判成「能写」）。
    探针自己收拾干净，不留文件也不留目录。
    """
    probe = target / name
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        return f"写 {name} 时：{exc}"
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    child = target / f"{name}.d{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        child.mkdir()
    except OSError as exc:
        return f"建子目录 {child.name} 时：{exc}"
    try:
        (child / name).write_text("ok", encoding="utf-8")
    except OSError as exc:
        return f"在子目录 {child.name} 里写文件时：{exc}"
    finally:
        try:
            (child / name).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            child.rmdir()
        except OSError:
            pass
    return ""


def probe_writable(directory: Path | str) -> tuple[bool, str]:
    """探一次目录能不能写，返回 ``(能不能写, 不能写的原因)``。

    和 :func:`can_write_dir` 探的是同一件事，区别只有一个：**把失败原因带回来**。
    只回一句「写不进去」在真机上没法排查 —— 2026-10-02 那台机器的导出目录和
    %LOCALAPPDATA% 兜底一起报不可写，可同一台机器拿别的程序写同一个目录毫无问题，
    程序自己最后也把文件写进去了：错的是探针，不是目录。

    两个名字各探一遍（``xdao-write-test.tmp`` / ``xdao-write-test.txt``）：
    探针名被安全软件单独挡掉是见过的真事，换一个名字能写就不该判「不可写」。
    """
    target = Path(directory)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"目录建不出来：{exc}"
    reasons: list[str] = []
    for name in (PROBE_NAME, PROBE_ALT_NAME):
        reason = _probe_once(target, name)
        if not reason:
            return True, ""
        reasons.append(reason)
    return False, "；".join(reasons)


def can_write_dir(directory: Path | str) -> bool:
    """轻量探测：目录能不能写。只回答是或否，不抛异常。

    和 ``ensure_writable`` 探的是同一件事，区别是这里要拿来**换地方重试**
    （例如缓存目录写不进去时另找一个），所以失败不该炸掉调用方。

    探测要**往下一层**探，不能只看这一层：实测过 ``D:\\X岛\\.cache`` 这一级
    建得出来、``.cache\\pages`` 却拒绝访问——只探表层会把这种目录判成"能写"，
    然后换目录的兜底逻辑就永远不会触发（v0.5.1 的实测教训）。

    想知道**为什么**写不进去（排查用），用 :func:`probe_writable`。
    """
    return probe_writable(directory)[0]


@dataclass(frozen=True)
class DirChoice:
    """换目录的结果：最终目录、要告诉用户的说明、以及"是不是换了地方"。"""

    path: Path
    notes: list[str]
    fallback: bool


def fallback_dirs() -> list[Path]:
    """给出"肯定能写"的兜底目录候选，按优先级排列。

    ``%LOCALAPPDATA%`` 与 ``%TEMP%`` 是 Windows 明确留给用户程序写数据的地方，
    安全软件的"受控文件夹访问"默认保护的是桌面/文档/图片/视频，不拦这两个 ——
    用户的桌面目录被拦死时，这里是唯一还写得进去的位置。
    """
    bases: list[Path] = []
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if local:
        bases.append(Path(local) / "xdao-export" / "导出")
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if temp:
        bases.append(Path(temp) / "xdao-export")
    if not bases:
        bases.append(Path.home() / "xdao-export")
    return bases


def _unique_dir(path: Path) -> Path:
    """尽量避免覆盖上一次的兜底产物：同名就加 -2、-3。"""
    if not path.exists():
        return path
    for index in range(2, 100):
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            return candidate
    return path


def _with_reason(text: str, why: str) -> str:
    """把「为什么」拼进说明里；没拿到原因（测试注入的探针）就只留正文。"""
    return f"{text}（{why}）" if why else text


# 拿不到具体原因时给的经典解释：Windows 的「受控文件夹访问」（勒索软件防护）
# 默认保护桌面/文档/图片/视频，只有白名单里的程序能写 —— 这是最常见的成因。
CLASSIC_BLOCK_HINT = "当前账户没有写入权限，或被安全软件的「受控文件夹访问」拦截"


def choose_writable_dir(
    requested: Path | str,
    *,
    kind: str = "导出",
    allow_fallback: bool = True,
    probe: Callable[[Path], bool] | None = None,
) -> DirChoice:
    """挑一个真正写得进去的目录，返回 :class:`DirChoice`。

    这是 2026-09-30 那次报障之后的兜底：Windows 上「桌面」「文档」这类目录可能被
    安全软件的**受控文件夹访问**（勒索软件防护）保护，只有白名单里的程序能写 ——
    导出目录设在桌面下的新文件夹，抓取全部成功、写文件时 ``[Errno 13]``，
    整趟白跑。不该为了写一个 HTML 文件先去学怎么配白名单。

    所以：请求的目录探不通时，自动改用 :func:`fallback_dirs` 里能写的位置，
    并把"原目录为什么不能用、这次写到哪"写成说明交给调用方去告诉用户。
    ``allow_fallback=False``（用户显式指定了目录时）只探测、不换地方。

    ``probe`` 只为测试保留（默认 :func:`can_write_dir`），调用方不用传。
    """
    check = probe or can_write_dir
    target = Path(requested)
    if check(target):
        return DirChoice(target, [], False)
    # 用真身探测时把原因一并带回来（「为什么」是排查的唯一线索）；
    # 测试注入的 probe 只回答是/否，这里就不编原因。
    why = "" if probe is not None else probe_writable(target)[1]
    # 探不通不等于导出会失败（v0.5.0 那次教训），所以措辞是「先说清楚，再照常往下走」。
    tail = "这一趟仍然写在你选的目录里；真写不进去会在导出时报出真实的文件名与错误。"

    if not allow_fallback:
        return DirChoice(
            target,
            [f"{kind}目录 {target} 写不进去（{why or CLASSIC_BLOCK_HINT}）。" + tail],
            False,
        )

    last_reason = ""
    for base in fallback_dirs():
        candidate = _unique_dir(base)
        if check(candidate):
            return DirChoice(
                candidate,
                [
                    f"{kind}目录 {target} 写不进去（{why or CLASSIC_BLOCK_HINT}），"
                    f"已自动改用 {candidate}。",
                    f"这次的成品都在 {candidate} 里；想固定用别的位置，可以在「设置」里"
                    "换一个目录，或把本程序加入安全软件的白名单。",
                ],
                True,
            )
        if probe is None:
            last_reason = probe_writable(candidate)[1]

    # 连兜底位置都探不通：如实说清两边各自的原因，然后照原目录继续
    # —— 真写不进去时，导出会带着真实文件名和 errno 报出来。
    first = fallback_dirs()[0]
    return DirChoice(
        target,
        [
            _with_reason(f"{kind}目录 {target} 写不进去", why)
            + _with_reason(f"，连备用位置（{first}）也不行", last_reason)
            + "。" + tail
        ],
        False,
    )
