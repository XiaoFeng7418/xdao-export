# 代码签名：现在是什么状况，免费那条路怎么走

本文件记的是**流程**，不是宣传材料。面向用户的说法在 `README.md` 的「代码签名政策」一节。

## 一、现状（2026-10-02）

- 发布包**没有**商业代码签名证书。所以 Windows 可能提示「未知发布者」、
  个别杀软可能误报 —— 这不是包坏了，是没签名。
- 用户现在能自己核对的三件事：
  1. 发布页上的 `xdao-export-vX-win64.zip.sha256`（v0.13.28 起）；
  2. 解压出来的 `xdao-export.exe`，右键「属性 -> 详细信息」里的**公司名是「晓风」**
     （PE 版本信息，`tools/build_zip.py` 里写死的，v0.13.28 起）；
  3. 源码可读可自行构建：`python tools/build_zip.py`。
- 说清楚一件事：`.sha256` 与 zip 在同一个 Release 里，**挡不住「账号被盗、两个附件一起被换」**。
  它只能证明「你下载到的字节，就是那一页上挂的那份」。

## 二、免费签名：只有一条路，而且署不了网名

免费且真能消掉「未知发布者」的，只有面向开源项目的
[SignPath Foundation](https://signpath.org/)（配套 [SignPath.io](https://signpath.io/)）。

| 事项 | 结论 |
| --- | --- |
| 花不花钱 | 开源项目免费 |
| 要不要个人实名 | 不要。他们核的是「这个二进制确实出自这个公开仓库」 |
| Windows 上显示谁 | **SignPath Foundation** —— 证书是签发给它的，不是签给作者 |
| 能不能署「晓风」 | 不能。签名证书只能签发给法律主体，网名不行 |
| 想署名字去哪 | PE 版本信息里的 `CompanyName`（本项目已经这么做了） |

申请条件（他们保留拒绝权，新项目可能被拒）：

- 许可证是 OSI 认可的、**没有商业双授权**、不含专有组件；
- 项目**在维护中**、**已经发过版**、有说明用途的下载页；
- 团队成员开 MFA，并区分 Author / Reviewer / Approver 角色；
- 每次签名请求都要**人工批准**；
- 项目首页要有「代码签名政策」小节（README 里的那一节就是为这个写的），
  并写明 `Free code signing provided by SignPath.io, certificate by SignPath Foundation.`
  以及隐私说明与角色成员；
- 产物要有并强制 metadata（产品名 / 版本号）。

没做的备选，以及为什么不做：

- **自签名证书**：免费，但 Windows 照样显示未知发布者，等于白做；
- **Azure Trusted Signing**：约 $9.99/月，不是免费；
- **OV/EV 商业证书**：约 ¥1000–2000/年起，EV 还要硬件令牌；
- **Sigstore / cosign 这类签名**：免费，但那是给容器与软件供应链用的，
  管不了 Windows 的「未知发布者」。

## 三、仓库这边已经准备好的东西

- `tools/build_zip.py`：打包 + 写 `.sha256`，并先核「版本号 / 发布说明 / 使用说明」是否对齐；
- `.github/workflows/build.yml`：在 GitHub 托管的 Windows runner 上打出 zip 并上传成
  artifact —— 这是签名要求的前置条件（**要签的产物必须由 CI 构建**）；
- PE 版本信息里的 `CompanyName=晓风`；
- `README.md` 的「代码签名政策」一节。

## 四、获批之后要做的事（按顺序）

1. 在 SignPath 后台建好三样，并记下它们的 slug：
   - Project（绑 `XiaoFeng7418/xdao-export`，默认分支 `master`）；
   - Artifact Configuration：根元素 `<zip-file>`（GitHub 的 artifact 本身就是个 zip），
     里面放开 `**/xdao-export.exe` 与 `**/_internal/**` 里我们自己打进去的部分；
   - Signing Policy：类型用 `release-signing`，勾上「需要人工批准」。
2. 在本仓库设好变量与密钥（`gh` 已登录，可以直接敲）：
   ```powershell
   gh variable set SIGNPATH_ENABLED --body true --repo XiaoFeng7418/xdao-export
   gh variable set SIGNPATH_ORGANIZATION_ID --body "<组织 id>" --repo XiaoFeng7418/xdao-export
   gh variable set SIGNPATH_PROJECT_SLUG --body "<project slug>" --repo XiaoFeng7418/xdao-export
   gh variable set SIGNPATH_SIGNING_POLICY_SLUG --body "<policy slug>" --repo XiaoFeng7418/xdao-export
   gh secret set SIGNPATH_API_TOKEN --repo XiaoFeng7418/xdao-export
   ```
3. 手动跑一次 `build` workflow，确认签名成功（`signpath/github-action-submit-signing-request@v3`
   那一步会等人工批准），然后核对签名后的 exe：右键属性应出现「数字签名」页。
4. 把发版流程改成「CI 构建 -> 签名 -> 发 Release」：**签名后的包必须来自 CI 那一次构建**，
   不能再挂本机打的包（两者字节不会相同：zip 条目顺序、时间戳、runner 上的运行时都可能不同）。
   在改完之前，Release 附件仍然用本机 `tools/build_zip.py` 的产物。
5. README 的「代码签名政策」一节把「正在申请」改成已生效，并写上真实角色。

## 五、万一被拒

退路是：保留 `.sha256` + PE 版本信息里的作者名 + README 里的现状说明，
不再对外宣称有任何签名。README 那一节要按实际情况改写 —— 宁可少说，不能虚说。
