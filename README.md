# DoneGuard

DoneGuard 是一个面向 Codex 的本地完成检查插件。它会观察代码改动和验证命令，在任务结束前检查当前工作区是否具备足够的新鲜验证证据。

当前版本适用于 Git 仓库。默认模式为 `warn`，它会展示报告，但不会阻止任务结束。

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
| `observe` | 保存报告，不在对话中提示 |
| `warn` | 展示报告，允许任务结束 |
| `strict` | 缺少阻断证据时请求 Codex 继续一次 |

`strict` 每个停止周期最多自动续跑一次，避免检查进入循环。

## 项目配置

在 Git 仓库根目录创建 `.doneguard.json`。最小配置如下。

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

报告会给出工作区 Merkle 指纹、分块数、性能指标、跨会话缓存命中、改动路径、覆盖映射、调试扫描完整性、验证证据、通过项、警告和阻断项。退出状态未知的命令和不完整指纹都不会被算作成功。

## 安装和更新

DoneGuard 当前通过个人 marketplace 安装。

```bash
codex plugin add doneguard@personal
```

更新插件后请新建 Codex 任务，让新任务加载新的 skill 和 hook。

## 开发验证

插件本身只依赖 Python 标准库。

```bash
cd /path/to/doneguard
python3 -m unittest -v tests/test_doneguard.py
python3 -m py_compile scripts/doneguard.py
```

本次更新包含 39 项插件单元测试、25 项通用黑盒验收测试，以及冷链 Node.js 项目的 8 项端到端测试。测试覆盖 Schema 3 非代码触发、结构化命令与目录约束、覆盖率产物、语言感知调试扫描、扫描完整性、Merkle 指纹预算、401 文件批处理、跨会话缓存、嵌套测试自动发现和空测试集拒绝通过。

## 当前边界

- 非代码文件需要由项目在 `when_changed` 或 `fingerprint_paths` 中声明；DoneGuard 无法自动判断一份业务文档是否影响运行结果。
- Python 使用标准分词器，JavaScript 和 TypeScript 使用语言词法扫描器，但后者仍不是完整 AST；无法完整扫描、文件过大或语法未闭合时会显式警告，不会静默声称扫描完整。
- Schema 3 的必需规则必须使用结构化 `argv`。内置命令和旧版启发式选择器只作为兼容与便捷识别，不应承担高可信阻断判断。
- Merkle 指纹支持 Git 批量哈希和跨会话缓存；预算超限仍会显式产生不完整证据并拒绝通过，大型仓库应根据指标调整规则分片与预算。
- 通过报告仍然取决于项目测试本身的质量。
