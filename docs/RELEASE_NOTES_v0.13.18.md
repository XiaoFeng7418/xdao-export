# v0.13.18：免安装包里那份说明，补上五个命令行开关

## 现象

`packaging/使用说明.txt` 会随免安装包一起发出去，是用户手上唯一那份说明书。它列了
PDF、缓存、EPUB 图片这些功能，却一次都没提过下面五个开关 —— 照文档抄的人根本不知道
它们存在，只能去翻 `--help`：

`--cache-dir`、`--verify`、`--image-mode`、`--pdf-timeout`、`--pdf-margin-mm`。

同一个毛病还有另一半：文档和程序之间没有任何机械约束。开关改名、删掉，或者新加一个
忘了写文档，测试全绿、CI 全绿，只会在用户那里变成「照着说明抄一遍，程序说没有这个开关」。

## 改法

- 「功能速览」里补三条：EPUB 图片的三种模式（`--image-mode embed|url|drop`）、
  缓存目录可以固定（`--cache-dir 目录`）、校验老楼层是否被编辑过（`--verify`，
  界面那个开关在「串监控」窗口里）。
- PDF 那一节补两条：页边距的毫米写法 `--pdf-margin-mm 12.5`（与 `--pdf-margin 12.5` 等价）、
  渲染超时 `--pdf-timeout 秒`（默认 900 秒）。
- `README.md` 的「校验老楼层改动」那条标注上它的命令行开关是 `--verify`。
- 新增 `tests/test_cli_docs.py`：拿 `main.build_parser()` 当唯一的事实来源 ——
  ①`README.md` 与 `packaging/使用说明.txt` 里**命令行示例行**（以 `python main.py` /
  `xdao-export.exe` / `main.py` 开头）用到的开关必须真的存在；②解析器里每个不是
  `argparse.SUPPRESS` 的选项，至少要在这两份文档之一露过面。上面那五个开关就是它
  第一次跑时红出来的。
- `MAINTENANCE.md` 第 8 条记下这道把关（连同上一版的公开材料检查），下次加开关时
  照着写文档就行。

程序本体、界面布局、导出结果与 0.13.17 完全一致。

## 真机核验

- 本机全量：`1252 passed / 7 skipped`（比上一版多 3 个新用例）。
- 新用例跑过反向验证：往 `README.md` 的示例行里塞一个不存在的开关，它当场变红、
  指出是哪个开关写在哪一行；再用 `git checkout` 还原。
- 文本卫生：`MAINTENANCE.md` CRLF 1132 / 裸 LF 0、`README.md` CRLF 476 / 裸 LF 0、
  `packaging/使用说明.txt` BOM 1 / 裸 LF 505（这份说明一直是「带 BOM + 裸 LF」）。
- 打包版 `--version` → `X岛串导出工具 0.13.18`；`--selftest` 与上一版基线结论一致。
- 免安装包里的 `使用说明.txt` 与仓库文件逐字节一致。
- `tools/repo_check.py` 13 项全绿。

## 附件

- `xdao-export-v0.13.18-win64.zip`：免安装包，解压后双击 `xdao-export.exe`。
  与上一版相比程序本体没有变化，只有里面的 `使用说明.txt` 换成了补齐开关的那份。
