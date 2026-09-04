---
name: doneguard
description: Configure or explain DoneGuard completion checks for Codex. Use when the user mentions DoneGuard, asks whether Codex actually finished, wants a completion-evidence report, or wants to change DoneGuard modes. Do not invoke for ordinary code review requests.
---

# DoneGuard

DoneGuard is a local completion guard for Git projects and protected Codex engineering assets. Its hooks record supported verification commands and inspect changes from the current user turn before Codex stops. Successful verification is tied to a fingerprint of the relevant changed content, so later edits invalidate older evidence regardless of which editing tool produced them.

Read-only turns must stay silent even when the current Git repository already contains unrelated dirty files. A report is eligible only when the current turn changes a protected scope or runs verification for one. Protected scopes are Git repositories, non-Git directories explicitly opted in with `.doneguard.json`, global Codex skills/plugins/bin/configuration under `CODEX_HOME`, and global agent skills/plugins under `~/.agents`. Determine the scope from the edited files rather than assuming every edit belongs to the chat's starting directory. Identical workspace findings should not notify repeatedly until the fingerprint or findings change.

## Modes

Read `.doneguard.json` from the project root when it exists. The default mode is `warn`.

- `observe`: save reports without interrupting the chat.
- `warn`: show a completion report but allow the turn to finish.
- `strict`: ask Codex to continue once when blocking evidence is missing or failed.

When the optional macOS Companion is installed, `warn` and the final `strict` stop deliver the report outside the project as a compact, non-activating upper-right notification. Keep the capybara beside the actions instead of making it the main content. Only the user's explicit View Report action should open a centered, focused report window. If Companion cannot be launched, preserve the inline `systemMessage` fallback. Never describe the first strict continuation as task completion.

When the user asks to change modes, create or update `.doneguard.json` while preserving unrelated fields:

```json
{
  "mode": "strict"
}
```

Supported optional fields are:

- `require_verification_when_code_changed` (boolean, default `true`)
- `block_on_failed_verification` (boolean, default `true`)
- `block_on_debug_markers` (boolean, default `false`)
- `block_on_sensitive_files` (boolean, default `false`)
- `ignore_paths` (array of repository-relative path prefixes)
- `debug_marker_ignore_paths` (array of repository-relative path prefixes)
- `verification_commands` (array of custom command recognizers)
- `debug_markers` (object containing `block`, `warn`, `ignore_paths`, and `allow_comment`)
- `fingerprint_limits` (object containing positive `max_files`, `max_total_bytes`, and `timeout_ms` budgets)
- `companion_enabled` (boolean, default `true`)
- `notification_policy` (`always`, `issues_only`, or `never`)
- `temporary_report_ttl_hours` (positive integer, default `24`)

Schema 3 custom verification rules have a `kind` of `test`, `lint`, `typecheck`, or `build`. Required rules should use a structured `argv` selector plus an optional repository-relative `cwd`. `when_changed` selects changes that require the rule, while `fingerprint_paths` adds non-code inputs that invalidate existing evidence:

```json
{
  "verification_commands": [
    {
      "id": "unit-tests",
      "kind": "test",
      "argv": ["make", "test"],
      "cwd": ".",
      "required": true,
      "when_changed": ["src/**", "test/**", "package.json", "README.md"],
      "fingerprint_paths": ["src/**", "test/**", "package.json", "README.md"],
      "artifacts": [
        {
          "path": "coverage/coverage-summary.json",
          "format": "coverage-summary",
          "thresholds": {"lines": 80, "branches": 75},
          "max_age_seconds": 120
        }
      ]
    }
  ]
}
```

DoneGuard only recognizes commands that Codex already ran; it does not execute configured commands. Schema 3 structured rules reject compound commands, redirections, command substitutions, and working-directory mismatches instead of falling back to heuristic evidence. Schema 1 and 2 `command`, `command_prefix`, `pattern`, and `covers` fields remain compatible, but a required Schema 3 rule using one of those heuristic selectors is reported as unsafe.

Rules can validate `coverage-summary` or `istanbul-summary` JSON artifacts. For repeated runs of the same rule, only the newest result for the current workspace fingerprint determines its status. A line containing the configured `allow_comment` value is exempt from debug-marker reporting. Debug scan reports identify the language engine and whether scanning completed; an incomplete scan must remain visible as a warning.

Fingerprint reports use a Merkle root and include chunk counts, file counts, bytes hashed, persistent cache hits, duration, completeness, and any budget limit reached. Cache entries are stored per repository under `PLUGIN_DATA`. An incomplete fingerprint is never accepted as fresh verification evidence.

Do not enable a blocking option unless the user requests it. Explain that strict mode performs at most one automatic continuation per stop cycle to avoid loops.

User-facing report bundles stay under `PLUGIN_DATA/reports/temporary` until the user explicitly saves or discards them. Saved reports move to `PLUGIN_DATA/reports/saved`; unopened temporary reports expire on a later check after the configured TTL. The rolling `reports/latest.json` is operational state, not a user-saved history. Do not claim a report was saved unless the user chose Save.

## Interpreting reports

Treat DoneGuard findings as completion evidence, not proof of correctness. A passing report means relevant recorded checks succeeded after the most recent observed edit. It does not replace code review or guarantee that tests cover the requested behavior.

If the user asks for the latest report, locate the plugin's `scripts/doneguard.py` from this skill directory and run:

```text
python3 <plugin-root>/scripts/doneguard.py status --cwd <project-root>
```

Add `--json` when a machine-readable report is needed.

Summarize the result in plain language. Do not claim that a command ran if DoneGuard recorded an unknown exit status.
