# 导出格式兼容性矩阵（只读）

审计对象：`xdao/exporters/` 下五个导出器（`html` / `pdf` / `txt` / `markdown` / `epub`）。
审计日期：2026-11-15。方式：构造一个塞满边界内容的测试串，五种格式各导出一次，读产物结构；再做图片模式矩阵与文件名/正文边界探测。所有探针脚本都在仓库外的临时目录，**没有改动任何仓库文件**。
结论口径：**"风险" 说的是"这种输入会让产物变成什么样"，不是"现在有 bug"**。

## 一、总览

| 格式 | 单文件 | 图片处理 | 边界串产物 | 结论 |
| --- | --- | --- | --- | --- |
| HTML | 是（`embed`） | 四选一 | 13,784 字节 | 结构完整，`<script>` 被转义成可见文本 |
| PDF | 是 | 四选一（`folder` 退回内嵌） | 159,912 字节 / 3 页 | 字体子集内嵌、带书签、无脚本注入面 |
| TXT | 是 | 只能留链接 | 10,377 字节 | 字面尖括号会被当标签吃掉（见 §3.1） |
| Markdown | 是 | 三选一（`embed` 按 `url` 处理） | 10,147 字节 | 标签保留为原文，`folder` 模式下中文路径被百分号编码 |
| EPUB | 是 | 三选一（`folder` 静默变 `embed`） | 5,286 字节 | 结构合规；`embed` 时图片存了两份（见 §4.1） |

## 二、测试输入

一串一楼，主帖故意塞满会出问题的地方：

- 3000 字不换行的长行（测自动换行与排版）
- `>>No.1001` 重复 40 次（测引用块 / 缩进）
- `<b>粗体</b>`、`<script>alert('x')</script>`（测标签处理与转义）
- 字面文本 `<尖括号>`（测"看起来像标签但不是标签"）
- `特殊字符：<&>"'`（测实体转义）
- emoji 🐟、RTL 覆盖字符 `a\u202eb`（测宽字符与双向文本）
- 两张正文图 + 一张附件图（`img` + `ext` 字段）

另有一楼带一张图、一楼正文为空。合计 3 楼，串号用 `50000001`。

## 三、逐格式观察

### 3.1 TXT（10,377 字节）

| 观察点 | 结果 |
| --- | --- |
| `[PO]` 标记 | 保留 |
| 饼干 | 保留（`POCOOKIE`） |
| 3000 字长行 | 原样保留，不折行 |
| 图片 | 每张一行 `[图片] 绝对URL` |
| `<b>粗体</b>` | 变成 `粗体`（标签去掉、文字留下） |
| `<script>alert('x')</script>` | 变成 `alert('x')` 脚本字面文本（不是执行，是当正文留下的） |
| **字面 `<尖括号>`** | **整段消失**：原文 `与字面尖括号 <尖括号> 以及` → 输出 `与字面尖括号  以及` |
| `特殊字符：<&>"'` | 输出 `特殊字符："'` —— `<&>` 被当成标签吃掉 |

**风险（低-中）**：`plain_text()` 的正则是 `<[^>]+>`，任何"尖括号 + 非空内容 + 尖括号"的**字面文本**都会被当 HTML 标签删掉。串里出现「a<b 且 b>c」这类数学/代码写法时，TXT 会静默少字。Markdown 与 HTML 没有这个问题（它们用 `HTMLParser` 或转义）。

### 3.2 Markdown（10,147 字节）

| 观察点 | 结果 |
| --- | --- |
| 楼号标题 | `## 1. [PO] POCOOKIE · 时间 · No.1001` |
| 元信息 | 引用块 `> 饼干 \`POCOOKIE\` · 标题…` |
| 字面 `<尖括号>` | **保留**（`HTMLParser` 把它当"未知标签"的数据流过 `handle_data`） |
| `<b>` / `<script>` | 标签被去掉、文字留下（`粗体`、`alert('x') 脚本`） |
| 特殊字符 `<&>"'` | 原样（`特殊字符：<&>"' 与 emoji 🐟 与 RTL a‮b`） |
| 图片 | `![图片](绝对URL)`，正文图与附件图连续两行 |
| 空正文楼层 | 照常输出标题行 + `---`，不留空段 |
| 长行 | 原样（Markdown 不做硬折行） |

**风险（低）**：`<&>"'` 原样留在正文里，某些 Markdown 渲染器会把 `<&>` 当原始 HTML 处理；粘贴到博客时可能出现渲染异常。属"Markdown 原生行为"，不视为缺陷。

### 3.3 HTML（13,784 字节）

| 观察点 | 结果 |
| --- | --- |
| 文档头 | `<!DOCTYPE html>` + `lang="zh-CN"` + `<meta charset="utf-8">` |
| `<script>` | **转义成可见文本** `&lt;script&gt;alert('x')&lt;/script&gt;` —— 不执行、不丢内容 |
| `<b>粗体</b>` | 同上，转义成可见文本（保持"原文长什么样就长什么样"） |
| 图片 | 4 处 `data:image`（正文 2 + 附件 1 + 回复 1）；`src="http` 计数 **0** —— 完全离线 |
| 每楼结构 | `<article class="post">` 一楼一个，PO 楼带 `po` 类与 `badge-po` |
| 空正文楼层 | 输出空 `<div class="content"></div>`，不塌结构 |
| 标题 | `<title>` / `<h1>` 正确；帖子内标题字段 `ptitle` 里的 `<尖括号>` 被 `plain_text` 去掉 |

**风险（无）**：这是五个格式里转义最保守的一个，标签一律当文本。`<script>` 不会执行。

### 3.4 EPUB（5,286 字节）

| 观察点 | 结果 |
| --- | --- |
| `mimetype` 条目 | **第一个条目**且 `compress_type=0`（STORED），内容 `application/epub+zip` |
| `META-INF/container.xml` | 存在，指向 `OEBPS/content.opf` |
| `content.opf` | `version="3.0"`，命名空间 `http://www.idpf.org/2007/opf` |
| metadata | `dc:identifier`（`urn:uuid:…`）、`dc:title`、`dc:language=zh-CN`、`dc:creator`、`dcterms:modified` |
| manifest | 11 项（nav / ncx / css / 3 个 xhtml / 4 张图） |
| spine | `toc="ncx"`，顺序 `nav, post-0001..0003` |
| `nav.xhtml` | `epub:type="toc"` + 三个链接 |
| 每楼 xhtml | **全部 well-formed**（`ElementTree` 能解析） |
| `toc.ncx` | 存在（EPUB 2 兼容） |
| **`dc:creator`** | 填的是 **PO 饼干**（`POCOOKIE`），不是标题也不是项目标识 |

**风险（低）**：`dc:creator` 用饼干，读者在阅读器的"作者"栏会看到一串匿名哈希；这本身不算错（本工具没有实际作者身份信息），但和用户预期可能有落差。

### 3.5 PDF（159,912 字节 / 3 页）

| 观察点 | 结果 |
| --- | --- |
| 生成器 | `/Producer (Skia/PDF m157)`、`/Creator (Mozilla/5.0 …)` —— 走本机 Chrome |
| 头尾 | `%PDF-1.4` 开头、`%%EOF` 结尾 |
| 分页 | 3 楼 → 3 页（`break-inside: avoid` 生效，没被从楼中间切断） |
| 书签 | `/Outlines` 存在（v0.21.17 起） |
| 字体 | `/Type0` + `/CIDFontType2`，10 个 `/FontFile2` 子集内嵌；`/ToUnicode` 12 处 ⇒ 中文可复制、可搜索 |
| 图片（真 PNG 探针） | `/Subtype /Image` 1 个、`/Width 600 /Height 400` 正确、`/FlateDecode` 重编码 |
| 危险对象 | `/JS`、`/EmbeddedFile`、`/OpenAction` 计数均为 **0** |

**风险（无）**：PDF 由浏览器渲染，没有脚本注入面；体积大是图片重编码的必然结果（295,595 字节的 PNG → 394,370 字节的 PDF，约 1.33 倍）。

## 四、图片模式矩阵

同一串（1 楼、2 张图）在四种模式下各导出一次：

| 格式 | `embed` | `url` | `drop` | `folder` |
| --- | --- | --- | --- | --- |
| HTML | 2 处 data URI、0 外链 | 0 data URI、2 外链 | 都 0 | 正文 0/0，另写 `成品.files/` 目录（`a.jpg`、`att.png`） |
| PDF | 内嵌（内存渲染） | 外链 | 丢弃 | **静默退回内嵌**（没有同名成品可挂） |
| Markdown | **与 `url` 输出完全相同** | 2 个 `![图片](URL)` | 无图片行 | `![图片](成品.files/a.jpg)`，路径被百分号编码 |
| EPUB | data URI + `OEBPS/images/img-000N` 各一份 | 外链、zip 里无 images 目录 | 都 0 | **静默变 `embed`**（`epub.py:140` 只认三种） |
| TXT | 三种模式输出**完全一致**（426 字节，都不带图） | 同左 | 同左 | 同左 |

### 4.1 EPUB 在 `embed` 下把图片存了两份（值得关注）

实测真 PNG（295,595 字节）导出 EPUB 得 **595,545 字节 ≈ 2.01 倍**：图片既以 `data:image` 写进 `post-0001.xhtml`，又以 `OEBPS/images/img-0001.png` 落盘并在 `content.opf` 里登记，而 xhtml 里**从不引用**那个文件（实测 `src=` 只有 data URI）。等于每张图在压缩包里占两倍空间，还多了没人用的 manifest 项。

`_collect_images` 只在 `image_mode == "embed"` 时收集图片（`epub.py:243`），而 xhtml 里 embed 走的是 `_data_uri_for`（`epub.py:290`）——两条路径同时生效就重复了。属**体积浪费**，不影响正确性（阅读器读 data URI）。

### 4.2 Markdown `folder` 模式的中文路径

`ImageFolderWriter.write` 用 `urllib.parse.quote(..., safe="/")` 转义相对路径（`_shared.py:387`），于是 `图片模式矩阵.files/a.jpg` 变成 `%E5%9B%BE%E7%89%87...files/a.jpg`。浏览器与 VS Code 都认，但部分老编辑器不认百分号编码 ⇒ 图片显示不出来。属**兼容性风险（低）**。

## 五、文件名边界（`sanitize_filename`）

| 标题 | 结果 |
| --- | --- |
| 空标题 | 回退到第一楼正文首句（`正文.txt`） |
| `///` | `___.txt` |
| `...` | `xdao-thread.txt`（回退） |
| `CON` | `CON.txt` —— **能建出来**。实测目录里**没有文件**（Windows 把 `CON.txt` 当控制台设备，写入被吞） |
| `nul`（小写） | 同上，文件不存在 |
| `PRN` / `COM1` | **`FileNotFoundError`** —— 导出直接失败 |
| 200 字标题 | 截到 80 字后 `.txt` |
| 含换行 / 制表符 | **`OSError: [Errno 22] Invalid argument`** —— 导出失败 |
| 含 emoji | `标题🐟.txt` 正常 |
| 含 `/ * ? " < > \|` | 替换成 `_`（`a_bd_e.txt`） |

**风险（中）**：`sanitize_filename` 只处理 Windows 保留字符，没处理**控制字符**（`\n`、`\t`）与**设备名**（`CON`/`PRN`/`AUX`/`NUL`/`COM1~9`/`LPT1~9`）。前两种是真会中断导出的（抛 `OSError`，不是优雅报错），后三种在 Windows 上要么报错要么静默丢产物。TXT 之外的格式同样受影响，因为文件名生成在基类（`base.py:99`）。

## 六、正文边界

| 输入 | 结果 |
| --- | --- |
| 0 楼（空帖子列表） | 输出 161 字节的空壳 TXT（只有头部与分隔线），不报错 |
| 空正文楼层 | 279 字节，楼号与元信息都在，正文段为空 |
| 5000 行空行 | 25,279 字节，正常完成 |
| 纯标签（`<br><br><b></b><img>`） | 318 字节，不报错 |
| 未闭合标签（`<b>没闭合 <i>斜 <br>下一行`） | 306 字节，`HTMLParser` 容错通过 |
| 200 条 `>>No.1000` 引用 | 2,479 字节，原样 |

**结论**：正文侧容错良好，未闭合标签、纯标签、超长正文都不会让导出失败。真正的脆弱点在**文件名**。

## 七、建议清单（按优先级）

1. **挡控制字符与设备名**（中）：`sanitize_filename` 里把 `[\x00-\x1f]` 换成空格或 `_`，并在 Windows 上给 `CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]` 加前缀（如 `_CON`）。现在这种情况会抛 `OSError` 中断导出，用户看不懂。
2. **EPUB `embed` 不要重复存图**（中）：`_collect_images` 只在需要 file 引用时才收集；或让 xhtml 引用 `images/img-000N` 而不是 data URI。现在等于体积翻倍。
3. **TXT 的字面尖括号**（低-中）：`plain_text` 改用 `HTMLParser`（和 Markdown 一致），或至少只把已知标签当标签。
4. **EPUB `dc:creator`**（低）：改用标题或固定字符串，避免阅读器里显示一串饼干。
5. **Markdown `folder` 的中文路径**（低）：可以只对手上的字符做最小转义，或干脆保留中文（多数现代编辑器都认）。
6. **GUI 的图片模式与导出器能力对齐**（低）：EPUB 下拉框里能选「存到同名目录」，但 EPUB 会静默变 `embed`；要么在 UI 上禁掉，要么实现它。

## 八、复现方式

探针脚本在仓库外（`matrix_probe*.py`），核心是构造 `ThreadData` + `FakeClient` 后调 `create_exporter(key, client, image_mode=…).save(thread, "all", out)`。仓库自带的 `tests.FakeClient` 与 `tests.make_post` 已够用，不需要联网。

## 九、范围说明

- 只测了导出器，没测抓取层（那属队列项 3）。
- PDF 依赖本机浏览器；本次用 Chrome 156 实测，Firefox/Safari 不支持（工具本来就不支持）。
- 「图片模式矩阵」里 PDF 的 `folder` 是静默回退，不是缺陷（内存渲染没有同名成品），已在 §4 注明。
