import json
from pathlib import Path

from kodawari.autopilot.architecture_plan import build_architecture_plan
from kodawari.autopilot.prd_contract import build_prd_intake, validate_prd_intake
from kodawari.autopilot.repo_inventory import build_repo_inventory
from kodawari.autopilot.task_card import build_task_card, render_task_card_markdown, validate_task_card
from kodawari.autopilot.task_graph import build_task_graph, render_task_graph_markdown, validate_task_graph


def _sample_prd() -> str:
    return "\n".join(
        [
            "Build a feed ranking API that returns stable article ordering.",
            "source of truth: db.articles and cache.feed",
            "Scope touches route/service/repository layers.",
            "This feature updates write path and read path.",
            "out of scope: frontend redesign",
        ]
    )


def test_prd_intake_contains_required_fields() -> None:
    intake = build_prd_intake(_sample_prd(), feature="contract-demo")
    errors = validate_prd_intake(intake)

    assert not errors
    assert intake["business_outcome"]
    assert intake["path_type"] in {"read", "write", "both"}
    assert "source_of_truth" in intake and intake["source_of_truth"]
    assert "layers" in intake and intake["layers"]


def test_prd_intake_business_outcome_ignores_markdown_heading() -> None:
    prd = "\n".join(
        [
            "# Feature PRD: Caregiver Profile Update",
            "",
            "## Goal",
            "Allow operators to update caregiver relation and phone for an existing patient.",
            "",
            "## Non-Goals",
            "- UI redesign",
        ]
    )
    intake = build_prd_intake(prd, feature="caregiver-profile-update")
    assert not intake["business_outcome"].startswith("#")
    assert "Allow operators to update caregiver relation" in intake["business_outcome"]
    assert "UI redesign" in intake["out_of_scope"]


def test_prd_intake_extracts_structured_chinese_sections_with_high_confidence() -> None:
    prd = "\n".join(
        [
            "1. business outcome（业务结果）",
            "- 让用户在喝水记录页看到当前饮水目标和最近一次调整历史。",
            "",
            "2. source of truth（真实数据源）",
            "- patient_settings.daily_water_goal_ml",
            "- reminder_events.amount_ml",
            "",
            "3. flow type（流程类型）",
            "- 这是 read path，只影响 current snapshot，不改 future generation。",
            "",
            "4. layer ownership（层级归属）",
            "- schema：不需要改",
            "- repository/data layer：需要读取 patient_settings.daily_water_goal_ml",
            "- service layer：需要聚合展示字段",
            "- route layer：需要暴露接口",
            "- frontend/UI：不做",
            "",
            "7. non-goals（这次不做什么）",
            "- 不改提醒生成逻辑",
        ]
    )
    intake = build_prd_intake(prd, feature="hydration-history")

    assert intake["business_outcome"] == "让用户在喝水记录页看到当前饮水目标和最近一次调整历史。"
    assert intake["source_of_truth"] == ["patient_settings.daily_water_goal_ml", "reminder_events.amount_ml"]
    assert intake["source_of_truth_canonical"] == ["db.patient_settings", "db.reminder_events"]
    assert intake["path_type"] == "read"
    assert intake["layers"] == ["repository", "service", "route"]
    assert "layer:repository" in intake["coverage_hints"]
    assert "sot:db.patient_settings" in intake["coverage_hints"]
    assert intake["out_of_scope"] == ["不改提醒生成逻辑"]
    assert intake["confidence"] == "high"
    assert intake["confidence_issues"] == []
    assert not validate_prd_intake(intake)


def test_prd_intake_marks_fallback_and_negative_outcome_low_confidence() -> None:
    prd = "\n".join(
        [
            "1. business outcome",
            "- 不要顺手重构 UI。",
        ]
    )
    intake = build_prd_intake(prd, feature="low-confidence-demo")

    assert intake["confidence"] == "low"
    assert any("source_of_truth fell back" in item for item in intake["confidence_issues"])
    assert any("layers fell back" in item for item in intake["confidence_issues"])
    assert any("business_outcome looks like a non-goal" in item for item in intake["confidence_issues"])


def test_task_graph_enforces_core_files_limit() -> None:
    intake = build_prd_intake(_sample_prd(), feature="contract-demo")
    graph = build_task_graph(intake)
    errors = validate_task_graph(graph)

    assert not errors
    assert graph["tasks"]
    for task in graph["tasks"]:
        assert len(task["core_files"]) <= 3
    markdown = render_task_graph_markdown(graph)
    assert "# Task Graph" in markdown
    assert "## T1" in markdown


def test_task_graph_prefers_app_layout_when_project_has_app_directory(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
    (tmp_path / "app" / "schemas.py").write_text("class Payload: ...\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    intake = build_prd_intake(
        "Support route/service/repository update with schema adjustments.",
        feature="app-layout-demo",
    )
    graph = build_task_graph(intake, project_root=tmp_path)
    assert graph["tasks"]
    assert graph["project_profile"] == "fastapi"
    assert graph["project_layout"]["kind"] == "app"
    assert graph["executability"]["status"] == "PASS"
    assert "layer:service" in graph["coverage_hints"]
    assert graph["boundary_debt"]["status"] == "WARN"
    debt_item = next(item for item in graph["boundary_debt"]["items"] if item["file"] == "app/main.py")
    assert debt_item["severity"] == "high"
    assert debt_item["touched_in_feature"] is True
    assert debt_item["recommended_split"]
    core_files = [item for task in graph["tasks"] for item in list(task.get("core_files") or [])]
    assert any(path.startswith("app/") for path in core_files)
    assert "tests/test_api.py" in core_files
    assert "app/repository.py" not in core_files
    assert "app/services.py" not in core_files
    assert "app/static/index.html" not in core_files


def test_repo_inventory_and_architecture_plan_capture_generic_surfaces(tmp_path: Path) -> None:
    (tmp_path / "backend" / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "app" / "main.py").write_text("def handler():\n    return 'ok'\n", encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    (tmp_path / "web" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "web" / "src" / "App.js").write_text("module.exports = { renderApp: () => 'ok' };\n", encoding="utf-8")
    (tmp_path / "web" / "src" / "App.test.js").write_text("console.log('ok')\n", encoding="utf-8")
    (tmp_path / "web" / "package.json").write_text(
        json.dumps({"name": "demo", "private": True, "scripts": {"test": "node src/App.test.js"}}),
        encoding="utf-8",
    )
    intake = build_prd_intake(
        "\n".join(
            [
                "Return backend data and show it in frontend.",
                "source of truth: db.widgets",
                "layer: service route frontend",
            ]
        ),
        feature="fullstack-demo",
    )

    inventory = build_repo_inventory(
        project_root=tmp_path,
        archetype="fullstack_fastapi_react",
        capabilities=["docs_runbook"],
        mode="existing",
    )
    plan = build_architecture_plan(
        project_root=tmp_path,
        prd_intake=intake,
        repo_inventory=inventory,
        planning_mode="existing",
    )

    assert inventory["archetype"] == "fullstack_fastapi_react"
    assert [item["name"] for item in inventory["surfaces"]] == ["backend", "frontend", "docs"]
    assert plan["recommended_layers"][:2] == ["schema", "repository"]
    assert any(item["surface"] == "frontend" for item in plan["module_boundaries"])
    assert any(item["surface"] == "frontend" and item["required"] for item in plan["verify_recipes"])


def test_confidence_issues_ignores_missing_verify_recipe(tmp_path: Path) -> None:
    # docs surface has no scripts/verify_docs_runbook.py → required=False, but that must NOT become a PRD confidence issue
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "main.py").write_text("", encoding="utf-8")
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "README.md").write_text("# docs", encoding="utf-8")

    intake = build_prd_intake(
        "\n".join([
            "Add a social aggregation endpoint.",
            "source of truth: db.social_events",
            "layer: service route",
        ]),
        feature="social-agg",
    )
    inventory = build_repo_inventory(
        project_root=tmp_path,
        archetype="fastapi_api",
        capabilities=["docs_runbook"],
        mode="existing",
    )
    plan = build_architecture_plan(
        project_root=tmp_path,
        prd_intake=intake,
        repo_inventory=inventory,
        planning_mode="existing",
    )

    docs_recipe = next((r for r in plan["verify_recipes"] if r["surface"] == "docs"), None)
    assert docs_recipe is not None
    assert not docs_recipe["required"], "docs recipe should be required=False when no verify script exists"
    assert plan["confidence_issues"] == [], (
        "missing verify recipe must not pollute confidence_issues; got: " + str(plan["confidence_issues"])
    )


def test_confidence_issues_passes_through_prd_level_issues(tmp_path: Path) -> None:
    # PRD-level low confidence should still propagate unchanged
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "main.py").write_text("", encoding="utf-8")

    intake = build_prd_intake("Add a social aggregation endpoint.", feature="social-agg")
    # Inject a synthetic PRD-level confidence issue
    intake["confidence_issues"] = ["scope unclear: social vs. push notifications"]

    inventory = build_repo_inventory(
        project_root=tmp_path,
        archetype="fastapi_api",
        capabilities=[],
        mode="existing",
    )
    plan = build_architecture_plan(
        project_root=tmp_path,
        prd_intake=intake,
        repo_inventory=inventory,
        planning_mode="existing",
    )

    assert "scope unclear: social vs. push notifications" in plan["confidence_issues"]


def test_greenfield_falls_back_to_auto_archetype_when_inventory_blank(tmp_path: Path) -> None:
    """A2: greenfield mode with blank inventory archetype must NOT silently
    coerce to fastapi_api — that forces the project into a FastAPI shape it may
    not be (CLI/lib/data-pipeline). Fall back to "auto" so downstream can stay
    archetype-neutral. Existing-mode keeps the legacy fastapi_api default for
    back-compat."""
    intake = build_prd_intake(
        "Build a CLI utility that lists items.\nsource of truth: filesystem",
        feature="cli-list",
    )
    # Hand-craft an inventory with a blank archetype (simulates project that
    # was init-scaffolded as cli_tool but the archetype string did not survive).
    inventory = {
        "project_root": str(tmp_path),
        "mode": "greenfield",
        "archetype": "",
        "capabilities": [],
        "surfaces": [],
        "project_layout": {"code_roots": []},
    }

    plan_greenfield = build_architecture_plan(
        project_root=tmp_path,
        prd_intake=intake,
        repo_inventory=inventory,
        planning_mode="greenfield",
    )
    plan_existing = build_architecture_plan(
        project_root=tmp_path,
        prd_intake=intake,
        repo_inventory=inventory,
        planning_mode="existing",
    )

    assert plan_greenfield["archetype"] == "auto", (
        "greenfield + blank inventory archetype must default to 'auto', not "
        f"silently coerce to fastapi_api; got {plan_greenfield['archetype']!r}"
    )
    assert plan_existing["archetype"] == "fastapi_api", (
        "existing-mode behavior must remain fastapi_api default (BC); got "
        f"{plan_existing['archetype']!r}"
    )


def test_task_graph_prefers_existing_app_src_frontend_entry_and_colocated_test(tmp_path: Path) -> None:
    (tmp_path / "backend" / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "app" / "main.py").write_text("def handler():\n    return 'ok'\n", encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    (tmp_path / "app" / "src" / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "src" / "main.tsx").write_text("export function bootstrap() { return null; }\n", encoding="utf-8")
    (tmp_path / "app" / "src" / "app" / "LegacyApp.test.tsx").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "app" / "package.json").write_text(
        json.dumps({"name": "demo-app", "private": True, "scripts": {"test": "vitest run"}}),
        encoding="utf-8",
    )
    intake = build_prd_intake(
        "\n".join(
            [
                "Return backend data and show it in frontend.",
                "source of truth: db.widgets",
                "layer: service route frontend",
            ]
        ),
        feature="app-src-frontend-demo",
    )

    inventory = build_repo_inventory(
        project_root=tmp_path,
        archetype="fullstack_fastapi_react",
        capabilities=[],
        mode="existing",
    )
    plan = build_architecture_plan(
        project_root=tmp_path,
        prd_intake=intake,
        repo_inventory=inventory,
        planning_mode="existing",
    )
    graph = build_task_graph(
        intake,
        project_root=tmp_path,
        repo_inventory=inventory,
        architecture_plan=plan,
        planning_mode="existing",
    )

    frontend_task = next(task for task in graph["tasks"] if task["layer_owner"] == "frontend")
    assert frontend_task["core_files"][0] == "app/src/main.tsx"
    assert "app/src/app/LegacyApp.test.tsx" in frontend_task["core_files"]
    assert frontend_task["executability"]["status"] == "PASS"
    assert graph["executability"]["status"] == "PASS"


def test_task_graph_util_layer_prefers_existing_app_src_shared_utility(tmp_path: Path) -> None:
    (tmp_path / "app" / "src" / "shared" / "api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "src" / "shared" / "api" / "httpClient.ts").write_text(
        "export const httpClient = {};\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "src" / "shared" / "api" / "httpClient.test.ts").write_text(
        "export {};\n",
        encoding="utf-8",
    )
    intake = build_prd_intake(
        "\n".join(
            [
                "Improve shared helper stability.",
                "source of truth: db.widgets",
                "layer: util",
            ]
        ),
        feature="util-compat-demo",
    )
    graph = build_task_graph(
        intake,
        project_root=tmp_path,
        architecture_plan={"recommended_layers": ["util"], "surfaces": [{"name": "frontend"}]},
        planning_mode="existing",
    )

    util_task = next(task for task in graph["tasks"] if task["layer_owner"] == "util")
    assert util_task["core_files"][0] == "app/src/shared/api/httpClient.ts"
    assert "app/src/shared/api/httpClient.test.ts" in util_task["core_files"]
    assert util_task["executability"]["status"] == "PASS"
    assert graph["executability"]["status"] == "PASS"


def test_task_graph_util_layer_still_fails_when_no_real_source_file_exists(tmp_path: Path) -> None:
    intake = build_prd_intake(
        "\n".join(
            [
                "Improve shared helper stability.",
                "source of truth: db.widgets",
                "layer: util",
            ]
        ),
        feature="util-fake-layout-demo",
    )
    graph = build_task_graph(
        intake,
        project_root=tmp_path,
        architecture_plan={"recommended_layers": ["util"], "surfaces": [{"name": "frontend"}]},
        planning_mode="existing",
    )
    errors = validate_task_graph(graph)

    assert graph["executability"]["status"] == "FAIL"
    assert any("util core file does not exist in project layout" in item for item in errors)


def test_task_graph_service_layer_supports_usecase_style_source_with_strict_executability(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "demo-node", "private": True}), encoding="utf-8")
    (tmp_path / "app" / "src" / "features" / "orders").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "src" / "features" / "orders" / "orderUsecase.ts").write_text(
        "export const orderUsecase = () => null;\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "src" / "features" / "orders" / "orderUsecase.test.ts").write_text(
        "export {};\n",
        encoding="utf-8",
    )

    intake = build_prd_intake(
        "\n".join(
            [
                "Implement order orchestration path.",
                "source of truth: db.orders",
                "layer: service",
            ]
        ),
        feature="service-usecase-compat-demo",
    )
    graph = build_task_graph(
        intake,
        project_root=tmp_path,
        architecture_plan={"recommended_layers": ["service"], "surfaces": [{"name": "frontend"}]},
        planning_mode="existing",
    )

    service_task = next(task for task in graph["tasks"] if task["layer_owner"] == "service")
    assert service_task["core_files"][0] == "app/src/features/orders/orderUsecase.ts"
    # Source file exists; test candidate may be an unresolved LAYER_CORE_CANDIDATES
    # entry, which correctly triggers WARN (not FAIL).
    assert service_task["executability"]["status"] in ("PASS", "WARN")
    assert graph["executability"]["status"] in ("PASS", "WARN")


def test_task_graph_service_layer_usecase_style_still_fails_when_source_is_missing(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "demo-node", "private": True}), encoding="utf-8")
    (tmp_path / "app" / "src" / "features" / "orders").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "src" / "features" / "orders" / "index.ts").write_text("export {};\n", encoding="utf-8")

    intake = build_prd_intake(
        "\n".join(
            [
                "Implement order orchestration path.",
                "source of truth: db.orders",
                "layer: service",
            ]
        ),
        feature="service-usecase-fail-demo",
    )
    graph = build_task_graph(
        intake,
        project_root=tmp_path,
        architecture_plan={"recommended_layers": ["service"], "surfaces": [{"name": "frontend"}]},
        planning_mode="existing",
    )
    errors = validate_task_graph(graph)

    assert graph["executability"]["status"] == "FAIL"
    assert any("service core file does not exist in project layout" in item for item in errors)


def test_task_graph_adjacent_layers_support_app_src_layout_with_strict_executability(tmp_path: Path) -> None:
    (tmp_path / "app" / "src" / "shared" / "api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "src" / "shared" / "session").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "src" / "features" / "auth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "src" / "main.tsx").write_text("export const mainEntry = true;\n", encoding="utf-8")
    (tmp_path / "app" / "src" / "shared" / "api" / "runtimeClient.ts").write_text(
        "export const runtimeClient = {};\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "src" / "features" / "auth" / "useAuthSessionController.ts").write_text(
        "export const useAuthSessionController = () => null;\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "src" / "shared" / "session" / "sessionStore.ts").write_text(
        "export const sessionStore = {};\n",
        encoding="utf-8",
    )

    intake = build_prd_intake(
        "\n".join(
            [
                "Stabilize app data flow with clean boundaries.",
                "source of truth: db.widgets",
                "layer: repository service route model",
            ]
        ),
        feature="adjacent-layer-compat-demo",
    )
    graph = build_task_graph(
        intake,
        project_root=tmp_path,
        architecture_plan={
            "archetype": "react_web",
            "recommended_layers": ["repository", "service", "route", "model"],
            "surfaces": [{"name": "frontend"}],
        },
        planning_mode="existing",
    )

    layer_to_source = {task["layer_owner"]: task["core_files"][0] for task in graph["tasks"]}
    assert layer_to_source["repository"] == "app/src/shared/api/runtimeClient.ts"
    assert layer_to_source["service"] == "app/src/features/auth/useAuthSessionController.ts"
    assert layer_to_source["route"] == "app/src/main.tsx"
    assert layer_to_source["model"] == "app/src/shared/session/sessionStore.ts"
    assert graph["executability"]["status"] == "PASS"
    assert not validate_task_graph(graph)


def test_task_graph_adjacent_layers_still_fail_when_app_src_sources_are_missing(tmp_path: Path) -> None:
    intake = build_prd_intake(
        "\n".join(
            [
                "Stabilize app data flow with clean boundaries.",
                "source of truth: db.widgets",
                "layer: repository service route model",
            ]
        ),
        feature="adjacent-layer-fail-demo",
    )
    graph = build_task_graph(
        intake,
        project_root=tmp_path,
        architecture_plan={
            "archetype": "react_web",
            "recommended_layers": ["repository", "service", "route", "model"],
            "surfaces": [{"name": "frontend"}],
        },
        planning_mode="existing",
    )
    errors = validate_task_graph(graph)

    assert graph["executability"]["status"] == "FAIL"
    assert any("repository core file does not exist in project layout" in item for item in errors)
    assert any("service core file does not exist in project layout" in item for item in errors)
    assert any("route core file does not exist in project layout" in item for item in errors)
    assert any("model core file does not exist in project layout" in item for item in errors)


def test_task_graph_validation_fails_when_project_layout_would_generate_fake_source_files(tmp_path: Path) -> None:
    intake = build_prd_intake(
        "\n".join(
            [
                "Enable order history query.",
                "source of truth: db.orders",
                "layer: repository service",
            ]
        ),
        feature="flat-layout-demo",
    )
    graph = build_task_graph(intake, project_root=tmp_path)
    errors = validate_task_graph(graph)

    assert graph["executability"]["status"] == "FAIL"
    assert errors
    assert any("core file does not exist in project layout" in item for item in errors)


def test_task_graph_validation_fails_for_too_many_core_files() -> None:
    payload = {
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "demo",
                "depends_on": [],
                "core_files": ["a.py", "b.py", "c.py", "d.py"],
                "layer_owner": "service",
                "invariants": ["x"],
                "test_proof": "run tests",
            }
        ]
    }
    errors = validate_task_graph(payload)
    assert errors
    assert any("core_files exceeds 3" in item for item in errors)


def test_task_card_build_and_validate() -> None:
    intake = build_prd_intake(_sample_prd(), feature="contract-demo")
    graph = build_task_graph(intake)
    card = build_task_card(graph, "T1")
    errors = validate_task_card(card)

    assert not errors
    assert card["task_id"] == "T1"
    assert card["files_to_change"]
    markdown = render_task_card_markdown(card)
    assert "# Task Card" in markdown
    assert "## Files To Change" in markdown


def test_task_card_validate_existing_mode_caps_at_3() -> None:
    """A4: existing-mode keeps the legacy 3-file cap on files_to_change."""
    card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T1",
        "why_this_layer": "x",
        "files_to_change": ["a.py", "b.py", "c.py", "d.py"],  # 4 files
        "invariants": ["preserve api"],
        "test_plan": "pytest",
        "requires": [],
    }
    errors = validate_task_card(card, planning_mode="existing")
    assert any("exceeds 3" in e for e in errors), errors


def test_task_card_validate_greenfield_mode_allows_5() -> None:
    """A4: greenfield bootstrap tasks may carry a full vertical slice
    (schema+model+repo+service+test) — cap is 5, not 3."""
    card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T1",
        "why_this_layer": "x",
        "files_to_change": ["schema.py", "model.py", "repo.py", "service.py", "test.py"],
        "invariants": ["preserve api"],
        "test_plan": "pytest",
        "requires": [],
    }
    errors = validate_task_card(card, planning_mode="greenfield")
    assert not errors, f"greenfield 5-file task must pass validator; got {errors}"


def test_task_card_validate_greenfield_mode_still_rejects_6() -> None:
    """A4: greenfield cap is 5 — not unbounded. 6+ files signals a task that
    is bundling cross-surface scope and should be split."""
    card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T1",
        "why_this_layer": "x",
        "files_to_change": ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"],  # 6 files
        "invariants": ["preserve api"],
        "test_plan": "pytest",
        "requires": [],
    }
    errors = validate_task_card(card, planning_mode="greenfield")
    assert any("exceeds 5" in e for e in errors), errors


def test_task_card_build_greenfield_does_not_truncate_below_5() -> None:
    """A4: build_task_card reads task_graph.planning_mode and uses 5-cap for
    greenfield, not the legacy 3-cap that silently dropped files."""
    graph = {
        "planning_mode": "greenfield",
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "bootstrap",
                "depends_on": [],
                "core_files": ["schema.py", "model.py", "repo.py", "service.py", "test_service.py"],
                "new_files": ["schema.py", "model.py", "repo.py", "service.py", "test_service.py"],
                "verify_cmd": "pytest -q",
                "layer_owner": "service",
                "invariants": ["preserve api"],
                "test_proof": "pytest",
            }
        ],
    }
    card = build_task_card(graph, "T1")
    assert len(card["files_to_change"]) == 5, (
        f"greenfield build must preserve all 5 vertical-slice files; got "
        f"{card['files_to_change']!r}"
    )


def test_task_card_build_existing_mode_still_truncates_to_3() -> None:
    """A4 BC: existing-mode (default when planning_mode missing) preserves the
    legacy 3-cap on build, so refactors of existing projects stay scoped."""
    graph = {
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "refactor",
                "depends_on": [],
                "core_files": ["a.py", "b.py", "c.py", "d.py", "e.py"],
                "verify_cmd": "pytest -q",
                "layer_owner": "service",
                "invariants": ["preserve api"],
                "test_proof": "pytest",
            }
        ],
    }
    card = build_task_card(graph, "T1")
    assert len(card["files_to_change"]) == 3


def test_task_card_preserves_new_files_and_verify_cmd() -> None:
    """Regression lock for the handoff-field-drop bug fixed 2026-04-23.

    Prior to this fix, ``build_task_card`` silently dropped ``new_files`` and
    ``verify_cmd`` from the planner's task-graph output, leaving executor and
    preflight with strictly less information than the planner produced.
    """
    graph = {
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "demo",
                "depends_on": [],
                "core_files": ["src/existing.py", "src/to_create.py", "tests/test_new.py"],
                "new_files": ["src/to_create.py", "tests/test_new.py"],
                "verify_cmd": "python -m pytest tests/test_new.py -q",
                "layer_owner": "service",
                "invariants": ["preserve api contract"],
                "test_proof": "run scoped tests",
            }
        ]
    }
    card = build_task_card(graph, "T1")
    assert card["new_files"] == ["src/to_create.py", "tests/test_new.py"]
    assert card["verify_cmd"] == "python -m pytest tests/test_new.py -q"


def test_task_card_new_files_filtered_to_files_to_change_subset() -> None:
    """``new_files`` must always be a subset of ``files_to_change``.

    Any planner-declared new file outside files_to_change (after the :3 cap)
    is silently dropped, not raised — upstream planning_agent already enforces
    the subset invariant at graph-validation time. This test pins the filter.
    """
    graph = {
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "demo",
                "depends_on": [],
                "core_files": ["src/a.py", "src/b.py", "src/c.py"],
                "new_files": ["src/b.py", "src/outside_scope.py"],
                "layer_owner": "service",
                "invariants": ["x"],
                "test_proof": "run tests",
            }
        ]
    }
    card = build_task_card(graph, "T1")
    assert card["new_files"] == ["src/b.py"]
    assert "verify_cmd" not in card  # absent when planner did not declare one


def test_task_card_v1_1_preserves_rich_contract_fields(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TASK_CARD_V1_1", "1")
    graph = {
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "demo",
                "depends_on": [],
                "core_files": ["src/service.py", "tests/test_service.py"],
                "new_files": [],
                "verify_cmd": "python -m pytest tests/test_service.py -q",
                "layer_owner": "service",
                "invariants": ["preserve ranking algorithm"],
                "test_proof": "run scoped tests",
                "target_symbols": [{"file": "src/service.py", "kind": "method", "class": "Svc", "name": "run"}],
                "read_only_symbols": [{"file": "src/service.py", "kind": "method", "class": "Svc", "name": "_rank"}],
                "do_not_change": ["candidate pool size"],
                "read_only_files": ["src/db_schema.py"],
                "behavior_changes": [{"id": "display_count", "from": "5", "to": "4", "scope": "final display only"}],
                "allowed_test_mutations": [
                    {
                        "file": "tests/test_service.py",
                        "match_kind": "literal_assert",
                        "old_pattern": "assert len(items) == 5",
                        "new_pattern": "assert len(items) == 4",
                        "behavior_change_id": "display_count",
                    }
                ],
                "related_existing_tests": ["tests/test_existing.py"],
                "review_focus": ["confirm ranking did not change"],
                "freshness": {
                    "scouted_at_commit": "abc123",
                    "source_file_hashes": [{"path": "src/service.py", "sha256": "deadbeef", "line_count": 100}],
                },
            }
        ]
    }
    card = build_task_card(graph, "T1")
    assert card["schema_version"] == "contract_first.task_card.v1.1"
    assert card["target_symbols"][0]["name"] == "run"
    assert card["read_only_files"] == ["src/db_schema.py"]
    assert card["allowed_test_mutations"][0]["match_kind"] == "literal_assert"
    assert card["behavior_changes"][0]["id"] == "display_count"
    assert card["freshness"]["scouted_at_commit"] == "abc123"


def test_contract_first_schema_files_exist() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    schema_dir = repo_root / "src" / "kodawari" / "schemas" / "contract_first"
    expected = {
        "prd_intake.schema.json",
        "repo_inventory.schema.json",
        "architecture_plan.schema.json",
        "task_graph.schema.json",
        "task_card.schema.json",
        "compliance_report.schema.json",
    }
    discovered = {item.name for item in schema_dir.glob("*.json")}
    assert expected.issubset(discovered)
    for name in expected:
        payload = json.loads((schema_dir / name).read_text(encoding="utf-8"))
        assert payload["$schema"] == "http://json-schema.org/draft-07/schema#"
    prd_intake_schema = json.loads((schema_dir / "prd_intake.schema.json").read_text(encoding="utf-8"))
    assert "schema_version" in prd_intake_schema["required"]
    assert prd_intake_schema["properties"]["schema_version"]["const"] == "contract_first.prd_intake.v1"
    task_card_schema = json.loads((schema_dir / "task_card.schema.json").read_text(encoding="utf-8"))
    assert "schema_version" in task_card_schema["required"]
    assert set(task_card_schema["properties"]["schema_version"]["enum"]) == {
        "contract_first.task_card.v1",
        "contract_first.task_card.v1.1",
    }
    task_graph_schema = json.loads((schema_dir / "task_graph.schema.json").read_text(encoding="utf-8"))
    assert "schema_version" in task_graph_schema["required"]
    assert task_graph_schema["properties"]["schema_version"]["const"] == "contract_first.task_graph.v1"
    assert "executability" in task_graph_schema["properties"]
    assert "executability" in task_graph_schema["properties"]["tasks"]["items"]["properties"]
    assert "coverage_hints" in prd_intake_schema["properties"]
    assert "source_of_truth_canonical" in prd_intake_schema["properties"]
    assert "coverage_hints" in task_graph_schema["properties"]
    assert "boundary_debt" in task_graph_schema["properties"]
    boundary_item_schema = task_graph_schema["properties"]["boundary_debt"]["properties"]["items"]["items"]["properties"]
    assert "severity" in boundary_item_schema
    assert "recommended_split" in boundary_item_schema
    repo_inventory_schema = json.loads((schema_dir / "repo_inventory.schema.json").read_text(encoding="utf-8"))
    assert repo_inventory_schema["properties"]["schema_version"]["const"] == "contract_first.repo_inventory.v1"
    architecture_plan_schema = json.loads((schema_dir / "architecture_plan.schema.json").read_text(encoding="utf-8"))
    assert architecture_plan_schema["properties"]["schema_version"]["const"] == "contract_first.architecture_plan.v1"
    compliance_schema = json.loads((schema_dir / "compliance_report.schema.json").read_text(encoding="utf-8"))
    compliance_checks = set(compliance_schema["properties"]["checks"]["items"]["properties"]["check_name"]["enum"])
    assert {"duplication", "import_rules", "domain_source_of_truth"} <= compliance_checks
