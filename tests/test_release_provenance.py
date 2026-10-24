"""A publisher repair may reuse verified binaries, never bypass their gates."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from tools.release_provenance import REQUIRED_ARTIFACTS, REQUIRED_JOBS, validate_build_run

REPOSITORY = "example/repo"
REVISION = "a" * 40
RUN_ID = "42"


def source_fixture():
    run = {
        "path": ".github/workflows/release.yml",
        "event": "push",
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        "head_sha": REVISION,
        "status": "completed",
        "conclusion": "failure",
    }
    jobs = [{"name": name, "conclusion": "success"} for name in sorted(REQUIRED_JOBS)]
    jobs.append({"name": "校验全部附件后公开 GitHub Release", "conclusion": "failure"})
    artifacts = [
        {
            "name": name,
            "id": index + 1,
            "expired": False,
            "size_in_bytes": 100,
            "digest": "sha256:" + "a" * 64,
            "workflow_run": {"id": 42, "head_sha": REVISION},
        }
        for index, name in enumerate(sorted(REQUIRED_ARTIFACTS))
    ]
    return run, jobs, artifacts


def run_check(run, jobs, artifacts, current_run_id=""):
    def api(*args):
        endpoint = args[1]
        if "/commits/" in endpoint:
            return REVISION
        if "/jobs?" in endpoint:
            assert args[-2:] == ("--paginate", "--slurp")
            return json.dumps([{"jobs": jobs[:5]}, {"jobs": jobs[5:]}])
        if "/artifacts?" in endpoint:
            return json.dumps([{"artifacts": artifacts[:1]}, {"artifacts": artifacts[1:]}])
        return json.dumps(run)

    return validate_build_run(REPOSITORY, RUN_ID, "v1.10.0", current_run_id=current_run_id, api=api)


def test_only_publication_failure_can_reuse_original_verified_artifacts():
    result = run_check(*source_fixture())
    assert result == {"revision": REVISION, "artifact_ids": "1,2,3"}


@pytest.mark.parametrize("gate", sorted(REQUIRED_JOBS))
def test_every_source_build_container_and_space_gate_is_required(gate):
    run, jobs, artifacts = source_fixture()
    next(job for job in jobs if job["name"] == gate)["conclusion"] = "failure"
    with pytest.raises(ValueError, match="did not succeed"):
        run_check(run, jobs, artifacts)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "new_failed_gate"])
def test_missing_duplicate_and_new_failed_gates_are_rejected(mutation):
    run, jobs, artifacts = source_fixture()
    if mutation == "missing":
        jobs.pop(0)
    elif mutation == "duplicate":
        jobs.append(deepcopy(jobs[0]))
    else:
        jobs.append({"name": "new quality gate", "conclusion": "failure"})
    with pytest.raises(ValueError):
        run_check(run, jobs, artifacts)


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("path", ".github/workflows/build.yml"),
        ("event", "pull_request"),
        ("repository", {"full_name": "foreign/repo"}),
        ("head_repository", {"full_name": "fork/repo"}),
        ("conclusion", "cancelled"),
    ],
)
def test_foreign_source_and_cancelled_runs_cannot_publish(field, value):
    run, jobs, artifacts = source_fixture()
    run[field] = value
    with pytest.raises(ValueError):
        run_check(run, jobs, artifacts)


def test_only_current_parent_run_may_still_be_in_progress():
    run, jobs, artifacts = source_fixture()
    run.update(status="in_progress", conclusion=None)
    jobs[-1].update(name="校验全部附件后公开 GitHub Release / 发布已验收产物", conclusion=None)
    with pytest.raises(ValueError, match="must finish"):
        run_check(run, jobs, artifacts)
    assert run_check(run, jobs, artifacts, current_run_id=RUN_ID)["revision"] == REVISION


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "report", "expired", "sha", "run", "digest"]
)
def test_only_complete_immutable_artifacts_from_that_run_are_accepted(mutation):
    run, jobs, artifacts = source_fixture()
    if mutation == "missing":
        artifacts.pop()
    elif mutation == "duplicate":
        artifacts.append(deepcopy(artifacts[0]))
    elif mutation == "report":
        artifacts[0]["name"] = "MusicToMidi-TestReport"
    elif mutation == "expired":
        artifacts[0]["expired"] = True
    elif mutation == "sha":
        artifacts[0]["workflow_run"]["head_sha"] = "b" * 40
    elif mutation == "run":
        artifacts[0]["workflow_run"]["id"] = 43
    else:
        artifacts[0]["digest"] = ""
    with pytest.raises(ValueError):
        run_check(run, jobs, artifacts)


def test_publication_workflow_checks_source_before_downloading_and_uses_original_notes():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release-publish.yml").read_text("utf-8"))
    steps = workflow["jobs"]["publish"]["steps"]
    assert "tools.release_provenance" in steps[1]["run"]
    assert steps[2]["with"]["ref"] == "${{ steps.source.outputs.revision }}"
    assert steps[3]["with"]["artifact-ids"] == "${{ steps.source.outputs.artifact_ids }}"
    assert '"$SOURCE_REVISION"' in steps[4]["run"]
    assert "release-source/docs/releases/" in steps[4]["run"]
    assert not any("continue-on-error" in step for step in steps)
