"""Entry point for the action: find results files, parse them, send them.

Settings come from environment variables that action.yml fills in from the
inputs. It never fails the job unless fail-on-error is true.
"""

import glob
import json
import os
import sys
import time
from typing import Dict, List

import otlp
from parsers import TestCase, parse


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def find_files(patterns: str) -> List[str]:
    files = []
    for pattern in patterns.splitlines():
        pattern = pattern.strip()
        if pattern:
            files.extend(sorted(glob.glob(pattern, recursive=True)))
    return list(dict.fromkeys(files))


def resource_attributes(service_name: str) -> Dict[str, object]:
    env = os.environ.get
    server = env("GITHUB_SERVER_URL", "https://github.com")
    repository = env("GITHUB_REPOSITORY", "")
    run_id = env("GITHUB_RUN_ID", "")
    branch = env("GITHUB_HEAD_REF") or env("GITHUB_REF_NAME", "")
    return {
        "service.name": service_name,
        "service.namespace": "ci",
        "deployment.environment.name": "ci",
        "cicd.pipeline.name": env("GITHUB_WORKFLOW", ""),
        "cicd.pipeline.run.id": run_id,
        "cicd.pipeline.run.url.full": f"{server}/{repository}/actions/runs/{run_id}" if run_id else "",
        "cicd.pipeline.run.attempt": env("GITHUB_RUN_ATTEMPT", ""),
        "cicd.pipeline.task.name": env("GITHUB_JOB", ""),
        "cicd.pipeline.trigger": env("GITHUB_EVENT_NAME", ""),
        "vcs.repository.url.full": f"{server}/{repository}" if repository else "",
        "vcs.ref.head.name": branch,
        "vcs.ref.head.revision": env("GITHUB_SHA", ""),
        "os.type": env("RUNNER_OS", "").lower(),
    }


def summarize(cases: List[TestCase]) -> str:
    counts = {s: sum(1 for c in cases if c.status == s) for s in ("pass", "fail", "skipped")}
    return f"{len(cases)} tests: {counts['pass']} passed, {counts['fail']} failed, {counts['skipped']} skipped"


def write_step_summary(lines: List[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def run() -> int:
    patterns = setting("TRO_RESULTS")
    fmt = setting("TRO_FORMAT", "auto")
    endpoint = setting("TRO_ENDPOINT") or setting("OTEL_EXPORTER_OTLP_ENDPOINT")
    headers = otlp.parse_headers(setting("TRO_HEADERS") or setting("OTEL_EXPORTER_OTLP_HEADERS"))
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    service = setting("TRO_SERVICE_NAME") or repository.split("/")[-1] or "tests"
    suite = setting("TRO_SUITE")
    dry_run = setting("TRO_DRY_RUN", "false").lower() == "true"

    files = find_files(patterns)
    if not files:
        print(f"::warning title=test-results-otel::No results files matched: {patterns!r}")
        return 0

    cases: List[TestCase] = []
    for path in files:
        try:
            found = parse(path, fmt)
        except Exception as error:  # a bad file shouldn't stop the others
            print(f"::warning title=test-results-otel::Couldn't read {path}: {error}")
            continue
        print(f"{path}: {summarize(found)}")
        cases.extend(found)
    if not cases:
        print("::warning title=test-results-otel::The results files held no test cases")
        return 0

    now = time.time_ns()
    resource = resource_attributes(service)
    branch = str(resource["vcs.ref.head.name"])
    payloads = {
        "logs": otlp.build_logs(cases, resource, suite, now),
        "metrics": otlp.build_metrics(cases, resource, suite, branch, now),
    }

    if dry_run:
        out_dir = setting("RUNNER_TEMP", ".")
        for signal, payload in payloads.items():
            out = os.path.join(out_dir, f"test-results-otel-{signal}.json")
            with open(out, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            print(f"Dry run: wrote {signal} to {out}")
        write_step_summary([f"**test-results-otel** (dry run): {summarize(cases)}"])
        return 0

    if not endpoint:
        print("::notice title=test-results-otel::No OTLP endpoint set (secrets missing?), so nothing was sent. "
              f"{summarize(cases)}")
        return 0

    failed = False
    for signal, payload in payloads.items():
        status, body = otlp.post(endpoint, signal, payload, headers)
        if 200 <= status < 300:
            print(f"Sent {signal}: HTTP {status}")
        else:
            failed = True
            print(f"::warning title=test-results-otel::Sending {signal} failed: HTTP {status} {body[:300]}")

    write_step_summary([f"**test-results-otel**: {summarize(cases)}"
                        + (" (sending failed, see the log)" if failed else f", sent to {service}")])
    if failed and setting("TRO_FAIL_ON_ERROR", "false").lower() == "true":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
