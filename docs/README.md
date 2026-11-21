# 文档导航 · Documentation

[简体中文首页](../README.md) · [English overview](../README.en.md) · [下载免安装包](https://github.com/XiaoFeng7418/xdao-export/releases/latest)

首次使用从[使用指南](USAGE.md)开始；英文功能、下载与隐私摘要见 [English README](../README.en.md)。详细使用与维护文档目前以中文为主。

## 使用与信任

| 文档 | 内容 |
| --- | --- |
| [使用指南](USAGE.md) | 登录、格式与图片模式、CLI 参数、监控、缓存和排错 |
| [隐私说明 · Privacy policy](PRIVACY.md) | 中英文隐私说明、Cookie 保存及联网边界 |
| [代码签名现状](SIGNING.md) | 当前未签名状态、申请记录和待启用流程 |
| [MIT License](../LICENSE) | 项目许可与版权 |

界面预览：[浅色模式](screenshot.png) · [深色模式](screenshot-dark.png)。截图展示 v0.24.0 的真实界面，使用演示配置。

## 版本与维护记录

- [v0.24.0 发布说明](RELEASE_NOTES_v0.24.0.md)：当前版本说明与附件名称。
- [v0.23.0 发布说明](RELEASE_NOTES_v0.23.0.md)：上一版的界面与文件名修复记录。
- [全部发布版本](https://github.com/XiaoFeng7418/xdao-export/releases)：下载及历史变更；对应源码说明保留在本目录的 `RELEASE_NOTES_v*.md`，不合并删除历史。
- [维护规范](../MAINTENANCE.md)：架构、界面设计系统、公开材料约定、测试与发布护栏。
- [技术交接](../HANDOFF.md)：功能边界、模块职责、完整测试清单。

## 既有审计资产

以下报告记录 2026-11-15 的观察，属于历史证据，不等于当前版本仍有相同问题。开始修改前应先复核实现及后续维护记录。

| 报告 | 范围 |
| --- | --- |
| [01 安全审计](backlog/01-security-audit.md) | 命令、路径、压缩包、Cookie 与线程生命周期 |
| [02 导出格式矩阵](backlog/02-export-format-matrix.md) | 五种格式、图片模式与边界内容 |
| [03 失败模式](backlog/03-failure-modes.md) | 网络异常、写盘失败、取消与输入边界 |
| [04 依赖边界](backlog/04-dependency-boundary.md) | 标准库、Tk、浏览器、打包与第三方许可 |
| [05 配置兼容](backlog/05-config-backward-compat.md) | 旧配置、未知键、枚举与文件名模板 |
| [06 首次使用](backlog/06-first-use-walkthrough.md) | 下载、启动、登录与首次导出的卡点 |
| [07 CI 评估](backlog/07-ci-assessment.md) | 当时的自动检查、构建与仓库设置 |

## 工程工具放在哪里

| 路径 | 用途 |
| --- | --- |
| [`tools/gui_shot.py`](../tools/gui_shot.py) | Windows 主窗口截图；只抓主窗口，不含弹窗 |
| [`tools/build_zip.py`](../tools/build_zip.py) | 构建免安装包并生成 SHA256 附件 |
| [`tools/repo_check.py`](../tools/repo_check.py) | 只读比对 GitHub 仓库、Release 与本地状态；需要网络与认证 |
| [`tools/clean_scratch.py`](../tools/clean_scratch.py) | 检查/清理测试残留，先使用 `--dry-run` 阅读目标 |
| [`诊断写入.ps1`](../诊断写入.ps1) / [`双击入口`](../诊断写入-双击运行.cmd) | 随包写入诊断工具，保留在根目录以维持打包引用 |
| [`.github/`](../.github/) | CI 工作流、issue 表单与 PR 模板 |

这些路径保持现状；使用工具前先读脚本说明。维护报告与历史发布说明通过导航归类，不移动随包工具或重写历史记录。
