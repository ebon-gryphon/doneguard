# DoneGuard

DoneGuard 是一个面向 Codex 的本地完成检查插件。它会观察代码改动和验证命令，在任务结束前检查当前工作区是否具备足够的新鲜验证证据。可选的 macOS Companion 会在项目外显示一条带二次元水豚的右上角通知，并在用户查看完整报告后再询问是否保存。

当前版本默认保护 Git 仓库，也会保护全局 Skill、插件和 Codex CLI 配置；非 Git 目录可以通过 `.doneguard.json` 显式启用。默认模式为 `warn`，它会展示报告，但不会阻止任务结束。

DoneGuard 以“本轮是否修改了受保护的工程资产”为触发条件。只读问答、新闻搜索和资料查询不会因为工作区里早先遗留的未提交改动而生成新报告。同一个工作区指纹和问题状态只通知一次，内容或验证状态变化后才会再次通知。

## 安装和更新

### 最省事的安装方法

你不用自己下载文件，也不用打开终端。照着下面几步操作即可。

1. 打开 Codex，确认当前任务使用本地环境。
2. 新建一个任务。
3. 复制下面方框里的全部文字。
4. 把文字发给 Codex，等它安装完成。
5. 如果 Codex 询问是否允许下载或安装，请先看清操作内容，再点击允许。

```text
请帮我安装 DoneGuard。
项目地址是 https://github.com/ebon-gryphon/doneguard
请把项目下载到 ~/plugins/doneguard，加入我的 Personal marketplace，安装插件，并在 macOS 上安装 Companion。完成后请检查是否安装成功，并告诉我结果。
```

看到安装成功的回复以后，再新建一个任务。DoneGuard 会从新任务开始工作。

DoneGuard 目前还不能在公开插件目录里搜索。Personal marketplace 是保存在你电脑上的个人插件列表，公开插件商店是另一套目录。上面的安装过程会由 Codex 处理，不需要你事先配置 Personal marketplace。

### 更新

已经安装过 DoneGuard 时，也不用自己寻找文件。新建一个 Codex 任务，把下面这段话完整发给 Codex。

```text
请帮我更新 DoneGuard。
项目地址是 https://github.com/ebon-gryphon/doneguard
请下载最新版本，重新安装插件并更新 macOS Companion。完成后请检查是否更新成功，并告诉我结果。
```

更新完成后，再新建一个任务，让 Codex 加载新版 Skill 和 Hook。

### 手动重新安装

如果你只是想使用 DoneGuard，可以跳过这一节。下面的命令只适合已经把 DoneGuard 加入本机 Personal marketplace，并且熟悉终端的用户。

```bash
codex plugin add doneguard@personal
zsh /path/to/doneguard/scripts/install_companion_macos.sh
```

## 它解决什么问题

一次测试通过以后，代码仍可能继续变化。DoneGuard 会为相关代码计算工作区指纹，并把指纹写入验证记录。任务结束时，当前指纹必须与成功验证时的指纹一致，旧测试结果才会被采纳。

这套检查还能发现下面几类情况。

- 代码已经改变，但没有记录到成功的测试、lint、类型检查或构建。
- 当前工作区对应的最新验证命令失败，或退出状态未知。
- 改动中出现临时调试标记。
- `.env`、密钥和凭据类文件发生变化。

DoneGuard 提供的是完成证据。报告通过说明插件观察到的相关检查成功，并且这些检查发生在当前代码状态下。它不能替代代码审查，也不能证明测试覆盖了需求。

## 三种模式

| 模式 | 行为 |
| --- | --- |
| `observe` | 更新滚动状态，不弹窗也不打断对话 |
| `warn` | Companion 已安装时弹窗，否则在对话中提示；允许任务结束 |
| `strict` | 缺少阻断证据时请求 Codex 继续一次 |

`strict` 每个停止周期最多自动续跑一次，避免检查进入循环。

## 项目配置

在 Git 仓库根目录创建 `.doneguard.json`。也可以在非 Git 工程目录中创建该文件，将该目录显式纳入保护。最小配置如下。

```json
{
  "mode": "warn"
}
```

完整配置示例。

```json
{
  "schema_version": 3,
  "mode": "strict",
  "companion_enabled": true,
  "notification_policy": "always",
  "temporary_report_ttl_hours": 24,
  "require_verification_when_code_changed": true,
  "block_on_failed_verification": true,
  "block_on_debug_markers": false,
  "block_on_sensitive_files": false,
  "ignore_paths": [
    "dist/",
    "build/",
    "coverage/",
    "vendor/",
    "node_modules/"
  ],
  "debug_marker_ignore_paths": ["fixtures/"],
  "debug_markers": {
    "block": [],
    "warn": ["TODO/FIXME/HACK", "console.log", "debugger"],
    "ignore_paths": [],
    "allow_comment": "doneguard: allow-debug"
  },
  "fingerprint_limits": {
    "max_files": 10000,
    "max_total_bytes": 536870912,
    "timeout_ms": 3000
  },
  "verification_commands": []
}
```

`notification_policy` 支持 `always`、`issues_only` 和 `never`。`always` 会在最终停止时显示成功或异常卡片；`issues_only` 只在有提醒或阻断项时显示。`strict` 第一次要求 Codex 继续时不会发出“任务完成”弹窗，只有最终停止才会交给 Companion。

不要在没有评估误报风险时开启新的阻断项。调试标记和敏感文件默认只产生警告。

## 验证命令

DoneGuard 内置识别常见的测试、lint、类型检查和构建命令，包括下面这些工具。

- 测试工具包括 `pytest`、`unittest`、Jest、Vitest、Go、Cargo、Gradle、Maven、xcodebuild 和 `make test`。
- lint 工具包括 Ruff、ESLint、Biome、golangci-lint、Clippy 和 `go vet`。
- 类型检查工具包括 TypeScript、mypy、Pyright 和 `cargo check`。
- 构建工具包括 npm、pnpm、Yarn、Bun、Cargo、.NET 和 Maven。

项目可以补充自定义识别规则。Schema 3 的必需规则使用结构化 `argv` 和可选的仓库相对 `cwd`。`when_changed` 表示哪些改动必须触发该检查，`fingerprint_paths` 表示哪些代码或非代码输入会让旧证据失效。

```json
{
  "verification_commands": [
    {
      "id": "unit-tests",
      "kind": "test",
      "argv": ["make", "test"],
      "cwd": ".",
      "required": true,
      "when_changed": [
        "src/**",
        "bin/**",
        "test/**",
        "package.json",
        "README.md"
      ],
      "fingerprint_paths": [
        "src/**",
        "bin/**",
        "test/**",
        "package.json",
        "README.md"
      ],
      "artifacts": [
        {
          "path": "coverage/coverage-summary.json",
          "format": "coverage-summary",
          "thresholds": {
            "lines": 80,
            "branches": 75
          },
          "max_age_seconds": 120
        }
      ]
    }
  ]
}
```

这些配置只用于识别 Codex 已经执行的命令。DoneGuard 不会主动执行配置中的内容。结构化规则会拒绝复合 Shell 命令、重定向、命令替换和工作目录不匹配，避免弱匹配结果被当作必需证据。Schema 1 和 2 的 `command`、`command_prefix`、`pattern` 与 `covers` 仍然兼容，但 Schema 3 不接受启发式选择器充当必需规则。

## 验证结果怎样生效

验证完成后，DoneGuard 会记录下面这些信息。

- 验证类型和经过脱敏的命令。
- 退出码及其来源。
- 执行时间。
- 当时相关代码的工作区指纹。

同一工作区指纹下，同一个验证规则只采用最新结果。一次失败后重新运行并成功，旧失败会被新结果覆盖。验证以后再次修改 `when_changed` 或 `fingerprint_paths` 命中的代码、配置、迁移、文档等输入，Merkle 指纹随之变化，原有证据会被标记为过期。必需规则还会验证覆盖率产物的哈希、新鲜度和阈值。

## 何时保持安静

DoneGuard 在每次用户提示开始时记录当前 Git 状态，并只为本轮发生的受保护变更生成报告。下面几类任务默认保持安静。

- 仅搜索新闻、浏览资料或回答问题，没有修改工程文件。
- 当前 Git 仓库虽然已有脏文件，但本轮没有继续修改，也没有运行验证。
- 在未配置 `.doneguard.json` 的普通非 Git 目录编辑一般文件。

下面几类修改仍会触发检查。

- 当前 Git 仓库或本轮实际触碰到的其他 Git 仓库。
- `$CODEX_HOME/skills`、`$CODEX_HOME/plugins`、`$CODEX_HOME/bin`、`config.toml` 和 `AGENTS.md`。
- `~/.agents/skills` 与 `~/.agents/plugins`。
- 使用 `.doneguard.json` 显式启用的非 Git 目录。

报告作用域由实际修改的文件决定，不要求它们位于聊天启动目录中。全局工程资产使用文件内容指纹，而不是依赖 Git。相同指纹、提醒和阻断项已经通知过时，后续无状态变化的停止事件不会重复弹窗。

## 调试标记和敏感文件

DoneGuard 会检查已跟踪改动和未跟踪源文件中的新增内容。目前内置识别 `TODO`、`FIXME`、`HACK`、`console.log`、`debugger`、Python 断点和 Ruby `binding.pry`。Python 文件使用标准库 `tokenize`，JavaScript 和 TypeScript 使用支持注释、正则、模板字符串及 `${...}` 表达式的语言词法扫描器，其他语言使用通用扫描器。

测试夹具或示例代码确实需要保留标记时，可以在同一行加入下面的豁免说明。

```text
doneguard: allow-debug
```

也可以通过 `debug_marker_ignore_paths` 排除整个路径。敏感路径检测覆盖 `.env`、证书私钥和 credentials、secret 等常见命名。

## 查看最新报告

```bash
python3 <plugin-root>/scripts/doneguard.py status --cwd <project-root>
```

需要机器可读结果时加入 `--json`。

```bash
python3 <plugin-root>/scripts/doneguard.py status --cwd <project-root> --json
```

报告首先用中文说明“检查了什么、为什么能或不能确认完成、接下来建议怎么做”。测试命令、英文原始证据、工作区 Merkle 指纹、分块数、性能指标、跨会话缓存命中、改动路径、覆盖映射和调试扫描结果仍会保留，但会放进次要的技术详情中。这样普通用户可以先看懂结论，开发者仍能追查原始判断依据。退出状态未知的命令和不完整指纹都不会被算作成功。

## macOS 水豚 Companion

Companion 是独立于项目目录的轻量 SwiftUI 应用。源码安装会在 DoneGuard 的插件数据目录中构建 `DoneGuard Companion.app`，不会向用户的代码仓库写入报告文件。

```bash
zsh /path/to/doneguard/scripts/install_companion_macos.sh
```

工作流程如下。

- Hook 只保留一份滚动的 `reports/latest.json`，并为待查看的报告创建临时 bundle。
- Companion 收到最终停止事件后，在屏幕右上角显示一条接近 macOS 系统横幅尺寸的非激活式通知，不会抢走当前应用焦点。水豚缩在内容左侧，“查看报告”和“稍后”分列操作区两端以减少误触；只有用户主动点击“查看报告”后，独立的不透明报告窗口才会居中并取得焦点。没有安装 Companion 时仍使用聊天内提示。
- 用户打开完整报告后可以选择“保存报告”或“关闭且不保存”。保存后 bundle 进入 `reports/saved/`，不保存则立即删除。
- 用户没有作出选择而关闭窗口时，报告仍是临时数据；默认 24 小时后由下一次检查清理。
- Companion 不保存用户提示词，也不复制项目源文件内容。报告只包含 DoneGuard 已有的证据、路径和检查结果。

需要脚本化管理临时报告时可以使用下面的命令。

```bash
python3 /path/to/doneguard/scripts/doneguard.py report-action save <report-id>
python3 /path/to/doneguard/scripts/doneguard.py report-action discard <report-id>
```

## 开发验证

插件本身只依赖 Python 标准库。

```bash
cd /path/to/doneguard
python3 -m unittest -v tests/test_doneguard.py
python3 -m py_compile scripts/doneguard.py
```

当前插件包含 56 项 Python 单元测试和一项 Swift 报告删除烟雾测试；原有黑盒与端到端验证项目继续保留。测试覆盖 Companion 缺失时的安全降级、临时报告事件、明确保存与删除、中文小白解释、过期清理、只读任务静默、跨仓库和全局资产作用域、重复通知抑制，以及 strict 首次续跑不误发完成弹窗。

## 当前边界

- 非代码文件需要由项目在 `when_changed` 或 `fingerprint_paths` 中声明；DoneGuard 无法自动判断一份业务文档是否影响运行结果。
- Python 使用标准分词器，JavaScript 和 TypeScript 使用语言词法扫描器，但后者仍不是完整 AST；无法完整扫描、文件过大或语法未闭合时会显式警告，不会静默声称扫描完整。
- Schema 3 的必需规则必须使用结构化 `argv`。内置命令和旧版启发式选择器只作为兼容与便捷识别，不应承担高可信阻断判断。
- Merkle 指纹支持 Git 批量哈希和跨会话缓存；预算超限仍会显式产生不完整证据并拒绝通过，大型仓库应根据指标调整规则分片与预算。
- 通过报告仍然取决于项目测试本身的质量。
- Companion MVP 目前只支持 macOS 13 及以上版本，使用本机 Swift 编译；应用没有 Developer ID 签名，不适合作为公开下载包直接分发。
- 当前视觉覆盖“完成”和“发现问题”两种主状态：完成时水豚泡在温泉里，发现问题时水豚带着无线电在炮火背景下进行狙击瞄准。warning 暂时复用异常水豚；后续可以继续扩展过期、等待和离线状态。
