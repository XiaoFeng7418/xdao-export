# 依赖边界：跑这个程序实际需要什么

这一份只回答一个问题：**把它拿到一台干净机器上，缺什么就跑不起来。**
答案分四层——运行时（Python 包）、打包（zip 里带了什么）、外部程序（系统里
得有什么）、开发期（只有改代码的人才需要）。每一层都是实际查过的，不是照抄
`README`。

范围：`xdao/`、`main.py`、`tools/` 的现有代码与 `v0.22.0` 的免安装包。
只读，不改任何源码。

## 一、总览

| 层 | 需要什么 | 缺了会怎样 | 谁需要 |
|---|---|---|---|
| 运行时 | Python 3.10+ 标准库（43 个顶层模块，第三方 0 个） | 起不来 | 源码运行 |
| 运行时 | `tkinter`（标准库，但要带 Tcl/Tk 的那份 Python） | 命令行照常，图形界面起不来 | 源码运行 |
| 运行时 | 一个基于 Chromium 的浏览器 | 只影响 PDF（静默改存 HTML）与「用浏览器登录」 | 两种运行方式 |
| 打包 | 免安装包自带解释器与 Tcl/Tk | —（都带了） | 免安装包 |
| 外部程序 | PowerShell、`taskkill`、`explorer` | 通知、登录取饼干、打开目录这些附加功能失效 | 免安装包 |
| 开发期 | pytest、ruff、PyInstaller | 不能跑测试／静态检查／打包 | 只有改代码的人 |

## 二、运行时依赖：真的只用标准库

### 2.1 导入普查

用 AST 解析 `xdao/**/*.py` 与 `main.py` 的全部 `import` / `from ... import`，
按顶层模块名去重：

- 顶层模块 **43 个**；
- 与 `sys.stdlib_module_names` 取差集后，非标准库只剩 **1 个：`tools`**；
- `tools` 的唯一出处是 `main.py:1219` 的 `from tools.pdf_diag import main as pdf_diag_main`
  —— 只有 `--pdfdiag` 诊断开关走这条路，而且它在函数内部（延迟导入）；
- 加上 `tools/*.py` 一起扫是 51 个，多出来的 `make_release` / `push_via_api` /
  `repo_info` / `tests` 都是仓库内脚本互相导入，不进运行时分发。

标准库清单（42 个，按字母序）：`__future__` `argparse` `base64` `collections`
`concurrent` `ctypes` `dataclasses` `datetime` `errno` `gzip` `hashlib` `html`
`http` `itertools` `json` `math` `os` `pathlib` `platform` `queue` `random` `re`
`secrets` `shutil` `socket` `struct` `subprocess` `sys` `tempfile` `threading`
`time` `tkinter` `traceback` `typing` `urllib` `uuid` `webbrowser` `winreg`
`winsound` `zipfile` `zlib`。

**动态导入 0 处**：`importlib`、`__import__`、`import_module`、`pkgutil` 全仓
没有命中。这意味着 PyInstaller 的静态扫描是可靠的，唯一需要手工补的是延迟导入
的 `tools.pdf_diag`（见 5.1）。

### 2.2 平台相关模块都有守卫

| 模块 | 用在哪里 | 守卫方式 |
|---|---|---|
| `tkinter` | `theme.py`、`widgets.py`、`gui.py` | **没有守卫**（模块级直接导入） |
| `winreg` | `browser_login.py:221`、`github_api.py:103` | 前者函数内导入并注明「只有 Windows 走这条路」，后者 `try/except ImportError` 返回空串 |
| `winsound` | `notifications.py:164` | `try: import winsound / except Exception: return False` |
| `ctypes` | `dpi.py:40`、`dpi.py:71`、`updater.py:332`、`main.py:108` | 都在 `if os.name != "nt"` / `if not getattr(sys, "frozen", False)` 之后，且整段 `except Exception` 兜住 |
| `webbrowser` | `gui.py:4965` | 函数内导入 |

结论：除 `tkinter` 之外的平台模块都做了「不是 Windows 就绕」的处理；`tkinter`
是唯一硬依赖，原因是它本身就是界面层。

### 2.3 Python 版本下限：3.10 站得住

- 用 `ast.parse(..., feature_version=(3, 10))` 扫全部 **110 个 `.py`**（排除
  `.git` / `dist` / `build` / `__pycache__` / `.test-artifacts` / `.cache`）：
  **0 个语法失败**。
- 逐个搜 3.11 / 3.12 才有的 API：`tomllib`、`typing.Self`、`itertools.batched`、
  `Path.walk`、`ExceptionGroup` / `except*`、`typing.override`、`StrEnum`、
  `hashlib.file_digest`、`contextlib.chdir`、`shutil.rmtree(onexc=)`、
  `datetime.UTC` —— **全部 0 处**（`datetime.UTC` 只在注释里出现过一次）。
- `str.removeprefix` / `removesuffix` 有 2 处（`preflight.py`），3.9 就有；
  `zip(strict=)` 0 处（那是 3.10 才有的参数，没用）。
- `tests/test_py310_syntax.py` 另外钉住了两类 3.12 才允许的 f-string 写法。

## 三、缺了 `tkinter` 会发生什么（实测）

用两种办法各测一遍（`sys.path` 下放影子模块，以及拦截 `builtins.__import__`），
结论一致：

| 命令 | 结果 |
|---|---|
| `python main.py --version` | 正常输出 `X岛串导出工具 0.22.0`，退出码 0 |
| `python main.py --help` | 正常，退出码 0 |
| `python main.py --selftest --offline` | 完整跑通，「本机自检全部通过。」，退出码 0 |
| `python main.py`（无参数 → 图形界面） | 打印「启动失败：ImportError: No module named 'tkinter'」，**退出码 1** |
| 直接调 `create_exporter("txt", ...).save(...)` | 正常，291 字节成品 |

也就是说：**命令行链路完全不碰 Tk**，缺 Tk 只影响图形界面。

### 3.1 一处真问题：缺 Tk 时配置会被静默丢掉一半

`settings.py` 的 `_apply()` 在第 211 行调 `_ui_font_scale()`，后者为了「不要在
两处各写一份归一化规则」而 `from .theme import normalize_font_scale`——`theme`
模块级就是 `import tkinter`。于是：

1. `_apply()` 在第 211 行抛 `ImportError`；
2. `AppSettings.load()` 的 `except Exception: pass` 把它吞掉；
3. 第 211 行**之后**的字段全部停在默认值，**没有任何提示**。

实测（配置里 16 个字段都写了非默认值，在缺 Tk 的解释器里加载）：

| 字段 | 配置里写的 | 实际生效 |
|---|---|---|
| `userhash` / `output_dir` / `proxy` / `timeout` / `retries` / `throttle` / `page_workers` / `format_key` / `scope` / `filename_template` / `image_mode` / `export_font_size` | 非默认 | **正常生效**（在第 211 行之前） |
| `ui_font_scale` | 1.3 | 1.0 |
| `skip_existing` | true | false |
| `merge_export` | true | false |
| `pdf_paper` / `pdf_orientation` / `pdf_margin` / `pdf_margin_mm` / `pdf_scale` / `pdf_background` / `pdf_page_ranges` | 非默认 | 全默认 |
| `use_cache` | false | true |
| `cache_dir` | `D:/c` | 空 |
| `watch_interval` | 120 | 300.0 |
| `verify_cached` | true | false |
| `watch_targets` | — | 未赋值（保持默认） |
| `notify` / `notify_interval` | false / 60 | true / 900.0 |
| `theme_name` / `extra` | dark / `{"a": 1}` | light / `{}` |

影响：**命令行用户在无 Tk 的 Python（例如 Linux 上没装 `python3-tk`）里跑导出
时，PDF 选项、缓存目录、去重与合并开关全部静默失效**，而程序一切正常、不报错。
这是「依赖边界」这一项里唯一会**改变行为却没有任何信号**的地方。

## 四、打包后的边界

### 4.1 命令

`tools/build_zip.py` 的 `pyinstaller_command()` 生成：

```
python -m PyInstaller --onedir --windowed --clean --noconfirm
  --version-file <临时版本资源文件>
  --hidden-import tools.pdf_diag
  --specpath <临时目录> --workpath <临时目录>/build --distpath <输出目录>
  --name xdao-export main.py
```

`--hidden-import tools.pdf_diag` 是必需的：`--pdfdiag` 在 `main.py` 里是延迟导入，
PyInstaller 静态扫描看不到（历史上有过一版包里没有这个模块、`--pdfdiag` 直接
`ModuleNotFoundError`）。`tests/test_build_zip.py` 有两条用例把这个参数钉住。

### 4.2 免安装包里实际有什么

`v0.22.0` 的 zip 解开后：**957 个文件、28,047,691 字节**。

顶层只有 5 个文件 ＋ 一个 `_internal/` 目录：

| 顶层文件 | 字节 |
|---|---|
| `xdao-export.exe` | 2,959,246 |
| `使用说明.txt` | 133,790 |
| `诊断写入.ps1` | 8,532 |
| `诊断写入-双击运行.cmd` | 338 |
| `LICENSE.txt` | 1,078 |

`_internal/` 里最大的几样：`python312.dll` 6.9 MB、`libcrypto-3.dll` 5.2 MB、
`tcl86t.dll` 1.8 MB、`tk86t.dll` 1.6 MB、`base_library.zip` 1.3 MB、
`unicodedata.pyd` 1.1 MB、`libssl-3.dll` 0.8 MB。按扩展名合计：`.dll` 9 个共
16.7 MB、`.pyd` 16 个 2.58 MB、`.enc` 82 个 1.54 MB、无扩展名 612 个 1.21 MB、
`.tcl` 69 个 0.96 MB、`.tm` 5 个 0.28 MB、`.msg` 145 个 0.20 MB。

`_internal/` 下只有 `_tcl_data` / `_tk_data` / `tcl8` 三个数据目录，**没有**
`idlelib` / `test` / `unittest` / `lib2to3` / `pydoc_data` / `ensurepip` /
`distutils` / `pkg_resources` / `setuptools` / `pip`——该砍的都砍掉了。
包内 `.py` 文件数为 0（运行时全是 `.pyc` / `.pyd` / 资源）。

系统运行库不需要另装：`VCRUNTIME140.dll`、`VCRUNTIME140_1.dll` 都在包里。

## 五、外部程序依赖

这些不是 Python 包，但少了对应功能就不可用：

| 程序 | 用在哪里 | 缺了会怎样 |
|---|---|---|
| Chromium 内核浏览器（Chrome / Edge） | PDF 渲染、`--pdfdiag`、「用浏览器登录」 | PDF 静默改存 HTML 并提示；登录得手填饼干 |
| PowerShell（`powershell` 或 `pwsh`） | 读浏览器进程与饼干、Windows 通知 | 自动取饼干失败；通知不弹 |
| `taskkill` | 关掉登录用的临时浏览器 | 临时浏览器窗口可能留着 |
| `explorer` | 「打开导出目录」「在文件夹中显示」 | 这两下点击无效 |
| `osascript` / `notify-send` | macOS / Linux 的通知 | 只在非 Windows 上相关 |

`git` 与 `gh` 只出现在 `github_api.py`，是给仓库发布脚本用的，**不在导出链路上**。

## 六、开发期依赖（不进免安装包）

| 工具 | 谁用 | 版本是否钉住 |
|---|---|---|
| pytest | `tests/` 49 个文件 | **CI 未钉**（`python -m pip install pytest`） |
| ruff | CI 静态检查 | **已钉 `0.17.5`**（`tests.yml`） |
| PyInstaller | `tools/build_zip.py`、`tools/gui_probe.py` | **CI 未钉**（`python -m pip install pyinstaller`） |
| coverage | `tools/cov_audit.py`、`tools/cov_funcs.py` | 本机体检用，不参与 CI |

仓库里没有 `pyproject.toml` / `setup.cfg` / `setup.py` / `tox.ini` /
`requirements*.txt`——不发包、不装依赖，这些文件确实用不上；`ruff.toml` 与
`pytest.ini` 各自独立就够。

## 七、许可与包内容的一致性

`README` 的「第三方组件」一节写：打包版里带着 Python 解释器（PSF 许可）、
Tcl/Tk 图形库（BSD 风格许可）与 PyInstaller 启动器（GPL + 例外条款），
「**它们各自的许可随包保留**」。

实际解开 `v0.22.0` 的 zip 之后，全包里只有两份许可文件：

- 包根 `LICENSE.txt`（1,078 字节，是本程序管理的 MIT 全文）；
- `_internal/_tk_data/license.terms`（2,307 字节，Tcl/Tk 的许可）。

**Python 解释器的 PSF 许可、PyInstaller 启动器的许可都没有随包。**
（`libcrypto-3.dll` / `libssl-3.dll` 是 OpenSSL，Apache 2.0 许可，同样没有。）
这不影响使用，但和 `README` 的承诺不一致，属于分发合规上的缺口。

另外一处小不一致：包内 `使用说明.txt` 的 SHA256 与仓库 `packaging/使用说明.txt`
不同（包是 2026-11-11 22:30 打的，之后仓库文档又改过）。发布记录不改写，
这里只记事实。

## 八、建议

按「会不会让人装不起来／查不出来」排序：

1. **把 `settings._apply` 里的 `from .theme import PALETTES` 与
   `_ui_font_scale` 里的 `from .theme import normalize_font_scale` 换成不依赖
   Tk 的实现**（配色名集合与字号归一化都是纯数据／纯数学），同时让
   `load()` 的异常至少写一行日志。当前后果是「无 Tk 时一半配置静默失效」。
2. **打包时把 PSF 许可、PyInstaller 许可、OpenSSL 许可一起放进 zip**
   （或在 `README` 里改成与事实一致的表述）。现在只有 Tcl/Tk 与 MIT 两份。
3. **CI 里把 PyInstaller 的版本钉住**（`ruff` 已经钉了）。否则「同一份源码
   打出来的包不一样」无法复现，也没有痕迹。
4. **给 `tkinter` 的缺失加一句可执行的提示**：现在 GUI 起不来时提示「删掉
   config.json 重试」，而真正的原因是没装 Tcl/Tk。识别
   `ModuleNotFoundError: tkinter` 之后单独说「这份 Python 没带图形库」。
5. **`pytest` 在 CI 里也钉一个版本**（与 `ruff` 一致），避免上游改了默认
   行为之后红得莫名其妙。
6. 低优先：`base_library.zip` 里 155 个标准库 `.pyc` 是 PyInstaller 的启动
   依赖，不必再优化；`_internal/.enc` 82 个文件合计 1.54 MB，属于 Tcl 编码
   表，删了会影响中文界面，不动。

## 九、复现方式

```powershell
# 1) 导入普查（43 个顶层模块，第三方 0 个）
python -X utf8 -c "import ast,pathlib,sys; ..."   # 见仓库 scripts/ 说明

# 2) 缺 tkinter 的行为
python -X utf8 -c "import sys; sys.meta_path.insert(0, Blocker()); ..."
python main.py --version
python main.py --selftest --offline

# 3) 包内容
python -X utf8 -c "import zipfile; z=zipfile.ZipFile('xdao-export-v0.22.0-win64.zip'); print(len(z.namelist()))"
```

探针都是一次性的，跑在临时目录里，不写仓库、不联网。

## 十、范围说明

- 只测「需不需要额外东西才能跑」，不测「跑起来有多快」「功能合不合规」。
- 免安装包用的是仓库里 `v0.22.0` 那次构建的产物；之后仓库的文档改动没有反映
  在包里，这是发布流程的正常结果，不算缺陷。
- 浏览器一项只验证了「没有它时 PDF 会降级」，没有对各家浏览器版本做矩阵测试。
