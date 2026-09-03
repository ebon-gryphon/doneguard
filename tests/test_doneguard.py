from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "doneguard.py"
SPEC = importlib.util.spec_from_file_location("doneguard", SCRIPT)
assert SPEC and SPEC.loader
doneguard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doneguard)


class DoneGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.data = self.root / "data"
        self.repo.mkdir()
        self.data.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "doneguard@example.test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "DoneGuard Test"], check=True)
        (self.repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.env = mock.patch.dict(os.environ, {"PLUGIN_DATA": str(self.data)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def event(self, hook: str, **extra):
        value = {
            "session_id": "session-test",
            "cwd": str(self.repo),
            "hook_event_name": hook,
            "turn_id": "turn-1",
        }
        value.update(extra)
        return value

    def start_and_edit(self) -> None:
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        (self.repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="apply_patch",
            tool_input={"command": "*** Update File: app.py"},
            tool_response={"output": "Done"},
        ))

    def test_warns_when_code_changed_without_verification(self) -> None:
        self.start_and_edit()
        result = doneguard.handle_hook(self.event("Stop", stop_hook_active=False))
        self.assertIn("systemMessage", result)
        self.assertIn("no successful test", result["systemMessage"])

    def test_strict_mode_requests_one_continuation(self) -> None:
        (self.repo / ".doneguard.json").write_text('{"mode":"strict"}\n', encoding="utf-8")
        self.start_and_edit()
        first = doneguard.handle_hook(self.event("Stop", stop_hook_active=False))
        second = doneguard.handle_hook(self.event("Stop", stop_hook_active=True))
        self.assertEqual(first.get("decision"), "block")
        self.assertNotIn("decision", second)
        self.assertIn("systemMessage", second)

    def test_successful_verification_clears_missing_evidence(self) -> None:
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_response={"exit_code": 0, "output": "1 passed"},
        ))
        result = doneguard.handle_hook(self.event("Stop", stop_hook_active=False))
        self.assertIn("systemMessage", result)
        self.assertNotIn("Needs attention", result["systemMessage"])
        self.assertIn("successful verification", result["systemMessage"])

    def test_failed_verification_is_reported(self) -> None:
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "python3 -m pytest"},
            tool_response=json.dumps({"exit_code": 1, "output": "failed"}),
        ))
        result = doneguard.handle_hook(self.event("Stop", stop_hook_active=False))
        self.assertIn("latest recorded test command failed", result["systemMessage"])

    def test_successful_verification_uses_transcript_fallback(self) -> None:
        self.start_and_edit()
        transcript = self.root / "rollout.jsonl"
        transcript.write_text(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "exec-success",
                    "command": ["/bin/zsh", "-lc", "npm test"],
                    "status": "completed",
                    "exit_code": 0,
                },
            },
        }) + "\n", encoding="utf-8")

        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="Bash",
            tool_use_id="exec-success",
            tool_input={"command": "npm test"},
            tool_response={"output": "4 tests passed"},
            transcript_path=str(transcript),
        ))

        result = doneguard.handle_hook(self.event("Stop", stop_hook_active=False))
        self.assertNotIn("Needs attention", result["systemMessage"])
        self.assertIn("successful verification", result["systemMessage"])

    def test_failed_verification_uses_transcript_command_fallback(self) -> None:
        self.start_and_edit()
        transcript = self.root / "rollout.jsonl"
        transcript.write_text(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "different-id",
                    "command": ["/bin/zsh", "-lc", "npm test"],
                    "status": "completed",
                    "exit_code": 1,
                },
            },
        }) + "\n", encoding="utf-8")

        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="Bash",
            tool_use_id="exec-failure",
            tool_input={"command": "npm test"},
            tool_response={"output": "tests failed"},
            transcript_path=str(transcript),
        ))

        result = doneguard.handle_hook(self.event("Stop", stop_hook_active=False))
        self.assertIn("latest recorded test command failed", result["systemMessage"])

    def test_observe_mode_saves_without_ui_output(self) -> None:
        (self.repo / ".doneguard.json").write_text('{"mode":"observe"}\n', encoding="utf-8")
        self.start_and_edit()
        result = doneguard.handle_hook(self.event("Stop", stop_hook_active=False))
        self.assertIsNone(result)
        self.assertIsNotNone(doneguard.latest_report(self.repo))

    def test_bash_edit_makes_previous_verification_stale(self) -> None:
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_response={"exit_code": 0},
        ))
        (self.repo / "app.py").write_text("def value():\n    return 3\n", encoding="utf-8")
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "python3 rewrite.py"},
            tool_response={"exit_code": 0},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("no successful test", message)
        self.assertIn("evidence is stale", message)

    def test_later_success_supersedes_same_command_failure(self) -> None:
        self.start_and_edit()
        for exit_code in (1, 0):
            doneguard.handle_hook(self.event(
                "PostToolUse",
                tool_name="Bash",
                tool_input={"command": "pytest -q"},
                tool_response={"exit_code": exit_code},
            ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertNotIn("Needs attention", message)
        self.assertIn("successful verification", message)

    def test_untracked_debug_marker_is_reported(self) -> None:
        self.start_and_edit()
        (self.repo / "scratch.py").write_text("# TODO remove\n", encoding="utf-8")
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="apply_patch",
            tool_input={"command": "*** Add File: scratch.py"},
        ))
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_response={"exit_code": 0},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("scratch.py:1: TODO/FIXME/HACK", message)

    def test_marker_inside_string_is_not_reported(self) -> None:
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        (self.repo / "app.py").write_text('LABEL = "TODO"\n', encoding="utf-8")
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="apply_patch",
            tool_input={"command": "*** Update File: app.py"},
        ))
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_response={"exit_code": 0},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertNotIn("debug or temporary markers", message)

    def test_debug_marker_inline_allow_is_honored(self) -> None:
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        (self.repo / "app.py").write_text("# TODO retained; doneguard: allow-debug\n", encoding="utf-8")
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="apply_patch",
            tool_input={"command": "*** Update File: app.py"},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertNotIn("debug or temporary markers", message)

    def test_malformed_config_is_reported(self) -> None:
        (self.repo / ".doneguard.json").write_text("{bad-json}\n", encoding="utf-8")
        self.start_and_edit()
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("Invalid .doneguard.json", message)

    def test_config_is_loaded_from_repository_root(self) -> None:
        (self.repo / ".doneguard.json").write_text('{"mode":"strict"}\n', encoding="utf-8")
        subdir = self.repo / "src"
        subdir.mkdir()
        self.start_and_edit()
        result = doneguard.handle_hook(self.event("Stop", cwd=str(subdir), stop_hook_active=False))
        self.assertEqual(result.get("decision"), "block")

    def test_python_unittest_is_recognized(self) -> None:
        self.assertEqual(doneguard.classify_verification("python3 -m unittest -v"), "test")

    def test_custom_verification_command_is_recognized(self) -> None:
        config = {
            "verification_commands": [
                {"kind": "lint", "command_prefix": "./scripts/quality"}
            ]
        }
        self.assertEqual(doneguard.classify_verification("./scripts/quality --fast", config), "lint")

    def test_sensitive_command_values_are_redacted(self) -> None:
        command = "API_TOKEN=secret tool --password hunter2 --api-key=abc"
        redacted = doneguard.redact_command(command)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("abc", redacted)
        self.assertIn("<redacted>", redacted)

    def test_user_prompt_text_is_not_persisted(self) -> None:
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        doneguard.handle_hook(self.event("UserPromptSubmit", prompt="private prompt text"))
        state = json.loads(doneguard.state_path("session-test").read_text())
        self.assertEqual(state["prompt_count"], 1)
        self.assertNotIn("last_prompt", state)

    def test_report_contains_fingerprint_and_evidence_source(self) -> None:
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_response={"exit_code": 0},
        ))
        doneguard.handle_hook(self.event("Stop"))
        report = doneguard.latest_report(self.repo)
        self.assertEqual(report["schema_version"], 3)
        self.assertTrue(report["workspace_fingerprint"].startswith("sha256:"))
        self.assertEqual(report["verification_evidence"][0]["exit_code_source"], "tool_response")

    def configure_required_coverage(self, lines: float = 90) -> None:
        config = {
            "schema_version": 2,
            "mode": "warn",
            "verification_commands": [{
                "id": "python-tests",
                "kind": "test",
                "command": "pytest -q",
                "required": True,
                "covers": ["app.py"],
                "artifacts": [{
                    "path": "coverage/summary.json",
                    "format": "coverage-summary",
                    "thresholds": {"lines": 80, "branches": 70},
                    "max_age_seconds": 60,
                }],
            }],
        }
        (self.repo / ".doneguard.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        coverage = self.repo / "coverage"
        coverage.mkdir(exist_ok=True)
        (coverage / "summary.json").write_text(
            json.dumps({"lines": lines, "branches": 75}) + "\n", encoding="utf-8"
        )

    def test_required_rule_maps_changed_paths_and_accepts_coverage(self) -> None:
        self.configure_required_coverage()
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_response={"exit_code": 0},
        ))
        result = doneguard.handle_hook(self.event("Stop"))
        self.assertNotIn("Needs attention", result["systemMessage"])
        report = doneguard.latest_report(self.repo)
        self.assertEqual(report["verification_coverage"]["python-tests"], ["app.py"])
        self.assertIn("lines 90.00%", result["systemMessage"])

    def test_coverage_below_threshold_blocks_evidence(self) -> None:
        self.configure_required_coverage(lines=50)
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_response={"exit_code": 0},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("lines coverage 50.00% is below 80.00%", message)
        self.assertIn("Needs attention", message)

    def test_artifact_changed_after_verification_is_rejected(self) -> None:
        self.configure_required_coverage()
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="Bash",
            tool_input={"command": "pytest -q"},
            tool_response={"exit_code": 0},
        ))
        (self.repo / "coverage" / "summary.json").write_text(
            json.dumps({"lines": 99, "branches": 99}) + "\n", encoding="utf-8"
        )
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("artifact changed after verification", message)

    def test_uncovered_changed_code_is_reported(self) -> None:
        config = {
            "schema_version": 2,
            "verification_commands": [{
                "id": "src-tests", "kind": "test", "command": "pytest -q",
                "required": True, "covers": ["src/**"],
            }],
        }
        (self.repo / ".doneguard.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="Bash",
            tool_input={"command": "pytest -q"}, tool_response={"exit_code": 0},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("not covered by a required verification rule: app.py", message)

    def test_fingerprint_budget_failure_is_visible(self) -> None:
        (self.repo / ".doneguard.json").write_text(
            json.dumps({"schema_version": 2, "fingerprint_limits": {"max_total_bytes": 1}}) + "\n",
            encoding="utf-8",
        )
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="Bash",
            tool_input={"command": "pytest -q"}, tool_response={"exit_code": 0},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("workspace fingerprint is incomplete", message)
        report = doneguard.latest_report(self.repo)
        self.assertFalse(report["fingerprint_metrics"]["complete"])

    def test_fingerprint_cache_is_reused(self) -> None:
        self.start_and_edit()
        config, _ = doneguard.load_config(self.repo)
        cache = {}
        first = doneguard.workspace_fingerprint_details(self.repo, ["app.py"], config, cache)
        second = doneguard.workspace_fingerprint_details(self.repo, ["app.py"], config, cache)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(second["metrics"]["cache_hits"], 1)

    def test_fingerprint_batches_large_changed_sets(self) -> None:
        paths = []
        for index in range(401):
            path = f"generated/source_{index:03d}.py"
            target = self.repo / path
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"VALUE = {index}\n", encoding="utf-8")
            paths.append(path)
        config, _ = doneguard.load_config(self.repo)
        cache = {}
        first = doneguard.workspace_fingerprint_details(self.repo, paths, config, cache)
        second = doneguard.workspace_fingerprint_details(self.repo, paths, config, cache)
        self.assertTrue(first["metrics"]["complete"])
        self.assertEqual(first["metrics"]["processed_files"], 401)
        self.assertEqual(first["metrics"]["algorithm"], "merkle-v1")
        self.assertEqual(first["metrics"]["chunk_count"], 2)
        self.assertEqual(second["metrics"]["cache_hits"], 401)

    def test_fingerprint_cache_persists_across_calls(self) -> None:
        self.start_and_edit()
        config, _ = doneguard.load_config(self.repo)
        first = doneguard.cached_workspace_fingerprint(self.repo, ["app.py"], config)
        second = doneguard.cached_workspace_fingerprint(self.repo, ["app.py"], config)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(second["metrics"]["cache_hits"], 1)

    def test_command_prefix_requires_token_boundary(self) -> None:
        config = {
            "verification_commands": [{
                "kind": "test", "command_prefix": "make test",
            }],
        }
        self.assertIsNone(doneguard.classify_verification("make tester", config))

    def test_structured_argv_accepts_environment_prefix(self) -> None:
        config = {
            "verification_commands": [{
                "id": "node-tests", "kind": "test", "argv": ["make", "test"],
                "cwd": ".", "structured": True,
            }],
        }
        self.assertEqual(
            doneguard.classify_verification("NODE_BIN=/opt/node make test", config, self.repo),
            "test",
        )

    def test_structured_argv_rejects_compound_command(self) -> None:
        config = {
            "verification_commands": [{
                "id": "node-tests", "kind": "test", "argv": ["make", "test"],
                "cwd": ".", "structured": True,
            }],
        }
        self.assertIsNone(
            doneguard.classify_verification("make test && false", config, self.repo)
        )

    def test_structured_argv_checks_working_directory(self) -> None:
        config = {
            "verification_commands": [{
                "id": "node-tests", "kind": "test", "argv": ["make", "test"],
                "cwd": "tools", "structured": True,
            }],
        }
        self.assertIsNone(doneguard.classify_verification("make test", config, self.repo))

    def test_schema3_required_rule_rejects_heuristic_selector(self) -> None:
        config = {
            "schema_version": 3,
            "verification_commands": [{
                "id": "unsafe-tests", "kind": "test", "pattern": "pytest",
                "required": True, "when_changed": ["app.py"],
            }],
        }
        (self.repo / ".doneguard.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        self.start_and_edit()
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="Bash",
            tool_input={"command": "pytest -q"}, tool_response={"exit_code": 0},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("Schema 3 requires argv", message)

    def test_non_code_trigger_invalidates_required_evidence(self) -> None:
        config = {
            "schema_version": 3,
            "verification_commands": [{
                "id": "docs-check", "kind": "test", "argv": ["make", "test"],
                "required": True, "when_changed": ["docs/**"],
                "fingerprint_paths": ["docs/**"],
            }],
        }
        (self.repo / ".doneguard.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        docs = self.repo / "docs"
        docs.mkdir()
        guide = docs / "guide.md"
        guide.write_text("version one\n", encoding="utf-8")
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="apply_patch",
            tool_input={"command": "*** Add File: docs/guide.md"},
        ))
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="Bash",
            tool_input={"command": "make test"}, tool_response={"exit_code": 0},
        ))
        first = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertNotIn("Needs attention", first)
        guide.write_text("version two\n", encoding="utf-8")
        second = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("required verification docs-check was not recorded", second)

    def test_multiline_python_string_does_not_trigger_marker(self) -> None:
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        (self.repo / "app.py").write_text(
            'TEXT = """first line\nTODO is data\nlast line"""\n', encoding="utf-8"
        )
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="apply_patch",
            tool_input={"command": "*** Update File: app.py"},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertNotIn("debug or temporary markers", message)

    def test_multiline_javascript_template_does_not_trigger_marker(self) -> None:
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        (self.repo / "scratch.js").write_text(
            "const label = `first line\nTODO is data\nlast line`;\n", encoding="utf-8"
        )
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="apply_patch",
            tool_input={"command": "*** Add File: scratch.js"},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertNotIn("debug or temporary markers", message)

    def test_javascript_template_expression_is_scanned(self) -> None:
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        (self.repo / "scratch.js").write_text(
            'const label = `value ${console.log("debug")}`;\n', encoding="utf-8"
        )
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("console.log", message)

    def test_javascript_regex_literal_does_not_trigger_marker(self) -> None:
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        (self.repo / "scratch.js").write_text(
            "const marker = /TODO|FIXME/;\n", encoding="utf-8"
        )
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertNotIn("debug or temporary markers", message)

    def test_incomplete_debug_scan_is_reported(self) -> None:
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        (self.repo / "scratch.py").write_text('TEXT = """unterminated\n', encoding="utf-8")
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("debug marker scan is incomplete", message)
        report = doneguard.latest_report(self.repo)
        self.assertFalse(report["debug_scan"]["complete"])

    def test_configured_high_confidence_marker_blocks(self) -> None:
        config = {
            "schema_version": 2,
            "debug_markers": {"block": ["debugger"]},
        }
        (self.repo / ".doneguard.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        doneguard.handle_hook(self.event("SessionStart", source="startup"))
        (self.repo / "app.py").write_text("debugger;\n", encoding="utf-8")
        doneguard.handle_hook(self.event(
            "PostToolUse", tool_name="apply_patch",
            tool_input={"command": "*** Update File: app.py"},
        ))
        message = doneguard.handle_hook(self.event("Stop"))["systemMessage"]
        self.assertIn("blocking debug markers found", message)


if __name__ == "__main__":
    unittest.main()
