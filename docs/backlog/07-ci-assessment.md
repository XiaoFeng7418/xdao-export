# CI 现状评估

评估日期：2026-11-15。范围：`.github/workflows/` 下两个工作流、最近 25 次运行记录、
仓库侧的协作开关。**只评估、不改动**（这一项按夜间任务属于「只读」）。

## 一、现状

| 项 | 现状 |
| --- | --- |
| 工作流 | `tests.yml`（5,059 字节）、`build.yml`（3,199 字节），两个都是 `active` |
| `tests` 触发 | push 任意分支 + push `v*` 标签 + `pull_request` + 手动 |
| `build` 触发 | 手动 + push `v*` 标签 |
| 权限 | 两个都是 `permissions: contents: read` |
| 并发 | `concurrency: tests-${{ github.ref }}` / `build-${{ github.ref }}`，都 `cancel-in-progress: true` |
| 矩阵 | `ubuntu-latest` py3.10、`ubuntu-latest` py3.12、`windows-latest` py3.12；`fail-fast: false` |
| 环境变量 | `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`、代理三项清空、`XDAO_HEADLESS=1` |
| 单元测试步骤 | `python -m pytest -q -rs` |
| 静态检查 | `python -m ruff check .`（配置在根目录 `ruff.toml`，只开 E4/E7/E9/F/B） |
| 语法检查 | `compileall` 后**显式 grep `error|Sorry`** 判错（注释写明 compileall 本身总返回 0） |
| 冒烟 | `main.py --version`、`main.py --help`、导出器注册表断言 `{html,pdf,txt,markdown,epub}` |
| 自检 | 仅 windows 跑 `main.py --selftest`，且 `continue-on-error: true` |
| 版本一致性 | 独立 job，只在 `refs/tags/v*` 上比 tag 与 `xdao.__version__` |
| 打包 | `build.yml`：装 PyInstaller → `python tools/build_zip.py --out dist` → `upload-artifact@v7`（`dist/*.zip` + `dist/*.sha256`，缺文件即报错） |
| 签名 | SignPath 步骤由 `vars.SIGNPATH_ENABLED == 'true'` 守卫，未配置时整段跳过 |

## 二、实测运行数据

最近 25 次运行（`gh run list --limit 25`）：`tests` 成功 17 次、`cancelled` 2 次；`build`
成功 6 次。**没有失败记录。**

最近一次 `tests`（run `43620448397`，提交 `612bcd3`）耗时分解：

| job | 结论 | 总耗时 | 单元测试 | 其它步骤 |
| --- | --- | --- | --- | --- |
| ubuntu-latest / py3.10 | success | 12.8 分钟 | 753 s | 全部 ≤1 s |
| ubuntu-latest / py3.12 | success | 12.0 分钟 | 710 s | 全部 ≤1 s |
| windows-latest / py3.12 | success | 6.7 分钟 | 351 s | 自检 23 s，其余 ≤5 s |
| 版本号与最新 Release 一致 | skipped | 0 | — | 非 tag 推送，预期 |

- **一次推送的实际成本 ≈ 13 分钟墙钟**（三个 job 并行），同一提交若有 PR 会再跑一轮。
- Linux 的单元测试比 Windows 慢一倍（753 s vs 351 s），且 Linux 上界面用例整组跳过
  —— 说明耗时主要在非界面用例的文件 I/O 上，不是 Tk。
- `安装测试依赖` 每次 4–12 秒，**没有 pip 缓存**。

## 三、做得好的地方（不建议动）

1. `compileall` 之后显式 grep 判错 —— 否则这一步等于空跑，该行为已有测试记录与注释说明。
2. ruff 版本钉在 `0.17.5`，规则集收窄到「一定是错」的五组，CI 与本地配置同源。
3. `conftest.py` 有一套跳过守卫：白名单外跳过直接 `UsageError`（退出码 4），白名单内但
   不是那几条真机用例的跳过会把退出码改成 1，除非显式声明 `XDAO_HEADLESS=1`。
   这是「0 failed 不等于干净」的正确做法。
4. `fail-fast: false` —— 一个平台红了不会掐掉其它平台的信息。
5. `upload-artifact` 的 `if-no-files-found: error` —— 产不出来就当失败，不留空构件。
6. 自检步骤带 `continue-on-error` 并写清理由（它查的是 runner 环境，不是代码）。

## 四、缺口（按影响排序）

**1. 打包与发版之间没有自动闸门（中）**

`build.yml` 在推 `v*` 时**不跑单元测试**，直接打包装；`tests.yml` 同时刻在跑测试，
但两者是并行关系，谁先结束不确定。也就是说：一个测试是红的提交，只要打了标签，
包照样会被构建出来并上传成构件。现在没出事，靠的是「本机先跑全量再打标签」这个人工纪律。

- 建议：在 `build.yml` 的打包步骤之前加一步 `python -m pytest -q -rs`（Windows 只要
  约 6 分钟），或者在 `build` job 里 `needs` 一个测试 job。

**2. 打包产物的冒烟结果不判失败（中）**

`tools/build_zip.py:330-338` 打完包会跑 `xdao-export.exe --version` 与 `--selftest`，
但**只把退出码打印出来**，不参与成败判定：

```python
for extra in (["--version"], ["--selftest"]):
    done = subprocess.run([str(exe), *extra], capture_output=True, text=True, ...)
    tail = " | ".join((done.stdout or done.stderr or "").strip().splitlines()[-2:])
    print(f"  {' '.join(extra)} → 退出码 {done.returncode}：{tail}", flush=True)
```

`--selftest` 在无浏览器的 runner 上必然非 0，直接判错会把 CI 红成噪音；但 `--version`
是纯本地操作，可以断言输出里带 `__version__` 且退出码为 0。这一条改起来只有几行。

**3. `version-consistency` 只比版本号（低—中）**

它只检查「tag 名 == `xdao.__version__`」。`docs/RELEASE_NOTES_vX.md` 是否存在、README 的
「最新版 vX」与附件表 zip 名是否一致、`packaging/使用说明.txt` 里有没有该版本号
—— 这些都由 `tools/build_zip.py:153 check_docs()` 在打包时核，但**打包成功不等于
Release 正文与附件正确**（发布正文是 `tools/make_release.py` 另外贴的）。

- 建议：把 `check_docs()` 的检查搬一条进 `version-consistency` job（它已经 checkout 了）。

**4. 没有 `timeout-minutes`（低—中）**

两个工作流里一处都没有。某个用例挂住时会一直跑到 GitHub 默认上限（360 分钟），
期间并发组里后续推送全部被 cancel。

- 建议：`tests` 的 job 加 `timeout-minutes: 30`，`build` 加 `40`。

**5. 没有 pip 缓存（低）**

每次运行都重装 pytest 与 ruff，4–12 秒。相对 13 分钟的总时长可以忽略，但加
`actions/cache` / `--cache-dir` 是零风险收益。

**6. 依赖版本只有一半钉住（低）**

| 依赖 | 钉住了吗 |
| --- | --- |
| `ruff` | ✅ `ruff==0.17.5` |
| `pytest` | ❌ `python -m pip install pytest` |
| `PyInstaller` | ❌ `python -m pip install pyinstaller` |

`pytest` 漂移会让「某个版本上突然红」变得难以复现；`PyInstaller` 漂移会让**同一个 tag
在不同日子打出不同字节的包**（打包不可复现）。两者都建议钉到具体版本。

**7. `tests.yml` 在每个分支上都跑全矩阵（低）**

`push: branches: ["**"]` 意味着任何临时分支推送都吃满 3 个 job × ~13 分钟。发布流程里
会有 `opt/*`、`docs/*`、`chore/*` 这类分支，属于纯文档改动也会跑满。

- 建议：保持 `pull_request` 与 `master` 全矩阵，其它分支只跑 windows 单 job（或加
  `paths-ignore` 排除纯 `docs/**` 改动）。

**8. 仓库侧协作开关（低，但值得配）**

实测（`gh api`）：

| 开关 | 现状 |
| --- | --- |
| `master` 分支保护 | **未设置**（`404 Branch not protected`）—— 可以直接强推、可以绕过 CI 合入 |
| secret scanning | 已启用，push protection 也已启用 |
| Dependabot 安全更新 | **已禁用**；仓库里也没有 `.github/dependabot.yml` |
| Discussions / Wiki | 都关闭 |
| `delete_branch_on_merge` | 已启用 |

对单人维护的项目，分支保护的意义有限；但「CI 绿才能合入」是这里唯一能防住「手滑推错」的
机制。最低成本的配法：只对 `master` 开「Require status checks to pass」并勾上
`tests` 的三个 job 名。Dependabot 对「钉死版本」的项目反而不太必要（不会自动跟进安全补丁
之外的升级），可以继续关着。

## 五、建议清单（按性价比）

| 优先级 | 建议 | 改动量 |
| --- | --- | --- |
| 1 | `build.yml` 打包前先跑一次单元测试（或 `needs` 测试 job） | 3–5 行 |
| 2 | `build_zip.py` 的 `--version` 冒烟改成断言（`--selftest` 保持只打印） | 5 行 |
| 3 | 两个工作流加 `timeout-minutes` | 2 行 |
| 4 | `pytest` 与 `PyInstaller` 钉版本 | 2 行 |
| 5 | `version-consistency` job 里加 `check_docs()` 同款检查 | 10 行 |
| 6 | `master` 开必需状态检查 | 仓库设置，无代码 |
| 7 | pip 缓存 | 4 行 |
| 8 | 非主分支只跑 windows 单 job | 3 行 |

## 六、范围说明

- 只评估了两个工作流与仓库开关；没有评估 GitHub Actions 的计费、构件保留期（当前用默认
  90 天）与 SignPath 的申请状态（后者见 `docs/SIGNING.md`）。
- 运行数据取自 `gh run list` 最近 25 次与 run `43620448397` 的逐步骤耗时；更早的历史没有逐条看。
- 所有结论都可用 `gh run list` / `gh api` 复现，没有改仓库里的任何文件。
