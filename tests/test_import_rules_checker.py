from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft7Validator

from kodawari.gate.checker_import_rules import relevant_ownership_context, run_import_rules_checker


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _ownership_payload() -> dict:
    return {
        "modules": {
            "route_module": {
                "owner": "backend",
                "path": "app/routes/feed_route.py",
                "public_api": ["build_feed_route"],
                "description": "Route layer binding",
                "forbidden_imports": ["app.repository.*"],
                "canonical_for": ["feed route binding"],
            },
            "service_module": {
                "owner": "backend",
                "path": "app/services/scoring_service.py",
                "public_api": ["calculate_rank"],
                "description": "Ranking logic",
                "forbidden_imports": [],
                "canonical_for": ["ranking rules"],
            },
        }
    }


def test_module_ownership_schema_accepts_fixture() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "src" / "kodawari" / "schemas" / "module_ownership.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = list(Draft7Validator(schema).iter_errors(_ownership_payload()))
    assert errors == []


def test_import_rules_checker_flags_forbidden_import(tmp_path: Path) -> None:
    ownership_path = tmp_path / "module_ownership.yaml"
    ownership_path.write_text(json.dumps(_ownership_payload(), ensure_ascii=False), encoding="utf-8")
    _write(tmp_path / "app" / "routes" / "feed_route.py", "from app.repository.feed_repo import FeedRepo\n")

    report = run_import_rules_checker(["app/routes/feed_route.py"], tmp_path, ownership_path=ownership_path)

    assert report["status"] == "FAIL"
    assert any(item["rule"] == "import_rules.forbidden_import" for item in report["evidence"])


def test_import_rules_checker_flags_non_public_api_symbol(tmp_path: Path) -> None:
    ownership_path = tmp_path / "module_ownership.yaml"
    ownership_path.write_text(json.dumps(_ownership_payload(), ensure_ascii=False), encoding="utf-8")
    _write(tmp_path / "app" / "routes" / "feed_route.py", "from app.services.scoring_service import _internal_helper\n")

    report = run_import_rules_checker(["app/routes/feed_route.py"], tmp_path, ownership_path=ownership_path)

    assert report["status"] == "FAIL"
    assert any(item["rule"] == "import_rules.non_public_api" for item in report["evidence"])


def test_relevant_ownership_context_selects_changed_modules(tmp_path: Path) -> None:
    ownership_path = tmp_path / "module_ownership.yaml"
    ownership_path.write_text(json.dumps(_ownership_payload(), ensure_ascii=False), encoding="utf-8")

    context = relevant_ownership_context(
        project_root=tmp_path,
        changed_files=["app/services/scoring_service.py"],
        ownership_path=ownership_path,
    )

    assert len(context) == 1
    assert context[0]["module"] == "service_module"
    assert context[0]["public_api"] == ["calculate_rank"]
