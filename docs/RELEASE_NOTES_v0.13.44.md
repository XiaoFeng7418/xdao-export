# v0.13.44：把「分区饼干」这件事的说法改准

## 现象

上一版（v0.13.42）为了解释「同一扇窗的 F12 里看得见 `userhash`、程序却读不到」，
把原因写成了：**`Network.getAllCookies` 已知不回首带 `Partitioned` 标记的分区饼干
（CHIPS）**，所以只有 `Storage.getCookies` 问得出来。

这句话来自传闻，v0.13.42 当时**没有真机证据**。这一版补了复验，结论是它不成立。

## 真机复验（2026-10-04，本机 Edge）

做法：headless 起一份一次性资料目录（跑完删），用 `Network.setCookie` 往罐里种两块
饼干 —— 一块不分区（`plain-control`）、一块带 `partitionKey`（`chips-b`）—— 再用三条
读法读回来。只打印名字与「是否带 partitionKey」，值一个字符都不打印。

| 读法 | 读回来的名单 |
| --- | --- |
| 按地址读 `Network.getCookies` | `plain-control`（**看不见分区饼干**） |
| 整罐读 `Network.getAllCookies` | `plain-control`、`chips-b`（带分区） |
| 存储读 `Storage.getCookies` | `plain-control`、`chips-b`（带分区） |
| 并集 `read_all_cookies()` | `plain-control`、`chips-b`（带分区） |

顺带量到一条：种分区饼干时 `Network.setCookie` 的 `partitionKey` 必须**同时**给出
`topLevelSite` 与 `hasCrossSiteAncestor`，只给前者会被浏览器拒绝（`Invalid parameters`）。

## 改法（只改文字）

并集**留着**：两条路的名单本来就可能不一样（按地址读受域/路径/Secure 过滤，不同
Chromium 版本对分区饼干的处理也不一致），合起来再加上对账行里的 `存储读=` 段，下一张
截图就能直接看出是哪条路漏。只是理由从「另一条一定瞎」改成「不赌浏览器版本」。

| 落点 | 改了什么 |
| --- | --- |
| `xdao/cdp.py` | `read_storage_cookies()` 与 `read_all_cookies()` 的文档字符串 |
| `xdao/browser_login.py` | `read_site_cookies()` 与 `jar_forensics()` 的文档字符串 |
| `tests/test_browser_login.py` | 两条用例的文档字符串 |
| `packaging/使用说明.txt` | v0.13.42 那一段 |
| `docs/RELEASE_NOTES_v0.13.42.md` | 加「v0.13.44 更正」一行，线上那页正文也重新同步 |
| `MAINTENANCE.md` | v0.13.42 条目里的同一处说法 + 新增 v0.13.44 条目 |

功能、界面、导出结果与 0.13.43 完全一样。

## 真机核验

- 上面那张表就是本机跑出来的结果（探针跑完把一次性资料目录删掉，没留在磁盘上）；
- 逐文件字节复查：`packaging/使用说明.txt` 仍是 UTF-8 BOM + 纯 LF，`README.md` /
  `MAINTENANCE.md` / `HANDOFF.md` 仍是 CRLF 且没有裸 LF，`xdao/*.py` 仍是纯 LF；
- 全量用例仍 **1813 passed / 7 skipped**（收集 1820 项）；
- 契约用例（文档事实、版本号一致、使用说明分段、文本卫生、打包清单）全绿。

## 登录还是不行的话怎么自救

v0.13.42 起「读罐对账」那一行多一段 `存储读=`，三种形状对应三种毛病：

1. `存储读=` 有 `userhash`、`整罐读=` 没有 → 两条读法名单不一致，把这一行贴回来；
2. 两边都没有 `userhash` → 多半是**读错了罐**：看同一行的 `罐=`、`接=端口…` 与窗口横幅
   末尾的【窗口号】，确认程序接的是不是你在看的那扇窗；
3. `原始userhash=` 有、`合并=` 没有 → 是域/路径过滤把它挡住了，也把这一行贴回来。

## 附件

- `xdao-export-v0.13.44-win64.zip`：免安装包，解压后双击 `xdao-export.exe`。
- `xdao-export-v0.13.44-win64.zip.sha256`：上面那个包的 SHA256 校验文件。
