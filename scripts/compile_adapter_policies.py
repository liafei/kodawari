#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read_json_like(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return dict(json.loads(text))


def _normalize_rules(payload: dict[str, Any], key: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in list(payload.get(key) or []):
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not pattern:
            continue
        out.append({"pattern": pattern, "reason": reason})
    return out


def _compile_claude_hooks(policy: dict[str, Any]) -> dict[str, Any]:
    deny = _normalize_rules(policy, "deny")
    ask = _normalize_rules(policy, "ask")
    rules: list[dict[str, Any]] = []
    idx = 0
    for action, source in (("deny", deny), ("ask", ask)):
        for item in source:
            idx += 1
            rules.append(
                {
                    "id": f"{action}_{idx:03d}",
                    "action": action,
                    "pattern": item["pattern"],
                    "reason": item["reason"],
                }
            )
    return {
        "schema_version": "claude.hooks.v1",
        "policy_schema_version": str(policy.get("schema_version") or ""),
        "policy_name": str(policy.get("policy_name") or "default"),
        "hooks": {"PreToolUse": rules},
    }


def _compile_codex_rules(policy: dict[str, Any]) -> str:
    lines = [
        "# kodawari generated rules",
        f"# policy_name={str(policy.get('policy_name') or 'default')}",
        f"# policy_schema_version={str(policy.get('schema_version') or '')}",
        "",
    ]
    for action in ("deny", "ask"):
        for item in _normalize_rules(policy, action):
            pattern = item["pattern"].replace("\\", "\\\\").replace('"', '\\"')
            reason = item["reason"].replace("\\", "\\\\").replace('"', '\\"')
            lines.append(
                f'prefix_rule(action="{action}", pattern="{pattern}", reason="{reason}")'
            )
    lines.append("")
    return "\n".join(lines)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile canonical execution guard policy into adapter-facing rule formats."
    )
    parser.add_argument(
        "--policy",
        default="src/kodawari/safety/policies/default.yaml",
        help="Path to canonical policy payload (JSON-compatible).",
    )
    parser.add_argument(
        "--claude-hooks-out",
        default="adapters/claude-code-plugin/.claude-plugin/hooks.json",
        help="Output path for Claude hooks payload.",
    )
    parser.add_argument(
        "--claude-settings-out",
        default="adapters/claude-code-plugin/.claude-plugin/settings.json",
        help="Output path for Claude adapter settings payload.",
    )
    parser.add_argument(
        "--codex-rules-out",
        default="adapters/codex-cli/.codex/rules/workflow.rules",
        help="Output path for Codex rules file.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    policy_path = Path(args.policy).resolve()
    policy = _read_json_like(policy_path)
    hooks = _compile_claude_hooks(policy)
    codex_rules = _compile_codex_rules(policy)
    settings = {
        "schema_version": "adapter.settings.v1",
        "source_policy": str(policy_path),
        "policy_name": str(policy.get("policy_name") or "default"),
    }
    _write_json(Path(args.claude_hooks_out), hooks)
    _write_json(Path(args.claude_settings_out), settings)
    _write_text(Path(args.codex_rules_out), codex_rules)
    print(
        json.dumps(
            {
                "status": "PASS",
                "policy": str(policy_path),
                "claude_hooks_out": str(Path(args.claude_hooks_out)),
                "claude_settings_out": str(Path(args.claude_settings_out)),
                "codex_rules_out": str(Path(args.codex_rules_out)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
