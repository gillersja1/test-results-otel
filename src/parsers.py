"""Read test results files into one common shape.

Supported: TRX (dotnet test), JUnit XML (Playwright, Newman, Gradle, ...),
k6 summary JSON (handleSummary output) and JMeter JTL (CSV with a header).
"""

import csv
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

MAX_MESSAGE = 2000


@dataclass
class TestCase:
    name: str
    status: str  # "pass", "fail" or "skipped"
    suite: str = ""
    classname: str = ""
    duration_s: float = 0.0
    message: str = ""
    start_unix_nano: Optional[int] = None
    attributes: Dict[str, object] = field(default_factory=dict)


def detect_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".trx":
        return "trx"
    if ext == ".json":
        return "k6"
    if ext in (".jtl", ".csv"):
        return "jmeter"
    if ext == ".xml":
        root = ET.parse(path).getroot()
        return "trx" if _local(root.tag) == "TestRun" else "junit"
    raise ValueError(f"Can't tell the format of {path}; set the format input")


def parse(path: str, fmt: str = "auto") -> List[TestCase]:
    if fmt == "auto":
        fmt = detect_format(path)
    parsers = {"trx": parse_trx, "junit": parse_junit, "k6": parse_k6, "jmeter": parse_jmeter}
    if fmt not in parsers:
        raise ValueError(f"Unknown format '{fmt}' (expected auto, trx, junit, k6 or jmeter)")
    return parsers[fmt](path)


def _local(tag: str) -> str:
    """Tag name without its XML namespace."""
    return tag.rsplit("}", 1)[-1]


def _children(element, name: str):
    return [c for c in element if _local(c.tag) == name]


def _first(element, name: str):
    found = _children(element, name)
    return found[0] if found else None


def _truncate(text: Optional[str]) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def _unix_nano(timestamp: Optional[str]) -> Optional[int]:
    if not timestamp:
        return None
    # Python before 3.11 can't read 7-digit fractions or a trailing Z.
    text = re.sub(r"(\.\d{6})\d+", r"\1", timestamp.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


# --- TRX -------------------------------------------------------------------

TRX_FAIL = {"Failed", "Error", "Timeout", "Aborted"}
TRX_PASS = {"Passed", "PassedButRunAborted", "Warning"}


def _trx_duration(text: Optional[str]) -> float:
    """TRX durations look like 00:00:01.2345678."""
    match = re.match(r"(?:(\d+)\.)?(\d+):(\d+):(\d+(?:\.\d+)?)", text or "")
    if not match:
        return 0.0
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_trx(path: str) -> List[TestCase]:
    root = ET.parse(path).getroot()
    classes = {}
    definitions = _first(root, "TestDefinitions")
    for unit_test in _children(definitions, "UnitTest") if definitions is not None else []:
        method = _first(unit_test, "TestMethod")
        if method is not None:
            classes[unit_test.get("id")] = method.get("className", "")

    cases = []
    results = _first(root, "Results")

    def add(result, parent_id=None):
        inner = _first(result, "InnerResults")
        if inner is not None and len(inner):
            # Data-driven tests: the rows are the real results.
            for child in inner:
                add(child, result.get("testId"))
            return
        outcome = result.get("outcome", "")
        status = "fail" if outcome in TRX_FAIL else "pass" if outcome in TRX_PASS else "skipped"
        message = ""
        output = _first(result, "Output")
        error = _first(output, "ErrorInfo") if output is not None else None
        text = _first(error, "Message") if error is not None else None
        if text is not None:
            message = text.text or ""
        classname = classes.get(result.get("testId")) or classes.get(parent_id, "")
        cases.append(TestCase(
            name=result.get("testName", ""),
            status=status,
            suite=classname.rsplit(".", 1)[-1] if classname else "",
            classname=classname,
            duration_s=_trx_duration(result.get("duration")),
            message=_truncate(message),
            start_unix_nano=_unix_nano(result.get("startTime")),
            attributes={"test.outcome": outcome},
        ))

    for result in _children(results, "UnitTestResult") if results is not None else []:
        add(result)
    return cases


# --- JUnit -----------------------------------------------------------------

def _seconds(text: Optional[str]) -> float:
    try:
        return float((text or "0").replace(",", ""))
    except ValueError:
        return 0.0


def parse_junit(path: str) -> List[TestCase]:
    root = ET.parse(path).getroot()
    cases = []

    def walk(element, suite_name, suite_start):
        if _local(element.tag) == "testsuite":
            suite_name = element.get("name", suite_name)
            suite_start = _unix_nano(element.get("timestamp")) or suite_start
        for child in element:
            tag = _local(child.tag)
            if tag == "testcase":
                cases.append(_junit_case(child, suite_name, suite_start))
            elif tag in ("testsuite", "testsuites"):
                walk(child, suite_name, suite_start)

    if _local(root.tag) == "testcase":
        cases.append(_junit_case(root, "", None))
    else:
        walk(root, "", None)
    return cases


def _junit_case(case, suite_name, suite_start) -> TestCase:
    status, message = "pass", ""
    for child in case:
        tag = _local(child.tag)
        if tag in ("failure", "error"):
            status = "fail"
            # The element's text is usually a stack trace, so only its first line.
            message = child.get("message") or (child.text or "").strip().split("\n")[0] or tag
            break
        if tag == "skipped":
            status = "skipped"
            message = child.get("message") or ""
    return TestCase(
        name=case.get("name", ""),
        status=status,
        suite=suite_name,
        classname=case.get("classname", ""),
        duration_s=_seconds(case.get("time")),
        message=_truncate(message),
        start_unix_nano=suite_start,
    )


# --- k6 --------------------------------------------------------------------

def _values(container):
    """k6 has stored groups and checks as both lists and objects over the years."""
    if isinstance(container, dict):
        return list(container.values())
    return container or []


def parse_k6(path: str) -> List[TestCase]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if "metrics" not in data:
        raise ValueError(f"{path} doesn't look like a k6 summary (no 'metrics')")
    cases = []

    def walk(group, path_name):
        for check in _values(group.get("checks")):
            passes, fails = int(check.get("passes", 0)), int(check.get("fails", 0))
            cases.append(TestCase(
                name=check.get("name", ""),
                status="fail" if fails else "pass",
                suite=path_name or "checks",
                classname="k6.check",
                message=f"{passes} passed, {fails} failed",
                attributes={"k6.check.passes": passes, "k6.check.fails": fails},
            ))
        for child in _values(group.get("groups")):
            walk(child, child.get("path") or child.get("name", ""))

    walk(data.get("root_group", {}), "")

    for metric_name, metric in sorted(data["metrics"].items()):
        for expression, result in sorted((metric.get("thresholds") or {}).items()):
            ok = result.get("ok", True) if isinstance(result, dict) else bool(result)
            cases.append(TestCase(
                name=f"{metric_name}: {expression}",
                status="pass" if ok else "fail",
                suite="thresholds",
                classname="k6.threshold",
                message="" if ok else f"threshold crossed: {metric_name} {expression}",
            ))

    duration = (data.get("state") or {}).get("testRunDurationMs")
    if duration and cases:
        cases[0].attributes["k6.test_run_duration_ms"] = duration
    return cases


# --- JMeter ----------------------------------------------------------------

def parse_jmeter(path: str) -> List[TestCase]:
    with open(path, encoding="utf-8", newline="") as handle:
        first = handle.read(1)
        if first == "<":
            raise ValueError(f"{path} is an XML JTL; save results as CSV with a header row")
        handle.seek(0)
        rows = list(csv.DictReader(handle))
    if rows and "label" not in rows[0]:
        raise ValueError(f"{path} has no header row; set jmeter.save.saveservice.print_field_names=true")

    by_label: Dict[str, list] = {}
    for row in rows:
        by_label.setdefault(row["label"], []).append(row)

    cases = []
    for label, samples in by_label.items():
        elapsed = sorted(int(s.get("elapsed") or 0) for s in samples)
        errors = sum(1 for s in samples if (s.get("success") or "").lower() != "true")
        p95 = elapsed[max(0, int(round(0.95 * len(elapsed))) - 1)]
        avg = sum(elapsed) / len(elapsed)
        start = min(int(s.get("timeStamp") or 0) for s in samples)
        codes = sorted({s.get("responseCode", "") for s in samples if (s.get("success") or "").lower() != "true"})
        cases.append(TestCase(
            name=label,
            status="fail" if errors else "pass",
            suite="samplers",
            classname="jmeter.sampler",
            duration_s=avg / 1000,
            message=f"{errors} of {len(samples)} samples failed (codes: {', '.join(codes)})" if errors else "",
            start_unix_nano=start * 1_000_000 if start else None,
            attributes={
                "jmeter.samples": len(samples),
                "jmeter.errors": errors,
                "jmeter.avg_ms": round(avg, 1),
                "jmeter.p95_ms": p95,
            },
        ))
    return cases
