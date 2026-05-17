from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_compile_adapter_policies_is_deterministic(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "compile_adapter_policies.py"
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "execution.guard.policy.v1",
                "policy_name": "deterministic-test",
                "deny": [
                    {"pattern": "git\\s+push\\s+--force", "reason": "Force push blocked"},
                    {"pattern": "\\bsudo\\b", "reason": "Sudo commands blocked"},
                ],
                "ask": [
                    {"pattern": "rm\\s+-rf", "reason": "Recursive delete requires confirmation"},
                ],
                "allow": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    hooks_out = tmp_path / "hooks.json"
    settings_out = tmp_path / "settings.json"
    rules_out = tmp_path / "workflow.rules"

    command = [
        sys.executable,
        str(script),
        "--policy",
        str(policy_path),
        "--claude-hooks-out",
        str(hooks_out),
        "--claude-settings-out",
        str(settings_out),
        "--codex-rules-out",
        str(rules_out),
    ]

    run_1 = subprocess.run(command, capture_output=True, text=True, cwd=str(repo_root))
    assert run_1.returncode == 0, run_1.stderr
    first_hooks = hooks_out.read_text(encoding="utf-8")
    first_settings = settings_out.read_text(encoding="utf-8")
    first_rules = rules_out.read_text(encoding="utf-8")

    run_2 = subprocess.run(command, capture_output=True, text=True, cwd=str(repo_root))
    assert run_2.returncode == 0, run_2.stderr
    second_hooks = hooks_out.read_text(encoding="utf-8")
    second_settings = settings_out.read_text(encoding="utf-8")
    second_rules = rules_out.read_text(encoding="utf-8")

    assert first_hooks == second_hooks
    assert first_settings == second_settings
    assert first_rules == second_rules

    hooks_payload = json.loads(first_hooks)
    assert hooks_payload["schema_version"] == "claude.hooks.v1"
    assert len(list(hooks_payload.get("hooks", {}).get("PreToolUse", []))) == 3
    assert "prefix_rule(action=\"deny\"" in first_rules
    assert "prefix_rule(action=\"ask\"" in first_rules
