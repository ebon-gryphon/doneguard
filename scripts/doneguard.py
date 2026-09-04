#!/usr/bin/env python3
"""DoneGuard hook runner.

Uses only the Python standard library. Hook state and reports live under
PLUGIN_DATA so the guarded repository stays clean unless a user explicitly
adds a .doneguard.json configuration file.
"""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import datetime as dt
import fnmatch
import hashlib
import html
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tokenize
from pathlib import Path
from typing import Any, Iterator


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 3,
    "mode": "warn",
    "companion_enabled": True,
    "notification_policy": "always",
    "temporary_report_ttl_hours": 24,
    "require_verification_when_code_changed": True,
    "block_on_failed_verification": True,
    "block_on_debug_markers": False,
    "block_on_sensitive_files": False,
    "debug_marker_ignore_paths": [],
    "debug_markers": {
        "block": [],
        "warn": [
            "TODO/FIXME/HACK",
            "console.log",
            "debugger",
            "Python breakpoint",
            "Ruby binding.pry",
        ],
        "ignore_paths": [],
        "allow_comment": "doneguard: allow-debug",
    },
    "fingerprint_limits": {
        "max_files": 10000,
        "max_total_bytes": 536870912,
        "timeout_ms": 3000,
    },
    "verification_commands": [],
    "ignore_paths": [
        ".git/",
        ".doneguard/",
        "dist/",
        "build/",
        "coverage/",
        "vendor/",
        "node_modules/",
    ],
}

CODE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".dart", ".ex", ".exs", ".go",
    ".html", ".java", ".js", ".jsx", ".kt", ".kts", ".lua", ".m", ".mm",
    ".php", ".py", ".rb", ".rs", ".scala", ".scss", ".sh", ".sql", ".swift",
    ".ts", ".tsx", ".vue", ".zig",
}

CODE_FILENAMES = {
    "Dockerfile", "Makefile", "Package.swift", "Podfile", "build.gradle",
    "build.gradle.kts", "go.mod", "pom.xml", "pyproject.toml", "package.json",
    "requirements.txt", "Cargo.toml",
}

VERIFY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("test", re.compile(r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test(?:\s|$)")),
    ("test", re.compile(r"(?:^|[;&|]\s*)(?:pytest|python(?:3)?\s+-m\s+pytest|vitest|jest|rspec|phpunit)(?:\s|$)")),
    ("test", re.compile(r"(?:^|[;&|]\s*)python(?:3)?\s+-m\s+unittest(?:\s|$)")),
    ("test", re.compile(r"(?:^|[;&|]\s*)(?:uv|poetry|pipenv)\s+run\s+(?:pytest|python(?:3)?\s+-m\s+(?:pytest|unittest))(?:\s|$)")),
    ("test", re.compile(r"(?:^|[;&|]\s*)(?:cargo|go|dotnet|swift|flutter)\s+test(?:\s|$)")),
    ("test", re.compile(r"(?:^|[;&|]\s*)(?:mvn|gradle|gradlew|\.\/gradlew).*\btest\b")),
    ("test", re.compile(r"(?:^|[;&|]\s*)(?:make|just)\s+(?:test|check)(?:\s|$)")),
    ("test", re.compile(r"\bxcodebuild\b.*\btest\b")),
    ("lint", re.compile(r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?lint(?:\s|$)")),
    ("lint", re.compile(r"(?:^|[;&|]\s*)(?:ruff\s+check|eslint|biome\s+check|golangci-lint\s+run|cargo\s+clippy|go\s+vet)(?:\s|$)")),
    ("typecheck", re.compile(r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:typecheck|type-check)(?:\s|$)")),
    ("typecheck", re.compile(r"(?:^|[;&|]\s*)(?:tsc|mypy|pyright)(?:\s|$)")),
    ("typecheck", re.compile(r"(?:^|[;&|]\s*)cargo\s+check(?:\s|$)")),
    ("build", re.compile(r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build(?:\s|$)")),
    ("build", re.compile(r"(?:^|[;&|]\s*)(?:cargo|dotnet)\s+build(?:\s|$)")),
    ("build", re.compile(r"(?:^|[;&|]\s*)mvn(?:\s+[^;&|]+)*\s+(?:package|verify)(?:\s|$)")),
]

BOOLEAN_CONFIG_FIELDS = {
    "companion_enabled",
    "require_verification_when_code_changed",
    "block_on_failed_verification",
    "block_on_debug_markers",
    "block_on_sensitive_files",
}

VERIFICATION_KINDS = {"test", "lint", "typecheck", "build"}
ALLOW_DEBUG_MARKER = re.compile(r"doneguard:\s*allow-debug", re.IGNORECASE)

DEBUG_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("TODO/FIXME/HACK", re.compile(r"\b(?:TODO|FIXME|HACK)\b")),
    ("console.log", re.compile(r"\bconsole\.log\s*\(")),
    ("debugger", re.compile(r"\bdebugger\s*;?")),
    ("Python breakpoint", re.compile(r"\b(?:breakpoint|pdb\.set_trace)\s*\(")),
    ("Ruby binding.pry", re.compile(r"\bbinding\.pry\b")),
]

SENSITIVE_PATH_PATTERNS = [
    re.compile(r"(^|/)\.env(?:\.|$)"),
    re.compile(r"\.(?:pem|key|p12|pfx)$", re.IGNORECASE),
    re.compile(r"(^|/)(?:credentials|secrets?)(?:\.|/|$)", re.IGNORECASE),
]

MANAGED_CODEX_SUBTREES = ("skills", "plugins", "bin")
MANAGED_CODEX_FILES = ("config.toml", "AGENTS.md")
MANAGED_AGENTS_SUBTREES = ("skills", "plugins")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def plugin_data_dir() -> Path:
    raw = os.environ.get("PLUGIN_DATA") or os.environ.get("DONEGUARD_DATA")
    if raw:
        path = Path(raw)
    else:
        installed = Path.home() / ".codex" / "plugins" / "data" / "doneguard-personal"
        path = installed if installed.exists() else Path.home() / ".codex" / "doneguard-data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:160] or "unknown"


def state_path(session_id: str) -> Path:
    path = plugin_data_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{safe_id(session_id)}.json"


@contextmanager
def state_lock(session_id: str) -> Iterator[None]:
    """Serialize read-modify-write cycles for hooks from the same session."""
    lock_path = state_path(session_id).with_suffix(".lock")
    descriptor: int | None = None
    for _ in range(50):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, f"{os.getpid()}\n".encode())
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.02)
    if descriptor is None:
        raise TimeoutError(f"Could not acquire DoneGuard session lock: {lock_path}")
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def new_state(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(event.get("session_id") or "unknown"),
        "cwd": str(event.get("cwd") or os.getcwd()),
        "started_at": now_iso(),
        "sequence": 0,
        "last_change_sequence": 0,
        "prompt_count": 0,
        "files_touched": [],
        "turn_files_touched": [],
        "fingerprint_cache": {},
        "verifications": [],
    }


def load_config(cwd: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    config = {
        **DEFAULT_CONFIG,
        "ignore_paths": list(DEFAULT_CONFIG["ignore_paths"]),
        "debug_marker_ignore_paths": [],
        "debug_markers": {
            key: list(value) if isinstance(value, list) else value
            for key, value in DEFAULT_CONFIG["debug_markers"].items()
        },
        "fingerprint_limits": dict(DEFAULT_CONFIG["fingerprint_limits"]),
        "verification_commands": [],
    }
    root = guard_root(cwd)
    config_path = root / ".doneguard.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config, warnings
    except json.JSONDecodeError as exc:
        return config, [f"Invalid .doneguard.json: {exc.msg} at line {exc.lineno}, column {exc.colno}; defaults were used."]
    except OSError as exc:
        return config, [f"Could not read .doneguard.json: {exc}; defaults were used."]
    if not isinstance(raw, dict):
        return config, [".doneguard.json must contain a JSON object; defaults were used."]

    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        warnings.append("Unknown .doneguard.json field(s): " + ", ".join(unknown))
    config.update(raw)

    if config.get("schema_version") not in {1, 2, 3}:
        warnings.append("Unsupported schema_version; version 3 semantics were used.")
        config["schema_version"] = 3
    if config.get("mode") not in {"observe", "warn", "strict"}:
        warnings.append("Unknown DoneGuard mode; using warn.")
        config["mode"] = "warn"
    if config.get("notification_policy") not in {"always", "issues_only", "never"}:
        warnings.append("notification_policy must be always, issues_only, or never; using always.")
        config["notification_policy"] = "always"
    ttl = config.get("temporary_report_ttl_hours")
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
        warnings.append("temporary_report_ttl_hours must be a positive integer; the default was used.")
        config["temporary_report_ttl_hours"] = DEFAULT_CONFIG["temporary_report_ttl_hours"]

    for field in BOOLEAN_CONFIG_FIELDS:
        if not isinstance(config.get(field), bool):
            warnings.append(f"{field} must be a boolean; the default was used.")
            config[field] = DEFAULT_CONFIG[field]

    for field in ("ignore_paths", "debug_marker_ignore_paths"):
        value = config.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            fallback = list(DEFAULT_CONFIG.get(field, []))
            warnings.append(f"{field} must be an array of strings; defaults were used.")
            config[field] = fallback

    debug_config = config.get("debug_markers")
    if not isinstance(debug_config, dict):
        warnings.append("debug_markers must be an object; defaults were used.")
        debug_config = dict(DEFAULT_CONFIG["debug_markers"])
    else:
        debug_config = {**DEFAULT_CONFIG["debug_markers"], **debug_config}
    for field in ("block", "warn", "ignore_paths"):
        value = debug_config.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            warnings.append(f"debug_markers.{field} must be an array of strings; defaults were used.")
            debug_config[field] = list(DEFAULT_CONFIG["debug_markers"][field])
    if not isinstance(debug_config.get("allow_comment"), str):
        warnings.append("debug_markers.allow_comment must be a string; the default was used.")
        debug_config["allow_comment"] = DEFAULT_CONFIG["debug_markers"]["allow_comment"]
    known_labels = {label for label, _ in DEBUG_MARKERS}
    for field in ("block", "warn"):
        unknown_labels = sorted(set(debug_config[field]) - known_labels)
        if unknown_labels:
            warnings.append(f"debug_markers.{field} contains unknown label(s): " + ", ".join(unknown_labels))
            debug_config[field] = [label for label in debug_config[field] if label in known_labels]
    config["debug_markers"] = debug_config

    limits = config.get("fingerprint_limits")
    if not isinstance(limits, dict):
        warnings.append("fingerprint_limits must be an object; defaults were used.")
        limits = dict(DEFAULT_CONFIG["fingerprint_limits"])
    else:
        limits = {**DEFAULT_CONFIG["fingerprint_limits"], **limits}
    for field in ("max_files", "max_total_bytes", "timeout_ms"):
        if not isinstance(limits.get(field), int) or isinstance(limits.get(field), bool) or limits[field] <= 0:
            warnings.append(f"fingerprint_limits.{field} must be a positive integer; the default was used.")
            limits[field] = DEFAULT_CONFIG["fingerprint_limits"][field]
    config["fingerprint_limits"] = limits

    commands = config.get("verification_commands")
    if not isinstance(commands, list):
        warnings.append("verification_commands must be an array; defaults were used.")
        config["verification_commands"] = []
    else:
        valid_commands: list[dict[str, Any]] = []
        for index, item in enumerate(commands):
            if not isinstance(item, dict) or item.get("kind") not in VERIFICATION_KINDS:
                warnings.append(f"verification_commands[{index}] has an invalid kind or shape and was ignored.")
                continue
            text_selectors = [
                key for key in ("command", "command_prefix", "pattern")
                if isinstance(item.get(key), str) and item[key]
            ]
            argv_valid = (
                isinstance(item.get("argv"), list)
                and bool(item["argv"])
                and all(isinstance(value, str) and value for value in item["argv"])
            )
            selectors = text_selectors + (["argv"] if argv_valid else [])
            if len(selectors) != 1:
                warnings.append(f"verification_commands[{index}] must define exactly one command selector and was ignored.")
                continue
            if selectors[0] == "pattern":
                try:
                    re.compile(item["pattern"])
                except re.error as exc:
                    warnings.append(f"verification_commands[{index}] has an invalid pattern ({exc}) and was ignored.")
                    continue
            rule: dict[str, Any] = {
                "id": item.get("id") if isinstance(item.get("id"), str) and item["id"] else f"custom-{index + 1}",
                "kind": item["kind"],
                selectors[0]: item[selectors[0]],
                "required": item.get("required", False),
                "covers": item.get("covers", []),
                "when_changed": item.get("when_changed", item.get("covers", [])),
                "fingerprint_paths": item.get(
                    "fingerprint_paths", item.get("when_changed", item.get("covers", []))
                ),
                "artifacts": item.get("artifacts", []),
                "cwd": item.get("cwd", "."),
                "structured": selectors[0] == "argv",
            }
            if not isinstance(rule["required"], bool):
                warnings.append(f"verification_commands[{index}].required must be a boolean and was set to false.")
                rule["required"] = False
            if not isinstance(rule["covers"], list) or any(not isinstance(path, str) for path in rule["covers"]):
                warnings.append(f"verification_commands[{index}].covers must be an array of strings and was cleared.")
                rule["covers"] = []
            for field in ("when_changed", "fingerprint_paths"):
                if not isinstance(rule[field], list) or any(not isinstance(path, str) for path in rule[field]):
                    warnings.append(f"verification_commands[{index}].{field} must be an array of strings and was cleared.")
                    rule[field] = []
            if not isinstance(rule["cwd"], str) or not rule["cwd"]:
                warnings.append(f"verification_commands[{index}].cwd must be a non-empty string and was set to '.'.")
                rule["cwd"] = "."
            if config.get("schema_version") == 3 and rule["required"] and not rule["structured"]:
                warnings.append(
                    f"verification_commands[{index}] is required but uses a heuristic selector; Schema 3 required rules should use argv."
                )
            artifacts = rule["artifacts"]
            if not isinstance(artifacts, list):
                warnings.append(f"verification_commands[{index}].artifacts must be an array and was cleared.")
                artifacts = []
            valid_artifacts: list[dict[str, Any]] = []
            for artifact_index, artifact in enumerate(artifacts):
                prefix = f"verification_commands[{index}].artifacts[{artifact_index}]"
                if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str) or not artifact["path"]:
                    warnings.append(f"{prefix} must define a path and was ignored.")
                    continue
                artifact_format = artifact.get("format", "coverage-summary")
                if artifact_format not in {"coverage-summary", "istanbul-summary"}:
                    warnings.append(f"{prefix}.format is unsupported and was ignored.")
                    continue
                thresholds = artifact.get("thresholds", {})
                if not isinstance(thresholds, dict) or any(
                    key not in {"lines", "branches", "functions", "statements"}
                    or not isinstance(value, (int, float)) or isinstance(value, bool)
                    or value < 0 or value > 100
                    for key, value in thresholds.items()
                ):
                    warnings.append(f"{prefix}.thresholds is invalid and was cleared.")
                    thresholds = {}
                max_age = artifact.get("max_age_seconds", 300)
                if not isinstance(max_age, (int, float)) or isinstance(max_age, bool) or max_age <= 0:
                    warnings.append(f"{prefix}.max_age_seconds is invalid and was set to 300.")
                    max_age = 300
                valid_artifacts.append({
                    "path": artifact["path"],
                    "format": artifact_format,
                    "thresholds": thresholds,
                    "max_age_seconds": max_age,
                })
            rule["artifacts"] = valid_artifacts
            valid_commands.append(rule)
        config["verification_commands"] = valid_commands
    return config, warnings


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=8,
        check=False,
    )


def repo_root(cwd: Path) -> Path | None:
    try:
        result = run_git(cwd, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def explicit_config_root(cwd: Path) -> Path | None:
    """Find the nearest opt-in root for a directory that is not in Git."""
    current = cwd if cwd.is_dir() else cwd.parent
    for candidate in (current, *current.parents):
        if (candidate / ".doneguard.json").is_file():
            return candidate
    return None


def guard_root(cwd: Path) -> Path:
    return repo_root(cwd) or explicit_config_root(cwd) or cwd


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser().resolve(strict=False) if raw else (Path.home() / ".codex").resolve(strict=False)


def managed_global_root(path: Path) -> Path | None:
    """Return the owner root for known global Codex engineering assets."""
    target = path.resolve(strict=False)
    codex = codex_home()
    if target in {codex / name for name in MANAGED_CODEX_FILES}:
        return codex
    if any(path_is_within(target, codex / name) for name in MANAGED_CODEX_SUBTREES):
        return codex
    agents = (Path.home() / ".agents").resolve(strict=False)
    if any(path_is_within(target, agents / name) for name in MANAGED_AGENTS_SUBTREES):
        return agents
    return None


def changed_paths(cwd: Path) -> list[str]:
    root = repo_root(cwd)
    if root is None:
        return []
    paths: set[str] = set()
    commands = [
        ("diff", "--name-only", "--relative", "HEAD"),
        ("diff", "--cached", "--name-only", "--relative", "HEAD"),
        ("ls-files", "--others", "--exclude-standard"),
    ]
    for args in commands:
        result = run_git(root, *args)
        if result.returncode == 0:
            paths.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    if paths:
        return sorted(paths)
    # Repositories without an initial commit do not have HEAD.
    status = run_git(root, "status", "--porcelain=v1")
    if status.returncode == 0:
        for line in status.stdout.splitlines():
            if len(line) > 3:
                paths.add(line[3:].split(" -> ")[-1].strip())
    return sorted(paths)


def ignored(path: str, prefixes: list[Any]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(normalized.startswith(str(prefix).replace("\\", "/").lstrip("./")) for prefix in prefixes)


def code_changed(paths: list[str]) -> bool:
    return any(Path(path).suffix.lower() in CODE_EXTENSIONS or Path(path).name in CODE_FILENAMES for path in paths)


def fingerprint_patterns(config: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    for rule in config.get("verification_commands", []):
        patterns.extend(rule.get("when_changed", []))
        patterns.extend(rule.get("fingerprint_paths", []))
    return patterns


def fingerprint_relevant(path: str, config: dict[str, Any]) -> bool:
    return (
        bool(config.get("_fingerprint_all_paths"))
        or code_changed([path])
        or path_matches(path, fingerprint_patterns(config))
    )


def project_path(root: Path, path: str) -> Path | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def fingerprint_target(root: Path, path: str) -> Path | None:
    """Resolve report paths, allowing already-vetted absolute external assets."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    return project_path(root, path)


def path_matches(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    for pattern in patterns:
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")
        if fnmatch.fnmatchcase(normalized, normalized_pattern):
            return True
        if normalized_pattern.endswith("/**") and normalized.startswith(normalized_pattern[:-3].rstrip("/") + "/"):
            return True
    return False


def git_blob_hashes(root: Path, paths: list[str], timeout_seconds: float) -> dict[str, str]:
    hashes: dict[str, str] = {}
    deadline = time.monotonic() + max(timeout_seconds, 0.05)
    for offset in range(0, len(paths), 200):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        batch = paths[offset:offset + 200]
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "hash-object", "--", *batch],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=remaining,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            break
        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or len(values) != len(batch):
            break
        hashes.update(zip(batch, values))
    return hashes


def fingerprint_cache_path(cwd: Path) -> Path:
    root = guard_root(cwd)
    identifier = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    return plugin_data_dir() / "fingerprint-cache" / f"{identifier}.json"


def load_persistent_fingerprint_cache(cwd: Path) -> dict[str, Any]:
    payload = load_json(fingerprint_cache_path(cwd), {})
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return {}
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else {}


def save_persistent_fingerprint_cache(cwd: Path, cache: dict[str, Any], max_entries: int) -> None:
    entries = dict(list(cache.items())[-max(max_entries, 1):])
    save_json(fingerprint_cache_path(cwd), {
        "schema_version": 1,
        "updated_at": now_iso(),
        "entries": entries,
    })


def merkle_fingerprint(entries: dict[str, tuple[str, str]], complete: bool, reason: str) -> tuple[str, int]:
    leaves: list[bytes] = []
    for path in sorted(entries):
        kind, file_hash = entries[path]
        payload = "\0".join((path, kind, file_hash)).encode("utf-8", errors="surrogateescape")
        leaves.append(hashlib.sha256(b"leaf\0" + payload).digest())
    chunks = [
        hashlib.sha256(b"chunk\0" + b"".join(leaves[offset:offset + 256])).digest()
        for offset in range(0, len(leaves), 256)
    ]
    root = hashlib.sha256(b"merkle-v1\0" + b"".join(chunks))
    if not complete:
        root.update(b"INCOMPLETE\0")
        root.update(reason.encode())
    return "sha256:" + root.hexdigest(), len(chunks)


def workspace_fingerprint_details(
    cwd: Path,
    paths: list[str],
    config: dict[str, Any],
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hash relevant changed code under an explicit time and I/O budget."""
    started = time.monotonic()
    root = guard_root(cwd)
    relevant = sorted({
        path.replace("\\", "/")
        for path in paths
        if not ignored(path, config["ignore_paths"]) and fingerprint_relevant(path, config)
    })
    limits = config.get("fingerprint_limits", DEFAULT_CONFIG["fingerprint_limits"])
    max_files = int(limits["max_files"])
    max_bytes = int(limits["max_total_bytes"])
    timeout_seconds = int(limits["timeout_ms"]) / 1000
    fingerprint_cache = cache if isinstance(cache, dict) else {}
    complete = True
    limit_reason = ""
    selected = relevant
    if len(selected) > max_files:
        selected = selected[:max_files]
        complete = False
        limit_reason = f"max_files exceeded ({len(relevant)} > {max_files})"

    entries: dict[str, tuple[str, str]] = {}
    pending: list[tuple[str, Path, os.stat_result, str]] = []
    cache_hits = 0
    bytes_hashed = 0
    total_bytes = 0
    for path in selected:
        if time.monotonic() - started > timeout_seconds:
            complete = False
            limit_reason = limit_reason or f"timeout exceeded ({limits['timeout_ms']} ms)"
            break
        target = fingerprint_target(root, path)
        if target is None:
            entries[path] = ("outside-root", "")
            continue
        try:
            stat = target.lstat()
        except OSError:
            entries[path] = ("deleted-or-unreadable", "")
            continue
        mode = f"{stat.st_mode & 0o777:o}"
        if target.is_symlink():
            try:
                entries[path] = (f"symlink:{mode}", hashlib.sha256(os.readlink(target).encode()).hexdigest())
            except OSError:
                entries[path] = (f"symlink-unreadable:{mode}", "")
            continue
        if not target.is_file():
            entries[path] = (f"non-file:{mode}", "")
            continue
        signature = f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_mode}"
        total_bytes += stat.st_size
        cached = fingerprint_cache.get(path)
        if isinstance(cached, dict) and cached.get("signature") == signature and isinstance(cached.get("hash"), str):
            entries[path] = (f"file:{mode}", cached["hash"])
            cache_hits += 1
            fingerprint_cache.pop(path, None)
            fingerprint_cache[path] = cached
            continue
        if bytes_hashed + stat.st_size > max_bytes:
            complete = False
            limit_reason = limit_reason or f"max_total_bytes exceeded ({bytes_hashed + stat.st_size} > {max_bytes})"
            break
        pending.append((path, target, stat, signature))
        bytes_hashed += stat.st_size

    remaining = max(0.05, timeout_seconds - (time.monotonic() - started))
    relative_pending = [path for path, _, _, _ in pending if not Path(path).is_absolute()]
    git_hashes = git_blob_hashes(root, relative_pending, remaining) if repo_root(cwd) is not None else {}
    for path, target, stat, signature in pending:
        if time.monotonic() - started > timeout_seconds:
            complete = False
            limit_reason = limit_reason or f"timeout exceeded ({limits['timeout_ms']} ms)"
            break
        file_hash = git_hashes.get(path)
        if file_hash is None:
            try:
                file_digest = hashlib.sha256()
                with target.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        file_digest.update(chunk)
                file_hash = file_digest.hexdigest()
            except OSError:
                complete = False
                limit_reason = limit_reason or f"could not hash {path}"
                break
        mode = f"{stat.st_mode & 0o777:o}"
        entries[path] = (f"file:{mode}", file_hash)
        fingerprint_cache[path] = {"signature": signature, "hash": file_hash}

    fingerprint, chunk_count = merkle_fingerprint(entries, complete, limit_reason)
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    return {
        "fingerprint": fingerprint,
        "metrics": {
            "algorithm": "merkle-v1",
            "chunk_count": chunk_count,
            "file_count": len(relevant),
            "processed_files": len(entries),
            "total_bytes": total_bytes,
            "bytes_hashed": bytes_hashed,
            "cache_hits": cache_hits,
            "duration_ms": duration_ms,
            "complete": complete,
            "limit_reason": limit_reason or None,
        },
    }


def cached_workspace_fingerprint(
    cwd: Path,
    paths: list[str],
    config: dict[str, Any],
) -> dict[str, Any]:
    cache = load_persistent_fingerprint_cache(cwd)
    result = workspace_fingerprint_details(cwd, paths, config, cache)
    max_entries = int(config.get("fingerprint_limits", DEFAULT_CONFIG["fingerprint_limits"])["max_files"]) * 2
    save_persistent_fingerprint_cache(cwd, cache, max_entries)
    return result


def workspace_fingerprint(cwd: Path, paths: list[str], config: dict[str, Any]) -> str:
    return str(workspace_fingerprint_details(cwd, paths, config)["fingerprint"])


def scope_for_path(path: Path) -> tuple[str, Path] | None:
    """Classify an edited path without assuming it belongs to the chat cwd."""
    target = path.resolve(strict=False)
    owner = target if target.is_dir() else target.parent
    repository = repo_root(owner)
    if repository is not None and path_is_within(target, repository):
        return "git", repository.resolve(strict=False)
    configured = explicit_config_root(owner)
    if configured is not None and path_is_within(target, configured):
        return "configured", configured.resolve(strict=False)
    managed = managed_global_root(target)
    if managed is not None:
        return "global", managed
    return None


def scope_path(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path.resolve(strict=False))


def runtime_config(cwd: Path, kind: str) -> tuple[dict[str, Any], list[str]]:
    config, warnings = load_config(cwd)
    if kind == "global":
        config = {**config, "_fingerprint_all_paths": True}
    return config, warnings


def git_workspace_snapshot(cwd: Path) -> dict[str, Any] | None:
    root = repo_root(cwd)
    if root is None:
        return None
    config, _ = load_config(root)
    paths = [path for path in changed_paths(root) if not ignored(path, config["ignore_paths"])]
    fingerprint = workspace_fingerprint_details(root, paths, config)
    return {
        "root": str(root.resolve(strict=False)),
        "fingerprint": fingerprint["fingerprint"],
    }


def recent_verifications(state: dict[str, Any]) -> list[dict[str, Any]]:
    values = [item for item in state.get("verifications", []) if isinstance(item, dict)]
    boundary = state.get("turn_started_sequence")
    if not isinstance(boundary, int):
        return values
    return [item for item in values if int(item.get("sequence") or 0) >= boundary]


def determine_turn_scope(cwd: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Resolve whether this turn changed a protected engineering scope."""
    has_turn_boundary = isinstance(state.get("turn_started_sequence"), int)
    raw_touched = state.get("turn_files_touched", []) if has_turn_boundary else state.get("files_touched", [])
    grouped: dict[tuple[str, str], list[Path]] = {}
    for raw in raw_touched:
        if not isinstance(raw, str) or not raw:
            continue
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = cwd / target
        owner = scope_for_path(target)
        if owner is None:
            continue
        kind, root = owner
        grouped.setdefault((kind, str(root)), []).append(target.resolve(strict=False))

    if grouped:
        (kind, root_text), primary_paths = max(
            grouped.items(), key=lambda item: (len(item[1]), item[0][1])
        )
        root = Path(root_text)
        protected = sorted({
            scope_path(root, target)
            for values in grouped.values()
            for target in values
        })
        return {"active": True, "kind": kind, "root": root, "paths": protected}

    latest = recent_verifications(state)
    if latest:
        verification_root = latest[-1].get("scope_root")
        root = Path(str(verification_root)).resolve(strict=False) if verification_root else guard_root(cwd)
        kind = str(latest[-1].get("scope_kind") or "")
        if kind not in {"git", "configured", "global"}:
            kind = "git" if repo_root(root) is not None else ("global" if managed_global_root(root) else "configured")
        config, _ = runtime_config(root, kind)
        paths = [path for path in changed_paths(root) if not ignored(path, config["ignore_paths"])]
        return {"active": True, "kind": kind, "root": root, "paths": paths}

    baseline = state.get("turn_git_baseline")
    current = git_workspace_snapshot(cwd)
    if isinstance(baseline, dict) and current is not None:
        if baseline.get("root") == current.get("root") and baseline.get("fingerprint") != current.get("fingerprint"):
            root = Path(str(current["root"]))
            config, _ = load_config(root)
            paths = [path for path in changed_paths(root) if not ignored(path, config["ignore_paths"])]
            return {"active": True, "kind": "git", "root": root, "paths": paths}

    # Older sessions and direct unit tests may not contain a prompt boundary.
    if not has_turn_boundary:
        root = repo_root(cwd)
        if root is not None:
            config, _ = load_config(root)
            paths = [path for path in changed_paths(root) if not ignored(path, config["ignore_paths"])]
            if paths:
                return {"active": True, "kind": "git", "root": root, "paths": paths}
        configured = explicit_config_root(cwd)
        if configured is not None:
            return {"active": True, "kind": "configured", "root": configured, "paths": []}

    return {"active": False, "kind": "none", "root": guard_root(cwd), "paths": []}


def parse_simple_command(command: str) -> dict[str, Any]:
    if "\n" in command:
        return {"complete": False, "reason": "multiline command"}
    if "$(" in command or "`" in command:
        return {"complete": False, "reason": "command substitution"}
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        return {"complete": False, "reason": f"shell parse error: {exc}"}
    operators = {";", "&", "&&", "|", "||", "<", ">", ">>", "<<", "<&", ">&"}
    if any(token in operators or set(token) <= set(";&|<>") for token in tokens if token):
        return {"complete": False, "reason": "compound command or redirection"}
    index = 0
    if tokens and tokens[0] == "env":
        index = 1
    assignment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
    while index < len(tokens) and assignment.match(tokens[index]):
        index += 1
    argv = tokens[index:]
    if not argv:
        return {"complete": False, "reason": "no executable"}
    return {"complete": True, "argv": argv}


def command_matches_rule(
    command: str,
    item: dict[str, Any],
    cwd: Path | None = None,
) -> bool:
    normalized = " ".join(command.split())
    if "argv" in item:
        parsed = parse_simple_command(command)
        if not parsed.get("complete") or parsed.get("argv") != item["argv"]:
            return False
        expected_cwd = str(item.get("cwd") or ".")
        if cwd is not None:
            root = guard_root(cwd)
            expected = Path(expected_cwd)
            if not expected.is_absolute():
                expected = root / expected
            if cwd.resolve() != expected.resolve():
                return False
        return True
    if "command" in item:
        return normalized == " ".join(str(item["command"]).split())
    if "command_prefix" in item:
        prefix = " ".join(str(item["command_prefix"]).split())
        return normalized == prefix or normalized.startswith(prefix + " ")
    if "pattern" in item:
        return re.search(str(item["pattern"]), normalized) is not None
    return False


def rule_fingerprint(rule: dict[str, Any]) -> str:
    payload = json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def match_verification(
    command: str,
    config: dict[str, Any] | None = None,
    cwd: Path | None = None,
) -> dict[str, Any] | None:
    parsed = parse_simple_command(command)
    if not parsed.get("complete"):
        return None
    normalized = " ".join(command.split())
    structured_candidate = False
    for item in (config or {}).get("verification_commands", []):
        if "argv" in item and parsed.get("argv") == item["argv"]:
            structured_candidate = True
        if command_matches_rule(command, item, cwd):
            return item
    if structured_candidate:
        return None
    normalized = " ".join(str(value) for value in parsed["argv"])
    for kind, pattern in VERIFY_PATTERNS:
        if pattern.search(normalized):
            return {
                "id": f"builtin-{kind}",
                "kind": kind,
                "required": False,
                "covers": [],
                "artifacts": [],
            }
    return None


def classify_verification(
    command: str,
    config: dict[str, Any] | None = None,
    cwd: Path | None = None,
) -> str | None:
    rule = match_verification(command, config, cwd)
    return str(rule["kind"]) if rule else None


def redact_command(command: str) -> str:
    """Remove common inline secrets before persisting a command."""
    normalized = " ".join(command.split())
    normalized = re.sub(
        r"(?i)\b([A-Z_][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|AUTH)[A-Z0-9_]*)=(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+)",
        lambda match: f"{match.group(1)}=<redacted>",
        normalized,
    )
    normalized = re.sub(
        r"(?i)(--(?:token|secret|password|passwd|api-key|authorization))(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+)",
        lambda match: f"{match.group(1)}=<redacted>",
        normalized,
    )
    return normalized[:500]


def verification_key(item: dict[str, Any]) -> tuple[str, str]:
    identifier = str(item.get("verification_id") or "")
    if identifier:
        return identifier, ""
    return str(item.get("kind") or "unknown"), " ".join(str(item.get("command") or "").split())


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def coverage_metrics(data: Any, artifact_format: str) -> dict[str, float] | None:
    if not isinstance(data, dict):
        return None
    if artifact_format == "coverage-summary":
        source = data.get("coverage", data)
        if not isinstance(source, dict):
            return None
        metrics = {
            key: float(value)
            for key, value in source.items()
            if key in {"lines", "branches", "functions", "statements"}
            and isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        return metrics or None
    total = data.get("total")
    if not isinstance(total, dict):
        return None
    metrics: dict[str, float] = {}
    for key in ("lines", "branches", "functions", "statements"):
        value = total.get(key)
        if isinstance(value, dict) and isinstance(value.get("pct"), (int, float)):
            metrics[key] = float(value["pct"])
    return metrics or None


def capture_artifact_evidence(cwd: Path, rule: dict[str, Any]) -> list[dict[str, Any]]:
    root = guard_root(cwd)
    now = time.time()
    evidence: list[dict[str, Any]] = []
    for spec in rule.get("artifacts", []):
        path = str(spec["path"])
        target = project_path(root, path)
        item: dict[str, Any] = {"path": path, "format": spec["format"], "valid": False, "errors": []}
        if target is None or not target.is_file():
            item["errors"].append("artifact is missing")
            evidence.append(item)
            continue
        try:
            stat = target.stat()
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            item["errors"].append(f"artifact could not be read: {type(exc).__name__}")
            evidence.append(item)
            continue
        metrics = coverage_metrics(data, spec["format"])
        if metrics is None:
            item["errors"].append("coverage metrics were not found")
        age_seconds = max(0.0, now - stat.st_mtime)
        if age_seconds > float(spec["max_age_seconds"]):
            item["errors"].append(
                f"artifact is stale ({age_seconds:.1f}s > {spec['max_age_seconds']}s)"
            )
        for key, threshold in spec["thresholds"].items():
            actual = None if metrics is None else metrics.get(key)
            if actual is None:
                item["errors"].append(f"{key} coverage is missing")
            elif actual < float(threshold):
                item["errors"].append(f"{key} coverage {actual:.2f}% is below {float(threshold):.2f}%")
        item.update({
            "hash": file_sha256(target),
            "mtime_ns": stat.st_mtime_ns,
            "age_seconds": round(age_seconds, 3),
            "metrics": metrics or {},
        })
        item["valid"] = not item["errors"] and item["hash"] is not None
        evidence.append(item)
    return evidence


def artifact_evidence_errors(cwd: Path, item: dict[str, Any], rule: dict[str, Any]) -> list[str]:
    root = guard_root(cwd)
    recorded = {entry.get("path"): entry for entry in item.get("artifact_evidence", [])}
    errors: list[str] = []
    for spec in rule.get("artifacts", []):
        path = str(spec["path"])
        evidence = recorded.get(path)
        if not isinstance(evidence, dict):
            errors.append(f"{path}: no artifact evidence was recorded")
            continue
        errors.extend(f"{path}: {message}" for message in evidence.get("errors", []))
        target = project_path(root, path)
        current_hash = file_sha256(target) if target is not None and target.is_file() else None
        if evidence.get("hash") != current_hash:
            errors.append(f"{path}: artifact changed after verification")
        metrics = evidence.get("metrics", {})
        for key, threshold in spec["thresholds"].items():
            actual = metrics.get(key) if isinstance(metrics, dict) else None
            if isinstance(actual, (int, float)) and actual < float(threshold):
                message = f"{path}: {key} coverage {actual:.2f}% is below {float(threshold):.2f}%"
                if message not in errors:
                    errors.append(message)
    return errors


def extract_exit_code(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("exit_code", "exitCode", "status_code", "statusCode"):
            if key in value and isinstance(value[key], int):
                return value[key]
        for nested in value.values():
            found = extract_exit_code(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = extract_exit_code(nested)
            if found is not None:
                return found
    elif isinstance(value, str):
        patterns = [
            r"(?:exit(?:ed)?(?: with)?(?: code| status)?|exit_code)\D{0,8}(-?\d+)",
            r"Process completed with code\s+(-?\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                return int(match.group(1))
    return None


def command_text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list) and value:
        return command_text(value[-1])
    return ""


def command_executions(value: Any):
    if isinstance(value, dict):
        if value.get("type") == "CommandExecution":
            yield value
        for nested in value.values():
            yield from command_executions(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from command_executions(nested)


def transcript_exit_code(event: dict[str, Any], command: str) -> int | None:
    """Recover a shell exit code when PostToolUse omits it from tool_response.

    Codex normally exposes the command result in the session transcript as a
    CommandExecution item. The transcript is only a compatibility fallback;
    tool_response remains the preferred, stable source.
    """
    raw_path = event.get("transcript_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None

    expected_id = str(event.get("tool_use_id") or "")
    expected_command = command_text(command)
    try:
        with Path(raw_path).open(encoding="utf-8") as handle:
            recent_lines = deque(handle, maxlen=4000)
    except OSError:
        return None

    for line in reversed(recent_lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in command_executions(record):
            exit_code = extract_exit_code(item)
            if exit_code is None:
                continue
            if expected_id and str(item.get("id") or "") == expected_id:
                return exit_code
            if expected_command and command_text(item.get("command")) == expected_command:
                return exit_code
    return None


def command_from_event(event: dict[str, Any]) -> str:
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            if isinstance(tool_input.get(key), str):
                return tool_input[key]
    return ""


def patch_paths(command: str) -> list[str]:
    patterns = [
        r"^\*\*\* (?:Add|Update|Delete) File:\s+(.+?)\s*$",
        r"^\+\+\+\s+b/(.+?)\s*$",
    ]
    paths: set[str] = set()
    for line in command.splitlines():
        for pattern in patterns:
            match = re.match(pattern, line)
            if match:
                paths.add(match.group(1))
    return sorted(paths)


def event_working_directory(event: dict[str, Any], fallback: str | Path) -> Path:
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("workdir", "cwd"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return Path(value).expanduser().resolve(strict=False)
    return Path(str(event.get("cwd") or fallback)).expanduser().resolve(strict=False)


def absolute_patch_paths(event: dict[str, Any], fallback: str | Path) -> list[str]:
    base = event_working_directory(event, fallback)
    values: list[str] = []
    for raw in patch_paths(command_from_event(event)):
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        values.append(str(candidate.resolve(strict=False)))
    return sorted(set(values))


def mask_string_literals(lines: list[str], path: str) -> list[str]:
    """Mask quoted text while preserving line numbers and comment text."""
    masked: list[str] = []
    quote = ""
    triple = ""
    escaped = False
    supports_triples = Path(path).suffix.lower() == ".py"
    for line in lines:
        output: list[str] = []
        index = 0
        while index < len(line):
            if triple:
                if line.startswith(triple, index):
                    output.extend(" " * len(triple))
                    index += len(triple)
                    triple = ""
                else:
                    output.append(" ")
                    index += 1
                continue
            character = line[index]
            if quote:
                output.append(" ")
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
                index += 1
                continue
            candidate = line[index:index + 3]
            if supports_triples and candidate in {"'''", '\"\"\"'}:
                triple = candidate
                output.extend("   ")
                index += 3
                continue
            if character in {"'", '"', "`"}:
                quote = character
                output.append(" ")
                index += 1
                continue
            output.append(character)
            index += 1
        masked.append("".join(output))
        if quote and quote != "`":
            quote = ""
            escaped = False
    return masked


def mask_python_strings(text: str, path: str) -> tuple[list[str], dict[str, Any]]:
    lines = text.splitlines()
    masked = [list(line) for line in lines]
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type != tokenize.STRING:
                continue
            start_line, start_column = token.start
            end_line, end_column = token.end
            for line_number in range(start_line, end_line + 1):
                if line_number < 1 or line_number > len(masked):
                    continue
                first = start_column if line_number == start_line else 0
                last = end_column if line_number == end_line else len(masked[line_number - 1])
                for column in range(first, min(last, len(masked[line_number - 1]))):
                    masked[line_number - 1][column] = " "
        return ["".join(line) for line in masked], {
            "path": path, "engine": "python-tokenize", "complete": True, "error": None,
        }
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        return mask_string_literals(lines, path), {
            "path": path,
            "engine": "python-tokenize-fallback",
            "complete": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def mask_javascript_strings(text: str, path: str) -> tuple[list[str], dict[str, Any]]:
    output = list(text)
    mode = "normal"
    escaped = False
    regex_character_class = False
    template_depths: list[int] = []
    previous_significant = ""
    index = 0

    def hide(position: int) -> None:
        if output[position] not in {"\n", "\r"}:
            output[position] = " "

    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if mode == "line-comment":
            if character == "\n":
                mode = "normal"
            index += 1
            continue
        if mode == "block-comment":
            if character == "*" and following == "/":
                index += 2
                mode = "normal"
            else:
                index += 1
            continue
        if mode in {"single", "double"}:
            hide(index)
            if character in {"\n", "\r"} and not escaped:
                mode = "normal"
            elif escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif (mode == "single" and character == "'") or (mode == "double" and character == '"'):
                mode = "normal"
            index += 1
            continue
        if mode == "regex":
            hide(index)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "[":
                regex_character_class = True
            elif character == "]":
                regex_character_class = False
            elif character == "/" and not regex_character_class:
                mode = "normal"
                previous_significant = "/"
            elif character in {"\n", "\r"}:
                mode = "normal"
            index += 1
            continue
        if mode == "template":
            hide(index)
            if escaped:
                escaped = False
                index += 1
                continue
            if character == "\\":
                escaped = True
                index += 1
                continue
            if character == "`":
                mode = "normal"
                previous_significant = "`"
                index += 1
                continue
            if character == "$" and following == "{":
                hide(index + 1)
                template_depths.append(1)
                mode = "normal"
                previous_significant = "{"
                index += 2
                continue
            index += 1
            continue

        if character == "/" and following == "/":
            mode = "line-comment"
            index += 2
            continue
        if character == "/" and following == "*":
            mode = "block-comment"
            index += 2
            continue
        if character == "'":
            hide(index)
            mode = "single"
            escaped = False
            index += 1
            continue
        if character == '"':
            hide(index)
            mode = "double"
            escaped = False
            index += 1
            continue
        if character == "`":
            hide(index)
            mode = "template"
            escaped = False
            index += 1
            continue
        if character == "/" and following not in {"/", "*"} and (
            not previous_significant or previous_significant in "([{:;,=!?&|+-*%^~<>"
        ):
            hide(index)
            mode = "regex"
            escaped = False
            regex_character_class = False
            index += 1
            continue
        if template_depths and character == "{":
            template_depths[-1] += 1
        elif template_depths and character == "}":
            template_depths[-1] -= 1
            if template_depths[-1] == 0:
                template_depths.pop()
                hide(index)
                mode = "template"
                previous_significant = "}"
                index += 1
                continue
        if not character.isspace():
            previous_significant = character
        index += 1

    complete = mode in {"normal", "line-comment"} and not template_depths
    return "".join(output).splitlines(), {
        "path": path,
        "engine": "javascript-lexer",
        "complete": complete,
        "error": None if complete else f"unterminated {mode}",
    }


def scan_source_text(text: str, path: str) -> tuple[list[str], dict[str, Any]]:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return mask_python_strings(text, path)
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return mask_javascript_strings(text, path)
    return mask_string_literals(text.splitlines(), path), {
        "path": path, "engine": "generic-lexer", "complete": True, "error": None,
    }


def added_line_numbers(cwd: Path, paths: list[str]) -> dict[str, set[int]]:
    root = repo_root(cwd)
    if root is None or not paths:
        return {}
    result = run_git(root, "diff", "--unified=0", "HEAD", "--", *paths)
    if result.returncode != 0:
        return {}
    added: dict[str, set[int]] = {}
    current_file = ""
    new_line = 0
    in_hunk = False
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            added.setdefault(current_file, set())
            in_hunk = False
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                new_line = int(match.group(1))
                in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.setdefault(current_file, set()).add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif not line.startswith("\\"):
            new_line += 1
    return added


def untracked_paths(cwd: Path) -> set[str]:
    root = repo_root(cwd)
    if root is None:
        return set()
    result = run_git(root, "ls-files", "--others", "--exclude-standard")
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def added_debug_markers(cwd: Path, paths: list[str], config: dict[str, Any]) -> dict[str, Any]:
    root = repo_root(cwd)
    if root is None or not paths:
        return {"warnings": [], "blockers": [], "scan": {"complete": True, "files": []}}
    debug_config = config.get("debug_markers", DEFAULT_CONFIG["debug_markers"])
    ignored_paths = list(config.get("debug_marker_ignore_paths", [])) + list(debug_config.get("ignore_paths", []))
    debug_paths = [
        path for path in paths
        if code_changed([path]) and not ignored(path, ignored_paths)
    ]
    if not debug_paths:
        return {"warnings": [], "blockers": [], "scan": {"complete": True, "files": []}}
    findings: dict[str, Any] = {
        "warnings": [], "blockers": [], "scan": {"complete": True, "files": []},
    }
    untracked = untracked_paths(root)
    tracked_paths = [path for path in debug_paths if path not in untracked]
    line_numbers = added_line_numbers(root, tracked_paths)
    head = run_git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        line_numbers = {}
    for path in sorted(debug_paths):
        target = project_path(root, path)
        if target is None or not target.is_file():
            continue
        try:
            raw = target.read_bytes()
        except OSError as exc:
            findings["scan"]["complete"] = False
            findings["scan"]["files"].append({
                "path": path, "engine": "unreadable", "complete": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        if b"\0" in raw[:8192]:
            continue
        truncated = len(raw) > 1024 * 1024
        text = raw[:1024 * 1024].decode("utf-8", errors="replace")
        lines = text.splitlines()
        masked, scan = scan_source_text(text, path)
        if truncated:
            scan["complete"] = False
            scan["error"] = "file exceeds 1 MiB scan limit"
        findings["scan"]["files"].append(scan)
        if not scan["complete"]:
            findings["scan"]["complete"] = False
        selected_lines = set(range(1, len(lines) + 1)) if head.returncode != 0 or path in untracked else line_numbers.get(path, set())
        for line_number in sorted(selected_lines):
            if line_number < 1 or line_number > len(lines):
                continue
            original = lines[line_number - 1]
            allow_comment = str(debug_config.get("allow_comment") or "")
            if allow_comment and allow_comment.lower() in original.lower():
                continue
            searchable = masked[line_number - 1]
            for label, pattern in DEBUG_MARKERS:
                if len(findings["warnings"]) + len(findings["blockers"]) >= 5:
                    break
                if not pattern.search(searchable):
                    continue
                message = f"{path}:{line_number}: {label}"
                if config.get("block_on_debug_markers") or label in debug_config.get("block", []):
                    findings["blockers"].append(message)
                elif label in debug_config.get("warn", []):
                    findings["warnings"].append(message)
    return findings


def sensitive_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if any(pattern.search(path.replace("\\", "/")) for pattern in SENSITIVE_PATH_PATTERNS)]


def covered_paths_for_rule(rule: dict[str, Any], paths: list[str]) -> list[str]:
    patterns = rule.get("covers", [])
    if not patterns:
        return list(paths)
    return [path for path in paths if path_matches(path, patterns)]


def triggered_paths_for_rule(rule: dict[str, Any], paths: list[str]) -> list[str]:
    patterns = rule.get("when_changed", [])
    if patterns:
        return [path for path in paths if path_matches(path, patterns)]
    return [path for path in paths if code_changed([path])]


MODE_LABELS = {
    "warn": "提醒模式（只提示，不阻止任务结束）",
    "strict": "严格模式（证据不足时会让 Codex 再检查一次）",
    "observe": "观察模式（只记录，不弹出提醒）",
}

VERIFICATION_KIND_LABELS = {
    "test": "测试",
    "lint": "代码规范检查",
    "typecheck": "类型检查",
    "build": "构建",
}


def plain_finding(message: str, category: str) -> dict[str, str]:
    """Turn stable machine evidence into a beginner-friendly Chinese explanation."""
    finding = {
        "title": "检查记录",
        "detail": "DoneGuard 记录了一项需要关注的检查结果。",
        "next_step": "如果你不确定这项内容的含义，可以把下方技术详情交给开发者查看。",
        "technical_detail": message,
    }

    if message == "code changed, but no successful test, lint, typecheck, or build was recorded after the latest observed edit":
        return {
            "title": "代码改动后还没有验证",
            "detail": "检测到代码有改动，但最后一次修改之后，没有找到成功的测试、代码规范检查、类型检查或构建记录。因此 DoneGuard 暂时无法确认这次修改已经验证。",
            "next_step": "请运行适合该项目的测试或构建命令，然后确认命令成功结束。",
            "technical_detail": message,
        }
    if message == "protected engineering assets changed, but no successful test, lint, typecheck, or build was recorded after the latest observed edit":
        return {
            "title": "全局工程配置改动后还没有验证",
            "detail": "检测到全局 Skill、插件或 Codex CLI 配置发生了修改，但修改后没有找到成功的验证记录。",
            "next_step": "请运行适合该资产的校验或烟雾测试，并确认命令成功结束。",
            "technical_detail": message,
        }
    if message in {
        "recorded verification evidence is stale because relevant code content changed",
        "recorded verification evidence is stale because relevant engineering content changed",
    }:
        return {
            "title": "之前的检查结果已经过期",
            "detail": "虽然之前运行过检查，但代码后来又发生了变化。旧结果不能说明当前代码仍然正常。",
            "next_step": "请在最后一次代码修改之后重新运行测试或构建。",
            "technical_detail": message,
        }
    if message.startswith("the latest recorded ") and " command failed: " in message:
        match = re.match(r"the latest recorded (\w+) command failed: (.+)", message)
        kind = VERIFICATION_KIND_LABELS.get(match.group(1), "验证") if match else "验证"
        command = match.group(2) if match else message
        return {
            "title": f"{kind}没有通过",
            "detail": f"最后一次运行的{kind}命令执行失败，所以当前任务还不能算验证完成。",
            "next_step": "请查看命令输出，修复问题后重新运行，直到命令成功结束。",
            "technical_detail": f"命令：{command}",
        }
    if message.startswith("successful verification recorded ("):
        details = message.removeprefix("successful verification recorded (").removesuffix(")")
        return {
            "title": "已找到成功的验证记录",
            "detail": "最后一次代码修改后，至少有一项测试、代码检查或构建成功完成。",
            "next_step": "",
            "technical_detail": "记录：" + details,
        }
    match = re.match(r"(\d+) changed or touched file\(s\) inspected", message)
    if match:
        count = match.group(1)
        return {
            "title": "已检查本次涉及的文件",
            "detail": f"DoneGuard 已检查本次改动涉及的 {count} 个文件。",
            "next_step": "",
            "technical_detail": message,
        }
    if message.startswith("new debug or temporary markers found: "):
        details = message.split(": ", 1)[1]
        return {
            "title": "发现可能遗留的调试内容",
            "detail": "改动中出现了调试语句或临时标记。它们可能只是开发时留下的内容。",
            "next_step": "请确认这些内容是否需要保留；不需要时请删除。",
            "technical_detail": "位置：" + details,
        }
    if message.startswith("blocking debug markers found: "):
        details = message.split(": ", 1)[1]
        return {
            "title": "发现不允许保留的调试内容",
            "detail": "项目规则要求处理这些调试语句或临时标记后才能完成任务。",
            "next_step": "请删除这些内容，或按项目规则明确标记为允许保留。",
            "technical_detail": "位置：" + details,
        }
    if message.startswith("sensitive-looking files changed: "):
        details = message.split(": ", 1)[1]
        return {
            "title": "改动涉及可能包含敏感信息的文件",
            "detail": "本次修改碰到了名称看起来像密钥、凭据或环境配置的文件。DoneGuard 无法判断其中是否真的包含秘密信息。",
            "next_step": "提交或分享前，请确认文件中没有密码、令牌、私钥等敏感内容。",
            "technical_detail": "文件：" + details,
        }
    if message.startswith("some verification commands had an unknown exit status"):
        return {
            "title": "有些检查无法确认是否成功",
            "detail": "DoneGuard 看到了验证命令，但没有取得明确的成功或失败状态，因此没有把它们算作已通过。",
            "next_step": "请重新运行这些命令，并确认能看到明确的成功结果。",
            "technical_detail": message,
        }
    if message.startswith("debug marker scan is incomplete: "):
        return {
            "title": "调试内容没有检查完整",
            "detail": "部分文件未能完整扫描，所以仍可能存在没有被发现的调试语句或临时标记。",
            "next_step": "请查看技术详情中的文件，并手动确认。",
            "technical_detail": message.split(": ", 1)[1],
        }
    if message.startswith("workspace fingerprint is incomplete: "):
        return {
            "title": "工作区状态没有读取完整",
            "detail": "项目较大或读取受到限制，DoneGuard 没能完整确认当前代码状态，因此验证结果的可信度会降低。",
            "next_step": "请查看技术详情；必要时调整 fingerprint_limits 后重新检查。",
            "technical_detail": message,
        }
    if message.startswith("changed code is not covered by a required verification rule: "):
        return {
            "title": "部分代码改动没有对应的必做检查",
            "detail": "项目设置了必须运行的验证规则，但这些改动没有被任何规则覆盖。",
            "next_step": "请补充或调整 .doneguard.json 中的验证覆盖范围。",
            "technical_detail": message.split(": ", 1)[1],
        }
    if " coverage (" in message:
        identifier, details = message.split(" coverage (", 1)
        return {
            "title": f"{identifier} 的覆盖率证据有效",
            "detail": "DoneGuard 已读取并确认这项覆盖率结果符合项目要求。",
            "next_step": "",
            "technical_detail": details.removesuffix(")"),
        }
    if message.startswith("required verification coverage mapped for "):
        return {
            "title": "必做检查已覆盖本次改动",
            "detail": "项目要求的验证规则已经覆盖到相关改动文件。",
            "next_step": "",
            "technical_detail": message,
        }
    if message.startswith("required verification "):
        identifier = message.split()[2]
        return {
            "title": f"项目要求的检查 {identifier} 尚未通过",
            "detail": "项目配置规定这项检查必须成功，但 DoneGuard 没有找到符合要求的成功记录。",
            "next_step": f"请运行项目中标识为 {identifier} 的检查并处理失败项。",
            "technical_detail": message,
        }

    if category == "passed":
        finding["title"] = "检查已通过"
        finding["detail"] = "DoneGuard 找到了支持任务完成的检查证据。"
        finding["next_step"] = ""
    elif category == "warning":
        finding["title"] = "有一项内容需要留意"
        finding["detail"] = "这项内容不会阻止任务结束，但建议在交付前确认。"
    else:
        finding["title"] = "有一项问题需要处理"
        finding["detail"] = "按照当前项目规则，这项问题会影响完成判断。"
    return finding


def plain_language_report(report: dict[str, Any]) -> dict[str, Any]:
    blockers = list(report.get("blockers", []))
    warnings = list(report.get("warnings", []))
    passed = list(report.get("passed", []))
    paths = list(report.get("changed_paths", []))

    if blockers:
        headline = "暂时还不能确认任务已完成"
        summary = f"发现 {len(blockers)} 个需要处理的问题。按建议处理并重新验证后，再结束任务会更稳妥。"
    elif warnings:
        headline = "任务已有完成证据，但还有提醒"
        summary = f"主要验证没有发现阻断问题，同时有 {len(warnings)} 项内容建议你确认。"
    else:
        headline = "任务已完成检查"
        summary = "DoneGuard 找到了与当前改动匹配的完成证据，没有发现需要阻止交付的问题。"

    has_missing_verification = any("changed, but no successful" in item for item in blockers)
    has_failed_verification = any(item.startswith("the latest recorded ") for item in blockers)
    debug_blocked = any("debug" in item or "temporary markers" in item for item in blockers)
    debug_warned = any("debug" in item or "temporary markers" in item for item in warnings)
    sensitive_blocked = any(item.startswith("sensitive-looking files changed") for item in blockers)
    sensitive_warned = any(item.startswith("sensitive-looking files changed") for item in warnings)
    has_success = any(item.startswith("successful verification recorded") for item in passed)
    checks = [
        {
            "title": "项目改动",
            "status": "passed" if paths else "neutral",
            "detail": f"已找到并检查 {len(paths)} 个本次涉及的文件。" if paths else "没有发现需要验证的项目改动。",
        },
        {
            "title": "测试与构建记录",
            "status": "issue" if has_missing_verification or has_failed_verification else ("passed" if has_success else "neutral"),
            "detail": "工程资产改动后还缺少成功的验证记录。" if has_missing_verification else ("最近一次验证失败。" if has_failed_verification else ("已找到最后一次修改后的成功记录。" if has_success else "本次没有需要验证的改动。")),
        },
        {
            "title": "调试与临时内容",
            "status": "issue" if debug_blocked else ("warning" if debug_warned else "passed"),
            "detail": "发现了需要处理的调试语句或临时标记。" if debug_blocked else ("发现了需要确认的调试语句或临时标记。" if debug_warned else "没有发现新增的常见调试语句或临时标记。"),
        },
        {
            "title": "敏感文件提示",
            "status": "issue" if sensitive_blocked else ("warning" if sensitive_warned else "passed"),
            "detail": "项目规则要求先确认本次涉及的敏感文件。" if sensitive_blocked else ("改动涉及名称看起来可能含有敏感信息的文件。" if sensitive_warned else "没有发现本次改动涉及常见的敏感文件名。"),
        },
    ]
    return {
        "headline": headline,
        "summary": summary,
        "mode_label": MODE_LABELS.get(str(report.get("mode")), str(report.get("mode") or "未知模式")),
        "checks": checks,
        "blockers": [plain_finding(item, "blocker") for item in blockers],
        "warnings": [plain_finding(item, "warning") for item in warnings],
        "passed": [plain_finding(item, "passed") for item in passed],
        "files_summary": f"本次共检查 {len(paths)} 个相关文件。" if paths else "本次没有发现需要验证的项目改动。",
    }


def evaluate(event: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    event_cwd = Path(str(event.get("cwd") or state.get("cwd") or os.getcwd())).resolve()
    scope = determine_turn_scope(event_cwd, state)
    if not scope["active"]:
        return None
    cwd = Path(scope["root"])
    scope_kind = str(scope["kind"])
    config, config_warnings = runtime_config(cwd, scope_kind)
    all_paths = sorted({
        path for path in scope["paths"]
        if not ignored(path, config["ignore_paths"])
    })
    code_paths = [path for path in all_paths if code_changed([path])]
    protected_assets = all_paths if scope_kind == "global" else code_paths
    relevant_code_changed = bool(protected_assets)
    verifications = state.get("verifications", [])
    fingerprint = cached_workspace_fingerprint(cwd, all_paths, config)
    current_fingerprint = fingerprint["fingerprint"]
    fingerprint_complete = bool(fingerprint["metrics"]["complete"])
    configured_rules = {
        str(rule["id"]): rule for rule in config.get("verification_commands", [])
    }
    current_evidence = [
        item for item in verifications
        if item.get("workspace_fingerprint") == current_fingerprint
        and (
            not item.get("scope_root")
            or Path(str(item.get("scope_root"))).resolve(strict=False) == cwd.resolve(strict=False)
        )
        and item.get("fingerprint_complete", True)
        and fingerprint_complete
        and (
            str(item.get("verification_id", "")).startswith("builtin-")
            or (
                str(item.get("verification_id", "")) in configured_rules
                and item.get("rule_fingerprint")
                == rule_fingerprint(configured_rules[str(item.get("verification_id"))])
            )
        )
    ]
    latest_by_command: dict[tuple[str, str], dict[str, Any]] = {}
    for item in current_evidence:
        latest_by_command[verification_key(item)] = item
    latest_results = sorted(
        latest_by_command.values(), key=lambda item: int(item.get("sequence") or 0)
    )
    candidate_successful = [item for item in latest_results if item.get("success") is True]
    failed = [item for item in latest_results if item.get("success") is False]
    unknown = [item for item in latest_results if item.get("success") is None]

    blockers: list[str] = []
    warnings = list(config_warnings)
    passed: list[str] = []

    if not fingerprint_complete:
        warnings.append(
            "workspace fingerprint is incomplete: "
            + str(fingerprint["metrics"].get("limit_reason") or "unknown limit")
        )

    successful: list[dict[str, Any]] = []
    artifact_passed: list[str] = []
    for item in candidate_successful:
        identifier = str(item.get("verification_id") or "")
        rule = configured_rules.get(identifier)
        if rule is None or not rule.get("artifacts"):
            successful.append(item)
            continue
        errors = artifact_evidence_errors(cwd, item, rule)
        if errors:
            blockers.append(
                f"verification {identifier} has invalid artifact evidence: " + "; ".join(errors[:5])
            )
            continue
        successful.append(item)
        for artifact in item.get("artifact_evidence", []):
            metrics = artifact.get("metrics", {})
            if metrics:
                summary = ", ".join(f"{key} {float(value):.2f}%" for key, value in sorted(metrics.items()))
                artifact_passed.append(f"{identifier} coverage ({summary})")

    required_rules = [rule for rule in configured_rules.values() if rule.get("required")]
    coverage_map: dict[str, list[str]] = {}
    covered_by_required: set[str] = set()
    latest_by_id = {str(item.get("verification_id") or ""): item for item in latest_results}
    successful_ids = {str(item.get("verification_id") or "") for item in successful}
    for rule in required_rules:
        covered = triggered_paths_for_rule(rule, all_paths)
        if not covered:
            continue
        identifier = str(rule["id"])
        coverage_map[identifier] = covered
        covered_by_required.update(covered)
        if config.get("schema_version") == 3 and not rule.get("structured"):
            blockers.append(
                f"required verification {identifier} uses a heuristic command selector; Schema 3 requires argv"
            )
            continue
        if identifier not in successful_ids:
            if identifier not in latest_by_id:
                blockers.append(
                    f"required verification {identifier} was not recorded for: " + ", ".join(covered[:5])
                )
            elif latest_by_id[identifier].get("success") is not True:
                blockers.append(
                    f"required verification {identifier} did not succeed for: " + ", ".join(covered[:5])
                )
    uncovered = sorted(set(protected_assets) - covered_by_required) if required_rules else []
    if uncovered:
        blockers.append(
            "changed code is not covered by a required verification rule: " + ", ".join(uncovered[:5])
        )

    if relevant_code_changed and config.get("require_verification_when_code_changed") and not successful:
        missing_message = (
            "protected engineering assets changed, but no successful test, lint, typecheck, or build was recorded after the latest observed edit"
            if scope_kind == "global"
            else "code changed, but no successful test, lint, typecheck, or build was recorded after the latest observed edit"
        )
        blockers.append(missing_message)
        if verifications and not current_evidence:
            warnings.append("recorded verification evidence is stale because relevant engineering content changed")
    if failed and config.get("block_on_failed_verification"):
        latest = failed[-1]
        blockers.append(f"the latest recorded {latest['kind']} command failed: {latest['command']}")
    if successful:
        labels = ", ".join(f"{item['kind']}: {item['command']}" for item in successful[-3:])
        passed.append(f"successful verification recorded ({labels})")
    passed.extend(artifact_passed)
    if coverage_map and not uncovered:
        passed.append(f"required verification coverage mapped for {len(covered_by_required)} changed file(s)")
    if unknown:
        warnings.append("some verification commands had an unknown exit status and were not counted as successful")

    debug_paths = [path for path in all_paths if not Path(path).is_absolute()]
    debug = added_debug_markers(cwd, debug_paths, config)
    if debug["warnings"]:
        warnings.append("new debug or temporary markers found: " + "; ".join(debug["warnings"]))
    if debug["blockers"]:
        blockers.append("blocking debug markers found: " + "; ".join(debug["blockers"]))
    if not debug["scan"]["complete"]:
        incomplete = [
            f"{item['path']} ({item.get('error') or 'unknown error'})"
            for item in debug["scan"]["files"] if not item.get("complete")
        ]
        warnings.append("debug marker scan is incomplete: " + "; ".join(incomplete[:5]))

    sensitive = sensitive_paths(all_paths)
    if sensitive:
        message = "sensitive-looking files changed: " + ", ".join(sensitive[:5])
        (blockers if config.get("block_on_sensitive_files") else warnings).append(message)

    if all_paths:
        passed.append(f"{len(all_paths)} changed or touched file(s) inspected")

    report = {
        "schema_version": 3,
        "checked_at": now_iso(),
        "session_id": str(event.get("session_id") or state.get("session_id") or "unknown"),
        "turn_id": str(event.get("turn_id") or "unknown"),
        "cwd": str(cwd),
        "project_name": cwd.name or str(cwd),
        "scope_kind": scope_kind,
        "mode": config["mode"],
        "companion_enabled": config["companion_enabled"],
        "notification_policy": config["notification_policy"],
        "temporary_report_ttl_hours": config["temporary_report_ttl_hours"],
        "workspace_fingerprint": current_fingerprint,
        "fingerprint_metrics": fingerprint["metrics"],
        "changed_paths": all_paths,
        "verification_coverage": coverage_map,
        "debug_scan": debug["scan"],
        "verification_evidence": latest_results[-10:],
        "passed": passed,
        "warnings": warnings,
        "blockers": blockers,
    }
    report["display"] = plain_language_report(report)
    return report


def report_status(report: dict[str, Any]) -> str:
    if report.get("blockers"):
        return "issue"
    if report.get("warnings"):
        return "warning"
    return "success"


def report_identifier(report: dict[str, Any]) -> str:
    identity = json.dumps({
        "session_id": report.get("session_id"),
        "turn_id": report.get("turn_id"),
        "workspace_fingerprint": report.get("workspace_fingerprint"),
        "passed": report.get("passed"),
        "warnings": report.get("warnings"),
        "blockers": report.get("blockers"),
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    session = safe_id(str(report.get("session_id") or "unknown"))[:48]
    turn = safe_id(str(report.get("turn_id") or "unknown"))[:48]
    return f"{session}-{turn}-{digest}"


def notification_signature(report: dict[str, Any]) -> str:
    """Identify a completion state without treating every turn as a new issue."""
    identity = json.dumps({
        "cwd": str(Path(str(report.get("cwd") or "")).resolve(strict=False)),
        "scope_kind": report.get("scope_kind"),
        "workspace_fingerprint": report.get("workspace_fingerprint"),
        "status": report_status(report),
        "warnings": report.get("warnings", []),
        "blockers": report.get("blockers", []),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(identity).hexdigest()


def notification_cache_path() -> Path:
    return plugin_data_dir() / "notification-cache.json"


def notification_scope_key(report: dict[str, Any]) -> str:
    value = str(Path(str(report.get("cwd") or "")).resolve(strict=False))
    return hashlib.sha256(value.encode()).hexdigest()


def notification_is_duplicate(report: dict[str, Any]) -> bool:
    cache = load_json(notification_cache_path(), {})
    if not isinstance(cache, dict):
        return False
    item = cache.get(notification_scope_key(report))
    return isinstance(item, dict) and item.get("signature") == notification_signature(report)


def record_notification(report: dict[str, Any]) -> None:
    cache = load_json(notification_cache_path(), {})
    if not isinstance(cache, dict):
        cache = {}
    cache[notification_scope_key(report)] = {
        "signature": notification_signature(report),
        "delivered_at": now_iso(),
    }
    save_json(notification_cache_path(), cache)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temp.write_text(value, encoding="utf-8")
    temp.replace(path)


def report_html(report: dict[str, Any]) -> str:
    def finding_section(title: str, values: list[dict[str, Any]], css_class: str) -> str:
        if not values:
            return ""
        items = ""
        for value in values:
            heading = html.escape(str(value.get("title") or "检查记录"))
            detail = html.escape(str(value.get("detail") or ""))
            next_step = html.escape(str(value.get("next_step") or ""))
            technical = html.escape(str(value.get("technical_detail") or ""))
            action_html = f'<p class="action"><strong>建议：</strong>{next_step}</p>' if next_step else ""
            technical_html = f'<details><summary>查看技术详情</summary><code>{technical}</code></details>' if technical else ""
            items += f"<li><strong>{heading}</strong><p>{detail}</p>{action_html}{technical_html}</li>"
        return f'<section class="{css_class}"><h2>{title}</h2><ul>{items}</ul></section>'

    def checks_section(values: list[dict[str, Any]]) -> str:
        cards = "".join(
            '<article class="check '
            + html.escape(str(item.get("status") or "neutral"))
            + '"><strong>'
            + html.escape(str(item.get("title") or "检查项目"))
            + '</strong><p>'
            + html.escape(str(item.get("detail") or ""))
            + "</p></article>"
            for item in values
        )
        return f'<section><h2>DoneGuard 检查了什么</h2><div class="checks">{cards}</div></section>'

    display = report.get("display") or plain_language_report(report)
    title = html.escape(str(report.get("project_name") or "DoneGuard"))
    checked_at = html.escape(str(report.get("checked_at") or ""))
    changed = [html.escape(str(value)) for value in report.get("changed_paths", [])]
    changed_html = "".join(f"<code>{value}</code>" for value in changed) or "<span>无相关文件变更</span>"
    status = report_status(report)
    status_label = {"success": "检查完成", "warning": "存在提醒", "issue": "需要处理"}[status]
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DoneGuard · {title}</title><style>
:root{{--ink:#18322f;--muted:#667975;--paper:#fffdf8;--green:#1f8a70;--amber:#d88718;--red:#c6533d}}
*{{box-sizing:border-box}} body{{margin:0;background:#edf4ef;color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
main{{max-width:820px;margin:40px auto;padding:34px;background:var(--paper);border:1px solid #dbe7df;border-radius:24px;box-shadow:0 18px 60px #244b3d20}}
.eyebrow{{color:var(--green);font-weight:700;letter-spacing:.08em}} h1{{margin:.2em 0;font-size:30px}} .meta{{color:var(--muted)}} .summary{{font-size:17px;max-width:680px}}
.pill{{display:inline-block;margin:10px 0 18px;padding:6px 12px;border-radius:99px;background:#e2f4eb;color:var(--green);font-weight:700}}
section{{margin-top:20px;padding:18px 20px;border-radius:16px;background:#f5f7f5}} section.issue{{background:#fff0ea}} section.warning{{background:#fff6df}}
h2{{margin:0 0 8px;font-size:17px}} ul{{margin:0;padding-left:21px}} li+li{{margin-top:16px}} li p{{margin:3px 0}} .action{{color:#314d47}} details{{margin-top:6px;color:var(--muted)}} details code{{display:block;margin-top:6px;white-space:pre-wrap}} .checks{{display:grid;grid-template-columns:1fr 1fr;gap:10px}} .check{{padding:12px;border-radius:12px;background:#fff}} .check p{{margin:4px 0 0;color:var(--muted)}} .check.issue{{border-left:4px solid var(--red)}} .check.warning{{border-left:4px solid var(--amber)}} .check.passed{{border-left:4px solid var(--green)}} .paths{{display:flex;flex-wrap:wrap;gap:8px}} code{{padding:4px 8px;border-radius:8px;background:#e8efeb}}
footer{{margin-top:26px;color:var(--muted);font-size:13px}}
@media(max-width:640px){{main{{margin:0;padding:22px;border-radius:0}}.checks{{grid-template-columns:1fr}}}}
</style></head><body><main><div class="eyebrow">DONEGUARD 完成检查报告</div><h1>{title} · {status_label}</h1>
<div class="pill">{status_label}</div><h2>{html.escape(str(display.get("headline") or status_label))}</h2>
<p class="summary">{html.escape(str(display.get("summary") or ""))}</p>
<div class="meta">检查时间 {checked_at} · {html.escape(str(display.get("mode_label") or report.get("mode") or ""))}</div>
{checks_section(list(display.get("checks", [])))}
{finding_section("为什么暂时不能确认完成", list(display.get("blockers", [])), "issue")}
{finding_section("还有这些内容值得留意", list(display.get("warnings", [])), "warning")}
{finding_section("已经确认的内容", list(display.get("passed", [])), "passed")}
<section><h2>本次检查涉及的文件</h2><div class="paths">{changed_html}</div></section>
<footer>DoneGuard 提供的是完成证据，不等同于需求正确性或完整测试覆盖。</footer></main></body></html>"""


def cleanup_temporary_reports(ttl_hours: int) -> None:
    temporary = plugin_data_dir() / "reports" / "temporary"
    if not temporary.exists():
        return
    cutoff = time.time() - ttl_hours * 3600
    for path in temporary.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                report_id = path.name
                shutil.rmtree(path)
                try:
                    (plugin_data_dir() / "events" / f"{report_id}.json").unlink()
                except FileNotFoundError:
                    pass
        except OSError:
            continue


def save_report(report: dict[str, Any], enqueue: bool = True) -> Path:
    reports = plugin_data_dir() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report = {**report, "report_id": report_identifier(report), "status": report_status(report)}
    if not report.get("display"):
        report["display"] = plain_language_report(report)
    save_json(reports / "latest.json", report)
    if not enqueue:
        return reports / "latest.json"

    cleanup_temporary_reports(int(report.get("temporary_report_ttl_hours") or 24))
    bundle = reports / "temporary" / str(report["report_id"])
    report_path = bundle / "report.json"
    html_path = bundle / "report.html"
    save_json(report_path, report)
    write_text(html_path, report_html(report))
    save_json(plugin_data_dir() / "events" / f"{report['report_id']}.json", {
        "schema_version": 1,
        "report_id": report["report_id"],
        "status": report["status"],
        "project_name": report.get("project_name"),
        "checked_at": report.get("checked_at"),
        "report_path": str(report_path),
        "html_path": str(html_path),
    })
    return report_path


def companion_app_path() -> Path:
    return plugin_data_dir() / "DoneGuard Companion.app"


def launch_companion() -> bool:
    app = companion_app_path()
    if sys.platform != "darwin" or not app.exists():
        return False
    try:
        completed = subprocess.run(
            ["open", "-gj", str(app), "--args", "--data-dir", str(plugin_data_dir())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def finalize_report(report_id: str, keep: bool) -> Path | None:
    identifier = safe_id(report_id)
    if identifier != report_id:
        raise ValueError("invalid report id")
    data = plugin_data_dir()
    source = data / "reports" / "temporary" / identifier
    event = data / "events" / f"{identifier}.json"
    try:
        event.unlink()
    except FileNotFoundError:
        pass
    if not source.exists():
        return None
    if not keep:
        shutil.rmtree(source)
        return None
    destination = data / "reports" / "saved" / identifier
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination
    source.replace(destination)
    return destination


def format_report(report: dict[str, Any]) -> str:
    display = report.get("display") or plain_language_report(report)
    pieces = [
        "DoneGuard 完成检查",
        f"结论：{display['headline']}",
        str(display["summary"]),
        f"模式：{display['mode_label']}",
        "检查内容：" + "；".join(
            f"{item['title']}—{item['detail']}" for item in display.get("checks", [])
        ),
    ]
    for heading, key in (
        ("需要处理", "blockers"),
        ("提醒", "warnings"),
        ("已确认", "passed"),
    ):
        findings = display.get(key, [])
        if not findings:
            continue
        pieces.append(heading + "：")
        for finding in findings:
            line = f"- {finding['title']}：{finding['detail']}"
            if finding.get("next_step"):
                line += f" 建议：{finding['next_step']}"
            if finding.get("technical_detail"):
                line += f" 技术详情：{finding['technical_detail']}"
            pieces.append(line)
    return "\n".join(pieces)


def _handle_hook_locked(event: dict[str, Any]) -> dict[str, Any] | None:
    session_id = str(event.get("session_id") or "unknown")
    path = state_path(session_id)
    hook_name = str(event.get("hook_event_name") or "")

    if hook_name == "SessionStart" and event.get("source") in {"startup", "clear"}:
        save_json(path, new_state(event))
        return None

    state = load_json(path, None)
    if not isinstance(state, dict):
        state = new_state(event)
    state["sequence"] = int(state.get("sequence") or 0) + 1
    state["cwd"] = str(event.get("cwd") or state.get("cwd") or os.getcwd())

    if hook_name == "UserPromptSubmit":
        state["prompt_count"] = int(state.get("prompt_count") or 0) + 1
        state["turn_started_sequence"] = state["sequence"]
        state["turn_files_touched"] = []
        cwd = Path(str(event.get("cwd") or state.get("cwd") or os.getcwd())).resolve()
        state["turn_git_baseline"] = git_workspace_snapshot(cwd)
        state.pop("last_prompt", None)
        save_json(path, state)
        return None

    if hook_name == "PostToolUse":
        tool_name = str(event.get("tool_name") or "")
        command = command_from_event(event)
        if tool_name == "apply_patch":
            touched = absolute_patch_paths(event, state.get("cwd") or os.getcwd())
            paths = set(state.get("files_touched", []))
            paths.update(touched)
            state["files_touched"] = sorted(paths)
            turn_paths = set(state.get("turn_files_touched", []))
            turn_paths.update(touched)
            state["turn_files_touched"] = sorted(turn_paths)
            state["last_change_sequence"] = state["sequence"]
        elif tool_name == "Bash":
            cwd = event_working_directory(event, state.get("cwd") or os.getcwd())
            scope = determine_turn_scope(cwd, state)
            if scope["active"]:
                scope_root = Path(scope["root"])
                scope_kind = str(scope["kind"])
            else:
                global_root = managed_global_root(cwd)
                scope_root = global_root or guard_root(cwd)
                scope_kind = "global" if global_root is not None else ("git" if repo_root(scope_root) is not None else "configured")
            config, _ = runtime_config(scope_root, scope_kind)
            rule = match_verification(command, config, cwd)
            if rule:
                exit_code = extract_exit_code(event.get("tool_response"))
                exit_code_source = "tool_response"
                if exit_code is None:
                    exit_code = transcript_exit_code(event, command)
                    exit_code_source = "transcript" if exit_code is not None else "unknown"
                current_paths = list(scope["paths"]) if scope["active"] else changed_paths(scope_root)
                fingerprint = cached_workspace_fingerprint(scope_root, current_paths, config)
                state.setdefault("verifications", []).append({
                    "sequence": state["sequence"],
                    "verification_id": rule["id"],
                    "rule_fingerprint": rule_fingerprint(rule),
                    "kind": rule["kind"],
                    "evidence_strength": "structured" if rule.get("structured") else "heuristic",
                    "command": redact_command(command),
                    "exit_code": exit_code,
                    "exit_code_source": exit_code_source,
                    "success": None if exit_code is None else exit_code == 0,
                    "scope_root": str(scope_root.resolve(strict=False)),
                    "scope_kind": scope_kind,
                    "workspace_fingerprint": fingerprint["fingerprint"],
                    "fingerprint_complete": fingerprint["metrics"]["complete"],
                    "fingerprint_metrics": fingerprint["metrics"],
                    "artifact_evidence": capture_artifact_evidence(cwd, rule),
                    "recorded_at": now_iso(),
                })
                state["verifications"] = state["verifications"][-30:]
        save_json(path, state)
        return None

    if hook_name == "Stop":
        save_json(path, state)
        report = evaluate(event, state)
        if report is None:
            return None
        message = format_report(report)
        mode = report["mode"]
        strict_continuation = (
            mode == "strict"
            and bool(report["blockers"])
            and not bool(event.get("stop_hook_active"))
        )
        policy = report.get("notification_policy", "always")
        has_issue = bool(report["warnings"] or report["blockers"])
        wants_delivery = (
            mode != "observe"
            and policy != "never"
            and (policy == "always" or has_issue)
            and not strict_continuation
        )
        wants_popup = bool(report.get("companion_enabled")) and wants_delivery
        duplicate_notification = wants_delivery and notification_is_duplicate(report)
        companion_available = companion_app_path().exists()
        save_report(
            report,
            enqueue=wants_popup and not duplicate_notification and companion_available,
        )
        delivered_to_companion = (
            wants_popup
            and not duplicate_notification
            and companion_available
            and launch_companion()
        )
        if delivered_to_companion:
            record_notification(report)

        if mode == "observe":
            return None
        if strict_continuation:
            return {
                "decision": "block",
                "reason": message + "\nBefore finishing, address the blocking evidence or explain why it cannot be produced."
            }
        if not wants_delivery:
            return None
        if duplicate_notification:
            return None
        if delivered_to_companion:
            return None
        if not report["changed_paths"] and not report["warnings"] and not report["blockers"]:
            return None
        record_notification(report)
        return {"systemMessage": message}

    if hook_name == "SessionEnd":
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None

    save_json(path, state)
    return None


def handle_hook(event: dict[str, Any]) -> dict[str, Any] | None:
    session_id = str(event.get("session_id") or "unknown")
    try:
        with state_lock(session_id):
            return _handle_hook_locked(event)
    except TimeoutError as exc:
        return {"systemMessage": str(exc)}


def latest_report(cwd: Path | None) -> dict[str, Any] | None:
    reports = plugin_data_dir() / "reports"
    if not reports.exists():
        return None
    latest = load_json(reports / "latest.json", None)
    if isinstance(latest, dict):
        if cwd is None or Path(str(latest.get("cwd", ""))).resolve() == cwd.resolve():
            return latest
    candidates = sorted(
        list((reports / "saved").glob("*/report.json"))
        + list((reports / "temporary").glob("*/report.json")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        report = load_json(path, None)
        if not isinstance(report, dict):
            continue
        if cwd is None or Path(str(report.get("cwd", ""))).resolve() == cwd.resolve():
            return report
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="DoneGuard completion evidence checker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hook", help="Process a Codex hook event from stdin")
    status_parser = subparsers.add_parser("status", help="Show the latest saved report")
    status_parser.add_argument("--cwd", type=Path)
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    action_parser = subparsers.add_parser("report-action", help="Save or discard a temporary report")
    action_parser.add_argument("action", choices=("save", "discard"))
    action_parser.add_argument("report_id")
    args = parser.parse_args()

    if args.command == "hook":
        try:
            event = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(json.dumps({"systemMessage": f"DoneGuard received invalid hook JSON: {exc}"}))
            return 0
        result = handle_hook(event)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "report-action":
        try:
            destination = finalize_report(args.report_id, keep=args.action == "save")
        except ValueError as exc:
            print(f"DoneGuard could not update the report: {exc}", file=sys.stderr)
            return 2
        if args.action == "save":
            if destination is None:
                print("DoneGuard could not find that temporary report.", file=sys.stderr)
                return 1
            print(destination)
        return 0

    report = latest_report(args.cwd)
    if report is None:
        print("DoneGuard has not saved a report for this project yet.")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.as_json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
