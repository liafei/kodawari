from kodawari.cli.root_cause_buckets import classify_root_cause_bucket


def test_classify_root_cause_bucket_prefers_env_missing_for_lane_triage() -> None:
    assert (
        classify_root_cause_bucket(
            classification_id="lane.integration_env_missing_fail_closed",
            status="FAIL",
            missing_env=["WORKFLOW_OPUS_API_KEY"],
            failure_messages=["required integration environment is incomplete"],
        )
        == "env_missing"
    )


def test_classify_root_cause_bucket_supports_lane_triage_classification_aliases() -> None:
    assert (
        classify_root_cause_bucket(
            classification_id="integration_env_missing",
            status="BLOCKED",
        )
        == "env_missing"
    )


def test_classify_root_cause_bucket_maps_external_gateway_category() -> None:
    assert (
        classify_root_cause_bucket(
            status="BLOCKED",
            error_categories=["external_gateway"],
            failure_messages=["gateway returned 503 service unavailable"],
        )
        == "external_gateway"
    )


def test_classify_root_cause_bucket_detects_verify_setup_from_message() -> None:
    assert (
        classify_root_cause_bucket(
            status="FAIL",
            failure_messages=["VERIFY setup failed: fixture db_session missing"],
        )
        == "verify_setup"
    )


def test_classify_root_cause_bucket_detects_max_cycles_from_run_outcome() -> None:
    assert (
        classify_root_cause_bucket(
            status="BLOCKED",
            stop_reason="MAX_CYCLES",
            run_outcome="stopped:max_cycles",
        )
        == "max_cycles"
    )


def test_classify_root_cause_bucket_detects_task_blocked_from_blocking_reason() -> None:
    assert (
        classify_root_cause_bucket(
            status="BLOCKED",
            run_outcome="blocked",
            blocking_reason="TASK_BLOCKED",
        )
        == "task_blocked"
    )
