# Privacy policy · 隐私说明

_Last updated 2026-11-21 · 最近更新 2026-11-21_

## English

Xdao Thread Exporter runs locally. It has no analytics, advertising, telemetry or automatic crash reporting. It does not upload exports, settings or login details to the project maintainer.

### Data stored on the computer

- **Login cookies:** the active `userhash` is saved **unencrypted** in the app's configuration file. Account passwords are not saved there. Browser login uses a separate profile managed by the app, which may retain site login data for reuse; it does not read the everyday browser profile.
- **Exports and caches:** exported documents, downloaded images and page caches are stored in the selected folders. Settings also record preferences and watch lists.
- **Diagnostics:** local logs and browser-login diagnostic files can include account cookies, URLs and local paths. Check and redact these files before sharing them. No diagnostic file is submitted automatically.

### Network activity

- Thread export and monitoring request pages and images from the site and image hosts referenced by those pages. Login cookies are scoped to the site's domains, including `nmbxd1.com` and `api.nmb.best`; they are not attached to GitHub update requests.
- Login and cookie activation also contact the site's account service. Account/password login submits credentials and CAPTCHA data to that service; login is not a read-only GET operation. Browser login opens the site's pages in a local Chromium browser.
- The GUI checks GitHub release information at startup when its local cache needs refreshing; manual update checks and command-line update checks also contact GitHub. An upgrade started in the app downloads the release asset from GitHub's download infrastructure. These requests do not include site login cookies or exported thread content.
- Online self-checks test site connectivity. `python main.py --selftest --offline` omits those network checks; **`--offline` is a self-check option, not a global network firewall**.

The services receiving requests still receive ordinary connection information, such as the source IP address and HTTP headers. Any configured proxy and the external browser have their own data handling policies.

### Removing local data

Configuration, app-managed browser profiles, caches, logs and exports are ordinary local files. Close the app and its login browser before removing them. Deleting only the configuration file does not delete exports, caches or browser profiles. See the [user guide](USAGE.md) for configuration and cache locations.

For support, [open an issue](https://github.com/XiaoFeng7418/xdao-export/issues) with a minimal, redacted example. Never post passwords, cookies or private thread content.

## 中文

X岛串导出工具在本机运行，**没有统计、广告、遥测或自动崩溃上报**，不会向项目维护者上传成品、配置或登录信息。

### 本机保存的数据

- **登录饼干：**当前 `userhash` 以**未加密明文**写入程序配置文件；配置文件不保存账号密码。浏览器登录使用程序管理的独立资料目录，可能保留站内登录状态以供复用，不读取日常浏览器的资料目录。
- **成品与缓存：**导出文件、下载图片与页面缓存保存在选定目录；配置还记录偏好与监控列表。
- **诊断信息：**本地日志与浏览器登录诊断文件可能包含饼干、网址和本机路径。分享前应检查并脱敏；程序不会自动提交这些文件。

### 联网范围

- 导出与监控会向站点请求串、页面，以及页面引用的图片资源。登录饼干限定在站点域名内使用，包括 `nmbxd1.com` 与 `api.nmb.best`，不会附加到 GitHub 的更新请求。
- 登录与应用饼干还会访问站点账号服务。账号密码登录会向该服务提交账号、密码和验证码，**并非所有请求都是只读 GET**；浏览器登录会在本机 Chromium 浏览器中打开站点页面。
- 图形界面在启动时按本地缓存情况查询 GitHub 发布信息；手动检查更新与命令行查版本同样会访问 GitHub。主动启动升级后，还会从 GitHub 的下载服务获取发布包。这些请求不包含站内登录饼干或导出内容。
- 联网自检会测试站点连接。`python main.py --selftest --offline` 省略这些联网检查；**`--offline` 仅用于离线自检，不是全局禁止联网开关**。

接收请求的服务仍会获得源 IP、HTTP 请求头等正常连接信息；配置的代理与外部浏览器也有各自的数据处理规则。

### 删除本机数据

配置、程序管理的浏览器资料目录、缓存、日志与成品都是普通文件，关闭程序及登录浏览器后可分别删除。仅删除配置文件不会连带删除成品、缓存或浏览器资料目录。位置说明见[使用指南](USAGE.md)。

需要支持时，可提交[问题反馈](https://github.com/XiaoFeng7418/xdao-export/issues)，提供最小化、已脱敏的示例；不要附带密码、饼干或私密串内容。
