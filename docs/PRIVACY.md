# Privacy policy · 隐私说明

_Last updated 2026-10-02 · 最近更新 2026-10-02_

## English

Xdao Export (X島串导出工具) is a local desktop application. **It collects nothing about you, and
sends nothing about you to us.** There is no analytics, no advertising, no crash reporting and no
telemetry of any kind.

What the program stores, and where:

- **Login cookies** — the `userhash` you paste in, or the cookie the built-in browser login brings
  back — are written to this program's own configuration file on your computer, and nowhere else.
  They are only ever sent to the X島 imageboard itself (nmbxd.com / nmbxd1.com), the site you asked
  the tool to read from.
- **Exported documents, downloaded images and the page cache** stay in the folders you chose.

Network access, exhaustively:

- Requests to the X島 imageboard (nmbxd.com / nmbxd1.com) to read threads, pages and images. All of
  them are read-only `GET` requests; the tool never posts, never changes account settings, and
  never touches another account.
- At most one request to `api.github.com`, asking whether a newer version exists. It runs once at
  startup, and again only when you press “检查更新”. It sends no identifiers, and the command-line
  flag `--offline` turns it off completely.
- Nothing else. There is no other outbound connection.

Deleting your data: the configuration file, the cache and the export folders are ordinary files on
your machine. Deleting them — or using the program's own cache-clearing commands — removes
everything the tool has ever stored.

Contact: open an issue at <https://github.com/XiaoFeng7418/xdao-export/issues>.

## 中文

X岛串导出工具是一个**只在你本机运行**的桌面程序。它**不收集、也不向外发送任何关于你的信息**：
没有统计、没有广告、没有崩溃上报，也没有任何形式的遥测。

程序会存下来的东西，以及存在哪儿：

- **登录饼干**（你粘贴的，或内置浏览器登录取回的 `userhash`）只写进本程序自己的配置文件，留在
  你自己机器上；除了 X岛揭示板本身（nmbxd.com / nmbxd1.com），不会发给任何第三方。
- **导出的文件、下载的图片与网页缓存**都在你指定的目录里。

联网只有这两种，没有第三种：

- 向 X岛揭示板读串、读页、下图，全部是只读的 `GET` 请求：不发帖、不改账号设置、不碰别人的账号。
- 最多一次问 `api.github.com` 有没有新版本：启动时一次，之后只有你点「检查更新」才会再问；
  不带任何身份信息，命令行加 `--offline` 可以彻底关掉。

删除你的数据：配置文件、缓存、导出目录都是你机器上的普通文件，删掉它们（或用程序自带的清理
命令）就等于删掉了这个工具存过的所有东西。

联系方式：在 <https://github.com/XiaoFeng7418/xdao-export/issues> 提一条 issue。
