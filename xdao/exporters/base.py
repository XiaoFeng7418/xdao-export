"""导出器基类：把「探目录 → 推导文件名 → 写盘」这套骨架收成一份。

2026-10-07 架构评审发现的毛病：五个导出器各写一份 ``save``（同一套流程抄五遍）、
扩展名在 ``EXPORTERS`` 注册表和各 ``save`` 里各写一份、构造签名彼此不同，于是
``create_exporter`` 只能靠「带全部参数 → ``TypeError`` 就删一项再试」的五层阶梯
兼容（那次阶梯悄悄丢过 ``browser_path`` / ``pdf_timeout`` / ``fallback_html`` /
``pdf_options``）。现在：

* 显示名、扩展名、注册表键是**类属性**，注册表由这些类推导（不再抄一份）；
* 构造签名统一成 ``(client, progress=None, filename_template=None, **options)``，
  不认识的参数落进 ``self.options`` 而不是炸掉构造；
* ``save()`` 是模板方法，子类只管实现 ``build()``（产出内容）。

EPUB 与 PDF 仍然自己覆盖 ``save()``：它们不是「先生成字符串再写文本文件」——
EPUB 直接写 zip，PDF 要交给浏览器渲染，还可能退而存 HTML。
"""

from __future__ import annotations

from pathlib import Path

from ..cancel import check as check_cancel
from ..client import XdaoClient
from ..paths import ensure_writable
from ._shared import ThreadData, derive_filename, render_filename, sanitize_filename


class Exporter:
    """所有导出器的共同骨架。"""

    #: 注册表键（``EXPORTERS`` 的键，界面与命令行都读它）
    key = ""
    #: 界面上显示的名字
    display = "导出"
    #: 产物扩展名（含点）
    suffix = ".txt"
    #: 保存前给用户的一句提示；空串表示这个导出器自己（在 ``build`` 里）提示
    save_message = ""
    #: 文本产物是否强制用 ``\n`` 写盘（Windows 上 ``write_text`` 会写成 CRLF）
    lf_newlines = False

    def __init__(
        self,
        client: XdaoClient,
        progress=None,
        filename_template: str | None = None,
        **options,
    ) -> None:
        self._client = client
        self._progress = progress
        self.filename_template = filename_template
        # 取消用的停止位（threading.Event）：界面点「取消导出」时举起来。
        # 没传就是 None，等于这趟导出不可取消（命令行、监控都走这条）。
        self.stop = options.get("stop")
        # 不认识的参数留在这里，不炸构造：调用方可以把所有参数一次性交给每个导出器，
        # 由各自取用（这正是 ``create_exporter`` 能删掉那条回退阶梯的原因）。
        self.options: dict = dict(options)
        # 批量断点续跑：同名成品已存在且非空就不重做。只影响手动/命令行导出，
        # 监控每轮本来就该重新导出新回复，所以不走这条路。
        self.skip_existing = bool(options.get("skip_existing"))
        #: 这一趟是不是「跳过」而不是真导出（调用方据此计数/写日志）
        self.last_skipped = False

    # ---------- 基础工具 ----------

    def check_cancel(self) -> None:
        """停止位举起来了就抛 :class:`~xdao.cancel.ExportCancelled`。"""
        check_cancel(self.stop)

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

    def existing_output(self, thread: ThreadData, output_dir: Path | str) -> Path | None:
        """开关打开、且目标文件已存在且非空时，返回那个路径（表示应当跳过）。"""
        if not self.skip_existing:
            return None
        path = self.target_path(thread, output_dir)
        try:
            if path.exists() and path.stat().st_size > 0:
                return path
        except OSError:
            return None
        return None

    def reuse_existing(self, path: Path) -> Path:
        """把「跳过」这件事记在对象上并提示一句，然后返回原路径。"""
        self.last_skipped = True
        self._notify(f"已存在，跳过：{path.name}")
        return path

    def output_name(self, thread: ThreadData) -> str:
        """按文件名模板（若设置）推导文件名主体。"""
        return render_filename(self.filename_template, thread, derive_filename(thread))

    def target_path(self, thread: ThreadData, output_dir: Path | str) -> Path:
        """产物路径：目录 + 安全化的文件名主体 + :attr:`suffix`。"""
        return Path(output_dir) / (sanitize_filename(self.output_name(thread)) + self.suffix)

    # ---------- 子类实现 ----------

    def build(
        self,
        thread: ThreadData,
        scope: str = "all",
        include_hashes: list[str] | None = None,
    ):
        """产出内容（文本导出器返回字符串）。子类必须实现。"""
        raise NotImplementedError(f"{type(self).__name__} 没有实现 build()")

    # ---------- 模板方法 ----------

    def write_text(self, path: Path, text: str) -> None:
        """写文本产物；需要固定 ``\n`` 的导出器把 :attr:`lf_newlines` 打开。"""
        if self.lf_newlines:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        else:
            path.write_text(text, encoding="utf-8")

    def save(
        self,
        thread: ThreadData,
        scope: str = "all",
        output_dir: Path | str = ".",
        include_hashes: list[str] | None = None,
    ) -> Path:
        """先确认目录可写，再生成内容并写盘，返回产物路径。

        目录先探一次是因为抓一个长串可能要几分钟，写不进去的话那一趟就白跑了。
        """
        output_dir = ensure_writable(output_dir)
        existing = self.existing_output(thread, output_dir)
        if existing is not None:
            return self.reuse_existing(existing)
        if self.save_message:
            self._notify(self.save_message)
        path = self.target_path(thread, output_dir)
        content = self.build(thread, scope, include_hashes)
        # 内容都做好了才检查取消：真的被取消就不落盘，免得留下一份半成品
        # 混在导出目录里（用户取消之后不会再去核对哪几份是完整的）。
        self.check_cancel()
        self.write_text(path, content)
        return path
