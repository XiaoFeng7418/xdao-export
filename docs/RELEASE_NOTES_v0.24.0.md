# v0.24.0：B 岛公开串支持

本版为 xdao-export 增加 B 岛（`bog.ac`）公开串支持。输入 B 岛串网址会自动识别，支持分页抓取、断点缓存、五种导出格式和串监控；不需要 B 岛账号或登录。

站点适配器统一解析 X 岛与 B 岛数据。B 岛每条楼层的多图、毫秒时间戳（按 UTC+8 展示）和 `>>Po.N` / `>>N` 引用会在 HTML、PDF、EPUB、Markdown 与 TXT 中按各自格式处理。B 岛缓存和监控条目使用独立命名空间，避免与同号 X 岛串互相覆盖；X 岛旧缓存路径和输出格式继续兼容。

命令行新增 `--site auto|xdao|bog`，默认自动识别。所有联网行为仍遵循既有节流、重试和取消机制，测试使用离线假 HTTP 数据。

## 附件

- `xdao-export-v0.24.0-win64.zip` 是 64 位 Windows 免安装包。解压后双击 `xdao-export.exe`，无需安装 Python。
- `xdao-export-v0.24.0-win64.zip.sha256` 是配套的 SHA256 校验文件。
