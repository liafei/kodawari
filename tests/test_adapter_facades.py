from __future__ import annotations

from pathlib import Path


def test_claude_and_codex_adapter_facades_route_to_canonical_kodawari() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    claude_skills = repo_root / "adapters" / "claude-code-plugin" / "skills"
    codex_skills = repo_root / "adapters" / "codex-cli" / ".codex" / "skills"

    claude_expected = {
        "wf-setup.md": ("kodawari wf-setup", "kodawari setup"),
        "wf-plan.md": ("kodawari wf-plan", "kodawari plan"),
        "wf-work.md": ("kodawari wf-work", "kodawari work"),
        "wf-work-all.md": ("kodawari wf-work-all", "kodawari work all"),
        "wf-review.md": ("kodawari wf-review", "kodawari review"),
        "wf-release.md": ("kodawari wf-release", "kodawari release"),
        "wf-status.md": ("kodawari wf-status", "kodawari status"),
    }
    codex_expected = {
        "wf-setup": ("kodawari wf-setup", "kodawari setup"),
        "wf-plan": ("kodawari wf-plan", "kodawari plan"),
        "wf-work": ("kodawari wf-work", "kodawari work"),
        "wf-work-all": ("kodawari wf-work-all", "kodawari work all"),
        "wf-review": ("kodawari wf-review", "kodawari review"),
        "wf-release": ("kodawari wf-release", "kodawari release"),
        "wf-status": ("kodawari wf-status", "kodawari status"),
    }

    for filename, (facade_cmd, canonical_cmd) in claude_expected.items():
        path = claude_skills / filename
        assert path.exists(), str(path)
        text = path.read_text(encoding="utf-8")
        assert facade_cmd in text
        assert canonical_cmd in text

    for skill_name, (facade_cmd, canonical_cmd) in codex_expected.items():
        path = codex_skills / skill_name / "SKILL.md"
        assert path.exists(), str(path)
        text = path.read_text(encoding="utf-8")
        assert facade_cmd in text
        assert canonical_cmd in text


def test_adapter_policy_outputs_exist() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    claude_plugin_root = repo_root / "adapters" / "claude-code-plugin" / ".claude-plugin"
    codex_rules_path = repo_root / "adapters" / "codex-cli" / ".codex" / "rules" / "workflow.rules"

    hooks_path = claude_plugin_root / "hooks.json"
    settings_path = claude_plugin_root / "settings.json"
    plugin_path = claude_plugin_root / "plugin.json"

    assert plugin_path.exists()
    assert hooks_path.exists()
    assert settings_path.exists()
    assert codex_rules_path.exists()

    hooks_text = hooks_path.read_text(encoding="utf-8")
    settings_text = settings_path.read_text(encoding="utf-8")
    rules_text = codex_rules_path.read_text(encoding="utf-8")

    assert "\"PreToolUse\"" in hooks_text
    assert "\"policy_name\"" in settings_text
    assert "prefix_rule(" in rules_text
