# v0.19.4：排障开关 --pdfdiag 在免安装包里真能用了

这一版不改任何用户能看到的行为，只修一个平时看不见、排障时用得上的开关。

## 为什么

`--pdfdiag` 是隐藏的排障开关（不在 `--help`、不在使用说明里）：跑一遍
`tools/pdf_diag.py`，打印浏览器路径、工作目录、关键环境变量与这次要补的附加参数，
再用六种方式各启动一次浏览器，最后收成一句结论——**这台机器到底能不能渲染 PDF**。
换过浏览器、装了安全软件、导出 PDF 失败时，先看它就是最快的。

问题是它**在免安装包里一直是坏的**：`main.py` 里那句 `from tools.pdf_diag import main`
是延迟导入，PyInstaller 静态扫描看不到，不显式点名就不会被打进包。所以敲
`xdao-export.exe --pdfdiag` 只会得到 `ModuleNotFoundError`（窗口版还会把错误吞掉）。

## 这版做了什么

- 打包命令里加 `--hidden-import tools.pdf_diag`，把排障模块显式打进包；
  命令段抽成 `pyinstaller_command()` 纯函数，配置与打包不再各写一份；
- 新建 `tools/__init__.py`，让 `tools` 成为常规包（命名空间包在打包时的边界情况更少）；
- `tests/test_build_zip.py` +2：一条盯「命令里必须带上这个模块」，一条盯「开关与打包成对」。

顺带记一笔：仓库根目录那几个 `*.spec` 是历史文件，真正打包用的是
`tools/build_zip.py` 自己拼的命令行——以后改打包参数别去改 spec。

## 影响

- 对正常导出、界面、命令行参数：**没有任何变化**；
- 对排障：打包版现在能真正跑 `xdao-export.exe --pdfdiag`，退出码 0 表示这台机器能出 PDF，
  1 表示不能（或压根找不到浏览器）。

## 升级建议

用着没问题可以不升级；遇到过「导出 PDF 失败」「换了浏览器之后 PDF 就不行」的，
这一版能少一轮来回。

## 附件

- xdao-export-v0.19.4-win64.zip —— 免安装包（解压后双击 xdao-export.exe），
  12,207,054 字节 / 957 个条目
- xdao-export-v0.19.4-win64.zip.sha256 —— 上面那个 zip 的 SHA256：
  471a9c5475a7c2622370a2dd44e00639b8f5ae16a5d66d6e5c29f45ca4f69f8e

详细变更见仓库里的 MAINTENANCE.md。
