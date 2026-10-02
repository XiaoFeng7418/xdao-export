# v0.13.24：领饼干那条 HTTP 路补上一处会 404 的拼地址写法

## 现象

0.13.23 把「应用饼干」整个搬回程序自己走 HTTP 之后，这条路里还留着一处**会 404 的拼地址**：
程序要按 id 自己拼两次地址（先 `switchTo/id/{id}.html`、再 `export/id/{id}.html`），
而站点「饼干」页里那些「应用」链接写的是**带 `.html` 的完整地址**
（`…/Cookie/switchTo/id/461037.html`）—— 于是 id 连后缀一起被带了出来，拼成
`…/switchTo/id/461037.html.html`。

## 原因

`XdaoClient.apply_cookie()` 取 id 的正则只截到 `/`、空白、引号为止：

    ids = re.findall(r"Cookie/(?:switchTo|export)/id/([^/\s\"'<]+)", html)

链接带不带 `.html`，它都会把后缀一起捕获；后面两句又固定补一次 `.html`（`switchTo/id/{id}.html`、
`export/id/{id}.html`）。列表页链接带后缀时，这两个请求就成了 `…html.html`，站点回 404 ——
而 `userhash` 正是在这两跳之后才拿到的。

它一直没被发现，是因为用例里那两个接口的路由键写得很宽松（`"/Cookie/export/"` 这样的子串匹配）：
`…/id/abc123.html.html` 照样命中，于是拼错的地址在测试里一路绿灯。

## 改法

- 拼地址之前先把 id 末尾的 `.html`／`.htm` 去掉（`re.sub(r"\.html?$", "", cookie_id, flags=re.I)`）
  —— 链接带后缀、不带后缀两种写法都能走通，其它逻辑一行没动。
- `tests/test_client.py` 补三条用例：①「饼干切换成功!」那张空 `href` 的成功页只请求一次
  （不许把它当成「再去一趟当前地址」）；② 跳转目标就是当前页时同样只请求一次；
  ③ 应用饼干时**逐字**钉住 `switchTo` 与 `export` 两个地址 —— 把去后缀那一行改回原样，
  这条用例立刻报出 `…/id/abc123.html.html`。

界面布局、导出格式、配置文件、命令行开关与 0.13.23 完全一致。

## 真机核验

- 本机全量：1649 passed / 7 skipped（共 1656 项）。
- 反向验证：把归一化那一行换成原样返回，新增的第③条用例立刻变红，报的正是
  `…/switchTo/id/abc123.html.html`；改回后全绿，`git diff` 里只剩本次改动。
- 真机（走本机代理，用一块无效会话的饼干）：`apply_leaf_cookie_over_http()` 1.6~1.7 秒返回，
  不抛异常、不卡住，`detail` 是「登录后没能进入用户系统（X 岛把请求弹回了登录页。）…」
  —— 失败分支仍然是一句能读懂的话。
- 打包版：`--version` 报 `X岛串导出工具 0.13.24`；包内 `使用说明.txt` 与仓库里的那份逐字节一致；
  `tools/repo_check.py` 14 项全过。

## 附件

`xdao-export-v0.13.24-win64.zip`（免安装包）。与 0.13.23 的差别只有上述行为改动。
