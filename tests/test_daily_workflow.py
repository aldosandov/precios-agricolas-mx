"""The daily job's workflow file (ALD-24).

A workflow only fails visibly once a day, and a silent one is worse: the app
would keep showing stale prices while the job reports green. So the parts that
would fail quietly are pinned here — that the sweep's exit code reaches the
job, that the load still runs when one market fails, and that no long-lived
credential sneaks back in.

Reads the YAML as text; the suite has no YAML parser and does not need one.
"""

import re
from pathlib import Path

import pytest

WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "daily.yml"
)


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


# --- when it runs ---


def test_it_runs_on_weekdays_only(workflow):
    """The source publishes on business days; a weekend run is 98 requests
    against a government server for nothing."""
    assert re.search(r'cron:\s*"0 23 \* \* 1-5"', workflow)


def test_it_can_be_triggered_by_hand(workflow):
    assert "workflow_dispatch:" in workflow


def test_two_runs_never_overlap(workflow):
    """Two sweeps writing the same partitions would race on the same files."""
    assert "concurrency:" in workflow


# --- what it does ---


def test_it_runs_the_sweep_and_then_the_load(workflow):
    sweep = workflow.index("scraper.spiders.daily")
    load = workflow.index("transform.load")

    assert sweep < load


def test_a_failed_market_does_not_skip_the_load(workflow):
    """The sweep exits non-zero if any market failed. Whatever the other 48
    captured still has to reach BigQuery."""
    assert "continue-on-error: true" in workflow


def test_the_load_is_skipped_when_the_window_had_no_prices(workflow):
    """A holiday is not a failure, and the loader exits non-zero on an empty
    directory. Missing data is the alert's job, not this step's exit code."""
    assert "hashFiles('out/raw/**/*.ndjson') != ''" in workflow


def test_the_run_still_ends_red_when_the_sweep_failed(workflow):
    """Otherwise a schema change repeats for days behind a green check."""
    assert re.search(r"steps\.\w+\.outcome\s*==\s*'failure'", workflow)


# --- how it authenticates ---


def test_it_asks_for_the_oidc_token(workflow):
    assert re.search(r"id-token:\s*write", workflow)


def test_it_federates_instead_of_carrying_a_key(workflow):
    assert "workload_identity_provider:" in workflow
    assert "credentials_json" not in workflow
    assert "GCP_SA_KEY" not in workflow


def test_the_federation_targets_this_projects_service_account(workflow):
    assert "ingesta-diaria@precios-agricolas-mx.iam.gserviceaccount.com" in workflow
    assert "workloadIdentityPools/github/providers/github" in workflow
