from kodawari.autopilot.effort_scoring import score_effort_profile


def test_score_effort_profile_defaults_to_economy_for_small_safe_task() -> None:
    profile = score_effort_profile(
        task_label="T001: tweak docs",
        task_scope="edit docs",
        requirements="update readme wording",
        task_card={"files_to_change": ["README.md"]},
        changed_files=["README.md"],
        prior_failures=0,
    )

    assert profile["schema_version"] == "effort.scoring.v1"
    assert profile["tier"] == "economy"
    assert profile["score"] == 0


def test_score_effort_profile_promotes_to_deep_reasoning_with_compound_risk() -> None:
    profile = score_effort_profile(
        task_label="T220: architecture migration",
        task_scope="security migration for core runtime",
        requirements="introduce architecture migration and parallel worker changes",
        task_card={
            "files_to_change": [
                "src/kodawari/autopilot/engine.py",
                "src/kodawari/autopilot/state.py",
                "src/kodawari/autopilot/local_adapter.py",
                "src/kodawari/autopilot/review_bridge.py",
            ]
        },
        prior_failures=2,
    )

    assert profile["tier"] == "deep_reasoning"
    assert profile["score"] >= 3
    assert "prior_failures_present" in profile["reasons"]
