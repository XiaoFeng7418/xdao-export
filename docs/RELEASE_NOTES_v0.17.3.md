# v0.17.3：一次点「用浏览器登录」冒出好几扇窗 —— 四个根因逐个修掉

> 修版线：只修缺陷，界面、命令行开关、导出格式一律没动。
> 谢谢站内反馈里那位导出 102 页跑团串的用户，和几位报「弹出好几个浏览器窗口」的人 —— 没有这条反馈，
> 我本来会把这当成一次偶发。

## 一句话结论

「开好几个浏览器窗口」不是同一个 bug 发作四次，而是四条各自独立的缺口叠在一起：
程序**看不出**刚起飞的窗口、**接不动**自己上一轮留下的窗口、出错时**不关**窗口、
退出时**不管**窗口。这版四条全补上，另外把 PDF 渲染改成无头模式 —— 它以前会顺手弹
一扇可见的浏览器窗，同样会被算进「怎么开了这么多窗」。

## 四个根因

1. **普查看不见刚起飞的窗口**（`xdao/browser_login.py`）。`live_browser_dirs()` 的判据是
   「资料目录里写的那个调试端口答不答话」，而 Edge 首次启动要装扩展、拉组件，端口文件写完
   到端口真答话之间有十几秒空档。这段时间里普查数出「没有窗口」，于是又开一扇。
   日志里 `[17:49:31] 开窗前普查：没探到活着的程序窗口` 之后 18 秒的「读罐对账」却列出
   三个同族资料目录，就是这个时间差。
2. **进程侧普查会把正在启动的窗当僵尸杀掉**（`_adopt_process_side_findings()`）。它对
   「进程还在、端口没答话」的目录直接 `_kill_processes_using_profile()`，正在启动的那扇窗
   会被杀在摇篮里，下一扇窗启动时浏览器就弹「Microsoft Edge 意外关闭 / 还原页面」。
3. **接不上的窗口没人负责收尾**（`stop()`）。`_try_attach_live()` 认领来的窗口 `process` 是
   `None`，`stop()` 只 kill 自己 `Popen` 的 pid，于是接手的窗口在程序退出后原地留成孤儿；
   实测留下过 598 MB、23 个浏览器进程、端口还活着的一扇。
4. **主窗口关掉时登录对话框直接失联**（`xdao/gui.py`）。对话框只挂了 `destroy` 收尾，主窗
   被 × 掉 / 程序退出时不走取消流程，子浏览器没人收。另外 `_worker()` 的异常分支只报错、
   不释放已经建好的浏览器句柄，失败重试一次就多一扇窗。

## 修法

- **宽限重探**：新增 `wait_for_attachable_window(profile, *, wait=STARTING_WAIT, poll=STARTING_POLL, ...)`；
  `STARTING_GRACE = 120.0`（端口文件 120 秒内写过就算「可能还在启动」）、
  `STARTING_WAIT = 12.0`、`STARTING_POLL = 0.5`。开窗前先按「有自家进程 + 端口文件新」等最多
  12 秒，让慢启动的那扇窗答上话，接上就不再开新的。
- **普查放过正在启动的窗**：`_is_starting_window()` 判定的目录从「杀掉」改成「补进候选」，
  交给宽限重探去接。
- **收尾必杀**：`stop()` 在自己进程之外，再按本轮真正使用的资料目录
  `_kill_processes_using_profile()` 兜一刀（只碰 `xdao-export-browser-profile*` 这类自家目录，
  用户平时的浏览器不沾）；新增 `sweep_orphan_profiles()`，程序启动与退出时各扫一次自家残留窗。
- **界面收尾**：`BrowserLoginDialog` 把自己登记进 `App._login_dialogs`；`App.on_close()` 先让
  每个对话框走「取消」这条正路（停线程 + 关浏览器），再延后 1.2 秒销毁主窗；`_worker()` 的
  异常分支改成 `_release_browser(browser)`。
- **PDF 无头**：`xdao/exporters/pdf.py` 起浏览器时 `attach_live=False, headless=True`
  （启动参数多一个 `--headless=new`），不再顺手弹窗，打印结果不变。

## 用例与基线

- `tests/test_browser_login.py` 343→353（宽限重探三条、普查放过启动中一条、收尾必杀一条、
  接活窗口两条、`build_args --headless=new`、孤儿清扫一条）
- `tests/test_gui_entry.py` 98→103（× 绑到 `App.on_close`、对话框登记、关主窗收尾、失败释放）
- 收集 **2088 项**、本机 **2081 passed / 7 skipped**（跳过的仍是显式开关的真机用例）

## 附件

- `xdao-export-v0.17.3-win64.zip`：免安装包（解压后双击 `xdao-export.exe`）
- `xdao-export-v0.17.3-win64.zip.sha256`：上面那个包的 SHA256 校验文件

## 自查

- 全量 `pytest -q` 全绿；契约套件（README / HANDOFF 计数、发布说明、公开材料、版本一致）全绿
- 打包版：`--version`、`--selftest --offline`、`--check-browser` 都过
- 现场清理过一遍：`%TEMP%\xdao-export-browser-profile-*` 与 `%APPDATA%\xdao-export\browser-profile`
  都删掉后，本版重新登录**只开一扇**窗
