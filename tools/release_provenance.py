"""Select immutable artifacts only after their source release gates succeeded."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from tools.release_assets import gh, validate_identity

REQUIRED_JOBS = {
    "核对发布版本与源码身份",
    "构建 Linux-GPU",
    "构建 Windows-GPU",
    "源码与打包契约 / Run source tests",
    "源码与打包契约 / Windows source and packaging contracts (no portable artifact)",
    "源码与打包契约 / Linux source and packaging contracts (no portable artifact)",
    "容器构建与发行包 / 容器与许可证门禁",
    "容器构建与发行包 / 构建并发布 backend",
    "容器构建与发行包 / 构建并发布 gateway",
    "容器构建与发行包 / 验收公开镜像并组装自托管发行包",
    "同步同一版本到 Space / 同步到 HF Space",
}
REQUIRED_ARTIFACTS = {"MusicToMidi-Windows-GPU", "MusicToMidi-Linux-GPU", "MusicToMidi-Docker"}


def validate_build_run(repository: str, run_id: str, tag: str, *, current_run_id="", api=gh):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Expected a numeric source run ID")
    # Validate the tag before using it in an API path.
    validate_identity(tag, "0" * 40)
    prefix = f"repos/{repository}"
    revision = api("api", f"{prefix}/commits/{tag}", "--jq", ".sha")
    validate_identity(tag, revision)
    run = json.loads(api("api", f"{prefix}/actions/runs/{run_id}"))
    if (
        run.get("path") != ".github/workflows/release.yml"
        or run.get("event") not in {"push", "workflow_dispatch"}
        or run.get("repository", {}).get("full_name") != repository
        or run.get("head_repository", {}).get("full_name") != repository
        or run.get("head_sha") != revision
    ):
        raise ValueError("Source run does not match the repository, release workflow and tag")
    if run.get("status") != "completed" and not (
        run_id == current_run_id and run.get("status") == "in_progress"
    ):
        raise ValueError("Another source run must finish before its artifacts can be published")
    if run.get("status") == "completed" and run.get("conclusion") not in {"success", "failure"}:
        raise ValueError("Source run was cancelled or did not finish normally")
    pages = json.loads(
        api(
            "api",
            f"{prefix}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
            "--paginate",
            "--slurp",
        )
    )
    jobs = [job for page in pages for job in page["jobs"]]
    publication = "校验全部附件后公开 GitHub Release"
    for job in jobs:
        name = job.get("name", "")
        if name == publication or name.startswith(publication + " / "):
            continue
        if job.get("conclusion") != "success":
            raise ValueError(f"Source gate did not succeed: {name}")
    for name in sorted(REQUIRED_JOBS):
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1 or matches[0].get("conclusion") != "success":
            raise ValueError(f"Required source gate did not succeed exactly once: {name}")
    pages = json.loads(
        api(
            "api", f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100", "--paginate", "--slurp"
        )
    )
    artifacts = [
        item
        for page in pages
        for item in page["artifacts"]
        if item.get("name", "").startswith("MusicToMidi-")
    ]
    if (
        len(artifacts) != len(REQUIRED_ARTIFACTS)
        or {item["name"] for item in artifacts} != REQUIRED_ARTIFACTS
    ):
        raise ValueError("Source run must contain exactly the three complete release artifacts")
    for item in artifacts:
        source = item.get("workflow_run", {})
        if (
            item.get("expired") is not False
            or item.get("size_in_bytes", 0) <= 0
            or not isinstance(item.get("id"), int)
            or item["id"] <= 0
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("digest", ""))
            or str(source.get("id")) != run_id
            or source.get("head_sha") != revision
        ):
            raise ValueError(f"Invalid or mismatched source artifact: {item['name']}")
    return {
        "revision": revision,
        "artifact_ids": ",".join(
            str(item["id"]) for item in sorted(artifacts, key=lambda x: x["name"])
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    result = validate_build_run(
        args.repository, args.run_id, args.tag, current_run_id=os.environ.get("GITHUB_RUN_ID", "")
    )
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            for name, value in result.items():
                handle.write(f"{name}={value}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
