"""Guards the CI workflow that gates merges.

The workflow is the only thing standing between a broken API and main, and a
workflow fails silently: delete a step, add ``continue-on-error``, or narrow
the trigger to a branch nobody pushes to, and every pull request turns green
without anything being tested. These tests read the workflow file and assert
the properties that make it a gate.
"""

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_workflow_exists() -> None:
    assert WORKFLOW.exists(), f"missing CI workflow at {WORKFLOW}"


def test_runs_on_push_to_main() -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")

    assert "push:" in contents
    assert "branches: [main]" in contents


def test_runs_on_pull_requests() -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in contents, "a pull_request trigger is required"


def test_cancels_a_superseded_run() -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")

    assert "concurrency:" in contents
    assert "cancel-in-progress: true" in contents


def test_no_step_continues_on_error() -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")

    assert "continue-on-error" not in contents, (
        "a step with continue-on-error cannot fail the run, so the pipeline "
        "would report green while the check it performs is broken"
    )


def test_token_is_read_only() -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:" in contents
    assert "contents: read" in contents


def test_installs_the_runtime_and_the_dev_requirements() -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")

    assert "-r requirements.txt" in contents
    assert "-r requirements-dev.txt" in contents


def test_runs_the_test_suite() -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")

    assert "pytest" in contents


def test_runs_the_linter() -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")

    assert "ruff check" in contents


def test_pins_the_python_version() -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")

    assert "python-version:" in contents
    assert "'3.13'" in contents
