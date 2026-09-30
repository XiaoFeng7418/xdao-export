# v0.4.0：监控到新回复时弹桌面通知

发布时间：2026-09-30

这一版把路线图上的「桌面通知」做完了：串监控发现有新回复时，不再只有日志里的一行字，
而是会弹一条 Windows 通知（带提示音），挂着监控去干别的事也不会漏掉更新。

## 新增

- **监控桌面通知**：监控模式下发现有新楼时弹系统通知，内容形如
  「X岛串导出工具：串有更新，已自动导出 / No.67024789 新增 12 楼（共 638 楼）/ 已保存：…」。
  - 界面上多了「监控时弹桌面通知」勾选框，默认开启；命令行加 `--no-notify` 可关掉。
  - 同一个串 15 分钟内只提醒一次（可用配置项 `notify_interval` 改），不会每轮都刷屏。
  - **首次导出不提醒**：那是你自己刚添加的监控，一次加十个串不该弹十条通知。
  - 通知发不出去（没装 PowerShell、没有音频设备、系统禁用通知）只会写进日志，
    绝不影响导出本身。
- **提示音**：Windows 的 Toast 默认是静音的，所以弹完通知会再响一声提示音。
- **真机集成用例** `tests/test_live_notify.py`：默认跳过，设 `XDAO_LIVE_NOTIFY=1`
  才跑。它会连真实接口抓一次官方测试串 No.50000001，在内存里伪造 3 个新楼层，
  走完「监控判定有更新 → 导出 → 发通知」的完整链路。

## 实现说明（为什么这么做）

- 仍然**零第三方依赖**：Windows 走 PowerShell 调 WinRT 的
  `Windows.UI.Notifications`，macOS 走 `osascript`，Linux 走 `notify-send`。
- **必须用哨兵 AppUserModelID**：Windows 只给「注册过 AppUserModelID」的程序显示通知，
  直接用应用名（`CreateToastNotifier("X岛串导出工具")`）时通知中心什么都收不到——这是实测结果，
  不是推测。所以默认用系统自带的
  `{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe`，
  免去给 exe 建开始菜单快捷方式的麻烦。
- 通知在主线程（界面的 `_poll_watch`）里发，监控线程只管解析结果，两边互不拖累。

## 顺手修掉的打包问题

- **打包版现在能连回父控制台**：GUI 子系统的 exe 从 cmd 里跑 `xdao-export.exe --version`
  时，之前什么都不显示，还会冒出 `OSError: [Errno 22] Invalid argument`。现在只要命令行里
  出现 `--version` / `--help` / `--selftest` 这类开关，程序就先 `AttachConsole` 到父控制台
  再重新打开标准流；双击启动（没有父控制台）时什么也不做，界面照旧。

## 下载哪个

- **推荐**：`xdao-export-v0.4.0-win64.zip`（免安装包）。解压后双击里面的 `xdao-export.exe`
  就能用，不需要装 Python；`_internal` 文件夹要和 exe 放在一起，别单独把 exe 拖出来。
- 单文件版：`xdao-export-v0.4.0.exe`。只有一个文件、方便携带，但它在系统临时目录
  不可写的环境里会打不开（报 `Could not create temporary directory!`）。遇到这种情况
  请改用上面的免安装包。

## 升级提示

- 配置项新增 `notify`（默认 `true`）与 `notify_interval`（默认 `900` 秒）；
  旧配置文件直接兼容，不需要手动改。
- 打包版仍然无法导出 PDF（见 v0.3.1 说明），需要真 PDF 请从源码运行。

## 测试

- 离线用例 **258 项通过、1 项跳过**（跳过的是上面那个真机用例）。
- 新增 `tests/test_notifications.py`（27 项）：转义、Windows/macOS/Linux 三条命令、
  哨兵 AUMID、提示音、节流、异常吞掉、与监控结果的衔接。
- 新增 `tests/test_cli.py` 里两项关于控制台的用例：只有命令行用法才去连控制台、
  源码运行时不动标准流。
- 本机实机验证：伪造 3 个新楼层后，监控判定「新增 3 楼」并成功弹出系统通知。
