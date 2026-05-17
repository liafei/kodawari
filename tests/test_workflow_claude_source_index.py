from pathlib import Path
import re


def _read(path: Path) -> str:
    assert path.exists(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def test_workflow_claude_history_is_preserved_in_consolidated_overview_doc() -> None:
    root = Path(__file__).resolve().parents[1]
    overview = _read(root / "docs" / "architecture" / "一、平台现状、架构与兼容总览.md")

    assert "# 一、平台现状、架构与兼容总览" in overview
    assert "## 5. 兼容与历史吸收" in overview
    assert "`workflow-claude` 吸收策略" in overview
    assert "历史源码只作为只读证据" in overview
    assert "兼容命令族包括" in overview
    assert "WORKFLOW_CLAUDE_SOURCE_INDEX.md" in overview
    assert "WORKFLOW_CLAUDE_TEST_DAMAGE_INDEX.md" in overview
    assert "WORKFLOW_CLAUDE_ABSORPTION_PRIORITY.md" in overview


def test_consolidated_docs_keep_explicit_module_mapping_examples() -> None:
    root = Path(__file__).resolve().parents[1]
    overview = _read(root / "docs" / "architecture" / "一、平台现状、架构与兼容总览.md")
    entry_doc = _read(root / "项目说明.md")

    assert re.search(r"`kodawari\.cli\.main`\s*（主 CLI 入口）", entry_doc)
    assert re.search(r"`kodawari\.autopilot\.engine`\s*（引擎装配层）", overview)
    assert re.search(r"`kodawari\.autopilot\.local_adapter`\s*（本地适配层）", overview)


def test_runbook_records_ws_270_281_as_completed_and_diagram_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    runbook = _read(root / "docs" / "operations" / "二、运行操作、门禁规则与后续路线.md")

    assert "WS-270 ~ WS-281" in runbook
    assert "当前状态为 `Done`" in runbook
    assert "`WS-281: 现有 fitness checks 纳入 ratchet` `Done`" in runbook
    assert "当前结果：`60 passed`" in runbook
    assert (root / "docs" / "architecture" / "diagrams" / "autopilot_flow.mmd").exists()
