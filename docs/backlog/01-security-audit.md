# 安全审计（只读）

审计对象：`xdao/**.py`、`main.py`、`tools/**.py`（不含 `tests/`，测试里打桩用的假实现不算攻击面）。
审计日期：2026-11-15。审计方式：全仓正则普查 + 逐处读码 + 20 条针对性实测（临时脚本，不落盘）。
结论口径：**"风险" 写的是"如果这里被喂了恶意输入会怎样"，不是"现在有漏洞"**。

## 一、总览

| 面 | 结论 |
| --- | --- |
| 命令执行 | 无 `eval`/`exec`/`compile`/`os.system`/`shell=True`；`subprocess` 43 处全是 `list[str]` 直接传参 |
| 路径拼接 | 全走 `pathlib`；`os.path.join` 零处；输出文件名一律先过 `sanitize_filename` |
| 压缩包解压 | `updater.extract_payload` 自己做了路径校验（等价或严于 `filter="data"`） |
| 网络输入 | URL 里的 CR/LF 由 `http.client` 拦死；响应体有 `max_bytes` 上限；gzip/deflate 解不开就原样返回 |
| 凭据 | 饼干只写往站内四个域；`userhash` 明文存本机配置文件（见 §5.2） |
| 界面线程 | 13 处工作线程全是 `daemon=True` 且只往队列里放东西，Tk 只在主线程碰 |
| 意外发现的两处缺口 | ① Windows 设备名未挡（导出会失败，报错难懂）② 关窗时后台线程可能被硬切（PNG 截图类残留） |

## 二、命令执行

### 2.1 危险函数普查结果为零

对 `xdao/**.py` + `main.py` + `tools/**.py` 逐行正则扫描，下列模式**命中 0 次**：
`eval(`、`exec(`、`compile(`、`os.system`、`shell=True`、`mktemp(`、`yaml.load`、`pickle`、`tarfile`、`extractall`、`urlretrieve`、`input(`。

`re.compile` 是唯一的 `compile(` 字形出现处（正则编译，不属于动态执行）。

### 2.2 `subprocess` 全部走参数列表

43 处调用分布在 7 个模块：`browser_flags`(1)、`browser_login`(25)、`github_api`(4)、`gui`(1)、`notifications`(3)、`updater`(4)、`exporters/pdf`(5)。全部是 `subprocess.run([...])` 或 `Popen([...])` 形式，没有一处把字符串交给 shell 解释。

逐个看过参数来源，都落在"常量 + 本机路径"里，没有一个参数直接来自页面正文：

| 位置 | 参数来源 | 风险 |
| --- | --- | --- |
| `xdao/gui.py:3886 _spawn(command)` | 只在 `:5181-5183` 用，三路兜底（`os.startfile` / `explorer folder` / `explorer /select,folder`），`folder` 是用户自己选的目录 | 低 |
| `xdao/updater.py:236-244` | `[*(prefix or []), str(exe), "--selftest", "--offline"]`，`exe` 是本进程路径 | 低 |
| `xdao/updater.py:426-433` | `["--apply-update", str(plan.target), str(payload), str(plan.pid), str(plan.launcher)]`，全部来自更新计划对象 | 低 |
| `xdao/exporters/pdf.py:234-251` | 浏览器路径 + 常量 flag + `source.as_uri()`（本地文件 URL） | 低 |
| `xdao/browser_login.py:1255 build_args(...)` | `info.path`（探测到的浏览器）+ 常量 flag + `start_url`（常量） | 低 |
| `xdao/notifications.py:125 _call(argv)` | `_notify_windows`/`_notify_macos` 构造的常量 argv | 低 |
| `xdao/github_api.py:76` / `:306` / `:319` | `[gh, "auth", "token"]`、`["git", *args]`，`args` 是本模块给定的常量序列 | 低 |

`xdao/notifications.py` 另有一层字符串转义，用于把串标题写进通知正文：

- `:193 _xml_escape` 转 `& < > " '`（toast XML）
- `:204 _ps_quote` 单引号翻倍（PowerShell）
- `:209 _applescript_quote` 转 `\` 与 `"`（osascript）
- `:213 _encode_command` 走 `base64(script.encode("utf-16-le"))` + `-EncodedCommand`

即四条通道都不做字符串拼接注入。

## 三、路径与文件

### 3.1 输出文件名：`sanitize_filename`

`xdao/exporters/_shared.py:26` 的 `_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')`，`:43 sanitize_filename(name, fallback="xdao-thread")` 依次做：替换非法字符 → `strip(" .")` → 截到 80 字符 → 空则回退。

调用链在 `xdao/exporters/base.py`：
- `:95 output_name()` → `render_filename(self.filename_template, thread, derive_filename(thread))`
- `:99 target_path()` = `Path(output_dir) / (sanitize_filename(self.output_name(thread)) + self.suffix)`
- `:112 merged_path()` 同构（`sanitize_filename(self.merged_name(threads)) + self.suffix`）

⇒ 用户自定义命名模板里的 `/`、`:`、`..` 都会先被替换，**构成不了路径穿越**。

实测（20 条边界里的 6 条）：

| 输入 | 输出 | 判定 |
| --- | --- | --- |
| `..` | `xdao-thread` | 挡 |
| `../../etc/passwd` | `_.._etc_passwd` | 挡（`/` 变 `_`） |
| `C:\Windows\x` | `C___Windows__x` | 挡 |
| `CON` | `CON` | **未挡** |
| `PRN` / `AUX` / `NUL` / `COM1` / `LPT1` | 原样 | **未挡** |
| `a\x00b` | 原样 | 到 `Path.write_text` 才 `ValueError: embedded null character` |

### 3.2 缺口一：Windows 设备名

**现象**：串标题恰好是 `CON`/`PRN`/`AUX`/`NUL`/`COM1`~`COM9`/`LPT1`~`LPT9` 时，导出成品名就是设备名。

**实测**：

- `(目录/"PRN.txt").write_text(...)` → `FileNotFoundError`
- `os.replace(part, 目录/"PRN.html")` → `FileExistsError [WinError 183]`（CON/AUX/NUL/COM1 全中）
- 带扩展名的 `CON.txt` / `NUL.txt` 反而能写（Windows 的历史怪癖）

**风险等级**：低。不是崩溃、不是安全问题（写不到设备里去），只是**导出失败且报错看不懂** —— 用户看到 `[WinError 183] 当文件已存在时，无法创建该文件` 会以为是自己目录有问题。

**为什么这轮没改**：`sanitize_filename` 已有 6 条既有断言钉着（`tests/test_exporters.py:63-79`），改它就等于改现有行为，而本轮约束是"不动现有 `.py`"。

**建议改法**（下一轮做）：
1. 在 `sanitize_filename` 里加一张设备名表（`CON PRN AUX NUL COM1..COM9 LPT1..LPT9`，大小写无关，比对去掉扩展名后的主名）；
2. 命中时前缀下划线或后缀 `_`，例如 `CON` → `_CON`；
3. 补一条用例：`for name in ("CON","prn","AUX","NUL","COM1","LPT9"): assert sanitize_filename(name) not in DEVICE_NAMES`；
4. 顺带处理 `a\x00b`：`sanitize_filename` 里把 `\x00` 换成 `_`，比让 `Path.write_text` 抛 `ValueError` 友好。

### 3.3 缓存目录：串号只能是整数

`xdao/cache.py:352 parse_thread_id(value)`：挡 `bool`、`int` 必须 `> 0`、正则 `_THREAD_ID_RE = r"(?:^|/t/|/thread/|/id/)(\d{3,})"`（`:349`）、`value.isdigit()` 兜底。

⇒ 缓存路径 `pages/{thread_id}/`、`threads/{thread_id}.json`（`:141`、`:137`）里只可能是纯数字，**没有穿越空间**。

`xdao/threadlist.py:57 read_thread_list(path)`：`read_text(encoding="utf-8-sig")`，分别捕 `FileNotFoundError` / `IsADirectoryError` / `UnicodeDecodeError` / `OSError`（`:66-76`），坏文件不会炸。

### 3.4 图片落盘

`xdao/exporters/html.py:208 _image_file_name(url, data)`：从 URL path 取 stem → `sanitize_filename(stem, fallback="")` → 空则 `img{序号:03d}` → 重名加 `-{serial}`。`:228 _write_image_file` 写 `self._image_dir / name`，失败返回空串（不中断导出）。

`xdao/client.py:848 _cache_path(url)`：URL 的 sha1 十六进制、`digest[:2]` 两级目录、后缀从 URL 白名单 `\.(jpg|jpeg|png|gif|webp|bmp)` 取，兜底 `.img`（`:851-857`）⇒ 缓存文件名**不可控**，无穿越。

### 3.5 取消时不留半份成品

`xdao/exporters/html.py:549-566 _save_streamed`：先写 `path.name + ".part"` → `write(part)` → `check_cancel()` → `os.replace(part, path)`；`finally` 里 `if part.exists(): part.unlink()`（`except OSError: pass`）。

`xdao/client.py:832 download_image`：`tmp = path.with_suffix(path.suffix + f".{threading.get_ident()}.part")`，`except OSError` 里 `tmp.unlink()`（`:842-845`）—— 两线程抢同一目标时失败的那个会收掉自己的临时文件。

## 四、网络输入

### 4.1 URL 里的 CR/LF 注入：标准库挡死

实测 `client._request('http://127.0.0.1:9/\r\nX-Injected: 1', retries=0)`：

```
http.client.InvalidURL: URL can't contain control characters.
'/\\r\\nX-Injected: 1' (found at least '\\r')
```

`urllib.request.Request` 构造期放行，真正发请求时被 `http.client` 拒。`_request` 的 `except OSError` **不覆盖** `InvalidURL`（它是 `HTTPException` 子类），所以异常原样抛给调用方 —— 结果是"报错"，而不是"带着注入头把请求发出去"。

**建议（可选）**：在 `_request` 入口加一条 `if "\r" in url or "\n" in url: raise XdaoError(...)`，把 `InvalidURL` 换成说得清的提示。优先级低。

### 4.2 响应体上限

`xdao/client.py:222 _request(..., max_bytes=None)`：给了上限时 `raw = resp.read(max_bytes + 1)`，超了抛 `XdaoError(f"响应内容超过 {max_bytes // 1024} KB 上限：{url}")`（`:265`）。

调用方：图片走 `MAX_IMAGE_BYTES = 24 * 1024 * 1024`（`:29`、`:827`）；升级包走 `MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024`（`xdao/updater.py:50`，在 `download_asset` 里边下边数，`:152` 超了中止）。页面 JSON 没有设上限 —— 站点单页响应是固定量级，判定可接受。

### 4.3 解压与解码

`xdao/client.py:35 _decompress(data, encoding="")`：先看 `GZIP_MAGIC = b"\x1f\x8b"`（`:32`），再按 encoding 分 gzip / deflate（deflate 先 `zlib.decompress`，失败再试 `wbits=-zlib.MAX_WBITS` 的裸 deflate），认不出或坏包**原样返回**。实测 13 组边界（空、纯文本、错声明、截断、5 MB、大写 `GZIP`、`gzip; charset=utf-8`、双层 gzip 只解一层）全部符合预期，无异常泄漏。

### 4.4 升级包解压

`xdao/updater.py:172 extract_payload(archive, into)`：

1. 取顶层名集合，`payload_names = sorted(n for n in roots if n.startswith(PAYLOAD_PREFIX))`，数量不是 1 就抛 `UpdateCheckError("升级包里没找到程序目录")`（`:191`）；
2. 逐 member 检查：`parts[0] != payload` 跳过、`any(part in ("..", "") for part in parts)` 抛 `UpdateCheckError("升级包里有不安全的路径")`（`:199-200`）；
3. `zipped.extract(name, into)`（`:205`）；
4. `BadZipFile` → `UpdateCheckError("升级包不是有效的 zip")`，`OSError` → `UpdateCheckError(f"解压失败（{exc}）")`。

没用 Python 3.12 的 `extractall(filter="data")`，但自带检查更严（额外要求"唯一顶层程序目录"）。**结论：无需改动**；可选优化是额外拒绝绝对路径与符号链接成员（`name.startswith("/")`、`member.external_attr >> 16 & 0o170000 == 0o120000`），属加固而非补漏。

## 五、凭据

### 5.1 饼干只发往站内四个域

`xdao/client.py:446 set_userhash`：先 `_drop_cookies_named("userhash")` 扫掉同名旧值，再对 `("nmbxd1.com", ".nmbxd1.com", "api.nmb.best", ".api.nmb.best")` 各设一条 `Cookie(...)`（`discard=True`、`secure=False`）。

`xdao/client.py:508 import_cookies(cookies)`：收 CDP `Network.getCookies` 的结果，名字/值为空跳过，装之前先删同名旧值。

⇒ 图片域名 `image.nmb.best` **不在**这四域里，所以饼干不会跟着图片请求出去。

### 5.2 `userhash` 明文存本机配置

`xdao/settings.py:264 save()` 的字典里有 `"userhash": self.userhash`，`:300 self._path.write_text(...)`。

**风险**：同机其他账户若对该目录有读权限，能直接读到饼干。Windows 下 `%APPDATA%` 默认只允许本账户访问，判定**可接受**。

**建议（可选，优先级低）**：写配置时 `os.chmod(path, 0o600)`（Windows 上不生效但 POSIX 有效），或在设置界面标注"饼干以明文保存在本机配置里，别把配置目录共享出去"。

### 5.3 无硬编码凭据

全仓 `ghp_` / `github_pat_` / `Bearer` 形状的字面量 0 处；发布脚本从 Credential Manager 取令牌（`tools/push_one_commit.py` 等），仓库里只有用户名占位。

## 六、界面线程与生命周期

### 6.1 线程纪律

`xdao/gui.py` 共 13 处 `threading.Thread(..., daemon=True)`：633（登录窗取验证码）、682（登录窗提交登录）、1261（登录浏览器 `_worker`）、3830（浏览器体检）、3982（清旧备份）、3986（清临时资料目录）、3989（`after(3000, ...)` 里起 `_cleanup_staging_leftovers`）、4887（检查更新）、4986（升级）、5367（`pick_cookies`）、5752（导出）、5990（`watch_forever`）、6044（`check_once`）。

**全部**只做两件事：调纯函数 / 把结果 `queue.put(...)` 进队列；Tk 控件只在主线程碰。源码注释也把这个规矩写死了（`:4884-4886`、`:4976-4977`：「Tk 的对象只能在主线程里碰，工作线程直接调 after() 会撞上 RuntimeError: main thread is not in main loop」）。

8 个 `_poll_*` 定时器负责消费队列：`_poll_form`(639)、`_poll_login`(685)、`_poll_browser_check`(3840)、`_poll_update`(4902)、`_poll_upgrade`(5053)、`_poll_cookies`(5370)、`_poll_export`(5818)、`_poll_watch`(6047)。

### 6.2 缺口二：关窗时后台线程被硬切

`xdao/gui.py:5765 on_close()`：

1. `_closing = True`
2. `cancel_export()`（只 `stop.set()`，不 join）
3. `stop_watching()`（`stop_event.set()` 后立刻把 `_watching = False`）
4. 逐个 `dialog._on_cancel()`
5. `self.root.after(1200, self._destroy_root)` → `root.destroy()`

`:5789-5790` 的注释写明这 1.2 秒的用意：「给导出线程一点收手时间：它手里可能正开着一个 PDF 用的浏览器，进程直接退出会把它留成孤儿窗」。

**风险**：如果 1.2 秒不够（PDF 渲染慢、图片下载卡在超时上、抓页正在等 `Retry-After`），主线程 `destroy()` 后进程退出，daemon 线程被直接杀掉 —— 此时：

- `.part` 临时文件可能留在输出目录（`_save_streamed` 的 `finally` 来不及跑）；
- 浏览器进程可能成为孤儿（PDF 渲染那条路径的清理在 worker 的 `finally` 里）；
- 缓存目录可能留半写的 `.part`。

**旁证**：同一类问题在登录窗上已有先例 —— 	ests/test_window.py:1853 的注释写着「手里只剩局部引用，如果不自己收，窗口就留成孤儿（真机 2026-10-18）」；	ests/test_window.py:2663 与 :3375 的用例名也强调「不留孤儿进程」。导出侧没有等价的收手保证，只靠 1.2 秒的延迟。

**风险等级**：低-中。只影响"导出中途强行关窗"，产物要么完整（已 `os.replace`）要么不存在，但残留文件需人工清。

**建议改法**（下一轮做）：
1. `on_close` 里把 1.2 秒改成"等 worker 自行退出，最多 N 秒"：给每个 worker 一个 `threading.Event` 作结束信号，`on_close` 里 `event.wait(timeout=5.0)` 再 `destroy()`；
2. 进程启动时扫一遍输出目录与缓存目录，把超过 1 小时没动的 `*.part` 删掉（`clean_scratch` 里已有类似逻辑，可以复用）；
3. 补用例：伪造一个卡住的 worker，断言 `on_close` 后 `wait` 真的等到超时再 destroy。

## 七、其它已核项

| 项 | 结论 |
| --- | --- |
| 正文 HTML 转义 | `xdao/exporters/html.py:254 render_content`：`<img>` 走 `embed_image` 并对 data URI `escape(quote=True)`；其它标签 `escape(get_starttag_text())` 当**可见文本**保留（不丢 `<script>` 片段、也不会执行） |
| `data:` 图片 | `_shared.resolve_image_url` 原样放行，但 `fetch_image` 对 `data:` 直接返回 `None`（`:187`）⇒ 不外发请求 |
| 配置坏掉 | `xdao/settings.py:161 load()` 整段 `except Exception: pass`；`:173 _apply` 首行 `if not isinstance(data, dict): return` ⇒ 坏配置回默认，不崩 |
| CDP 只连本机 | `xdao/cdp.py:168 _http_json_once` 显式关代理、只读 `127.0.0.1` 上的 `DevToolsActivePort` 与 `/json/list` |
| 日志泄漏 | `xdao/gui.py:5224 log()` 只把文本插进 Text 控件，不做格式化字符串拼接 |

## 八、建议清单（按优先级）

| 优先级 | 项 | 位置 |
| --- | --- | --- |
| 中 | 挡 Windows 设备名，`\x00` 换成 `_` | `xdao/exporters/_shared.py:43 sanitize_filename` |
| 中 | 关窗时等 worker 结束（Event + 超时）而非固定 1.2 秒 | `xdao/gui.py:5765 on_close` |
| 低 | 启动清理超期 `*.part` | 新增（可复用 `tools/clean_scratch.py`） |
| 低 | `_request` 入口显式拒 CR/LF 的 URL | `xdao/client.py:222` |
| 低 | 配置写盘 `chmod 0o600`（POSIX） | `xdao/settings.py:300` |
| 低 | 解压额外拒绝对路径与符号链接成员 | `xdao/updater.py:199` |

以上六项都不属于"当前可被利用的漏洞"，属"让失败更好懂、让残留更少"的加固。
