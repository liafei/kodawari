from __future__ import annotations

import json
from pathlib import Path

from kodawari.source_of_truth import load_domain_source_of_truth


def test_load_domain_source_of_truth_reads_json_backed_yaml_file(tmp_path: Path) -> None:
    ownership_path = tmp_path / "module_ownership.yaml"
    ownership_path.write_text(
        json.dumps(
            {
                "modules": {
                    "feed_service": {
                        "owner": "backend",
                        "path": "app/feed_service.py",
                        "public_api": ["build_feed"],
                        "description": "Feed assembly",
                        "forbidden_imports": [],
                        "canonical_for": ["feed assembly logic", "homepage feed"],
                    },
                    "scoring_service": {
                        "owner": "backend",
                        "path": "app/scoring_service.py",
                        "public_api": ["calculate_rank"],
                        "description": "Ranking rules",
                        "forbidden_imports": [],
                        "canonical_for": ["ranking rules"],
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mapping = load_domain_source_of_truth(ownership_path)

    assert mapping["feed assembly logic"] == "feed_service"
    assert mapping["homepage feed"] == "feed_service"
    assert mapping["ranking rules"] == "scoring_service"


def test_load_domain_source_of_truth_returns_empty_mapping_for_missing_file(tmp_path: Path) -> None:
    assert load_domain_source_of_truth(tmp_path / "missing.yaml") == {}
