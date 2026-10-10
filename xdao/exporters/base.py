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
        # 不认识的参数留在这里，不炸构造：调用方可以把所有参数一次性交给每个导出器，
        # 由各自取用（这正是 ``create_exporter`` 能删掉那条回退阶梯的原因）。
        self.options: dict = dict(options)

    # ---------- 基础工具 ----------

    def _notify(self, message: str) -> None:
        if self._progress:
            self._progress(message)

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
        if self.save_message:
            self._notify(self.save_message)
        path = self.target_path(thread, output_dir)
        self.write_text(path, self.build(thread, scope, include_hashes))
        return path
