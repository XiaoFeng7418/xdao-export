# v0.13.19：写明白「用浏览器登录」只支持 Chrome / Edge 这类内核

## 现象

「用浏览器登录」整条路走的是 Chromium 的调试协议（CDP）：Chrome 与 Edge 都是 Chromium，
协议一致，所以一套实现同时支持两者；Firefox 用的是另一套（Marionette / WebDriver BiDi），
这条路在它上面根本不存在。程序一直是对的 —— 错的是**话没说明白**：

- 登录窗开头只写「干净的一次性窗口」，没说是哪种浏览器；
- `packaging/使用说明.txt` 的支持浏览器一节只列了「Edge、Chrome、Chromium、Brave」；
- 平时用 Firefox 的人点开这条路，只能对着一个一直不动的窗口等；机器上只有 Firefox 时，
  界面只说「没找到 Edge 或 Chrome。请先装一个」—— 没解释为什么 Firefox 不算数。

## 改法

- 登录窗开头加两行：「用的是机器上的 Chrome / Edge（Chromium、Brave 也行）；Firefox
  内核的浏览器不行，两者不是同一套内核。」（界面里每行一句 —— Tk 的中文折行按字符切，
  长句容易把「X岛」这种词劈开。）
- 找不到浏览器时那句提示补全：「没找到 Edge 或 Chrome。Windows 自带的 Edge 一般就有；
  Firefox 走不了这条路（内核不同）。也可以改用「直接粘贴饼干登录」。」
- `README.md` 的「用浏览器登录」那条标注成「Edge / Chrome 这类 Chromium 内核…**Firefox 不行**」。
- `packaging/使用说明.txt` 支持浏览器那一节加一条：Firefox 用不了这条路、为什么，
  以及「系统默认浏览器是 Firefox」时程序照旧往下找 Edge / Chrome。
- `MAINTENANCE.md` 的浏览器登录一节记下这个边界（为什么只有 Chromium 内核能用；
  `browser_login.choose_browser()` 认不出 Firefox 时按原顺序往下找，所以不用改功能）。
- 新增 `tests/test_browser_scope.py`（3 条）：界面与两份公开文档都得写着这件事，
  同时钉住 `browser_login._CANDIDATES` 里**真的**没有 Firefox —— 将来真加了支持，
  这三处说明会被提醒一起改，而不是让文档继续写着「不行」。

程序逻辑一行没动（挑浏览器那套本来就会跳过认不出的浏览器），**同一个输入得到的导出结果
与 0.13.18 完全一致**。

## 真机核验

- 本机全量：`1562 passed / 7 skipped`（共 1569 项；比上一版多 3 个新用例）。
- 文本卫生：`MAINTENANCE.md` CRLF 1361 / 裸 LF 0、`README.md` CRLF 476 / 裸 LF 0、
  `packaging/使用说明.txt` BOM 1 / 裸 LF 515（这份说明一直是「带 BOM + 裸 LF」）。
- 打包版 `--version` → `X岛串导出工具 0.13.19`；`--selftest` 与上一版基线结论一致。
- 免安装包里的 `使用说明.txt` 与仓库文件逐字节一致。
- `tools/repo_check.py` 14 项全绿。

## 附件

- `xdao-export-v0.13.19-win64.zip`：免安装包，解压后双击 `xdao-export.exe`。
  与上一版相比只有这几句说明文字变了，功能与导出结果完全一致。
