"""Turn test cases into OTLP/HTTP JSON and send them."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Dict, List, Optional, Tuple

from parsers import TestCase

SCOPE = {"name": "test-results-otel", "version": "1.0.0"}

# OTLP severity numbers: INFO, WARN, ERROR.
SEVERITY = {"pass": (9, "INFO"), "skipped": (13, "WARN"), "fail": (17, "ERROR")}
LABEL = {"pass": "PASS", "skipped": "SKIP", "fail": "FAIL"}


def _value(value) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def attributes(values: Dict[str, object]) -> List[dict]:
    return [{"key": k, "value": _value(v)} for k, v in values.items() if v not in (None, "")]


def build_logs(cases: List[TestCase], resource: Dict[str, object], suite: str, now_nano: int) -> dict:
    records = []
    for case in cases:
        number, text = SEVERITY[case.status]
        body = f"{LABEL[case.status]} {case.name}"
        if case.message:
            body += f": {case.message.splitlines()[0]}"
        records.append({
            "timeUnixNano": str(case.start_unix_nano or now_nano),
            "observedTimeUnixNano": str(now_nano),
            "severityNumber": number,
            "severityText": text,
            "body": {"stringValue": body},
            "attributes": attributes({
                # Names follow the OpenTelemetry test.* semantic conventions where they exist.
                "test.suite.name": suite or case.suite,
                "test.case.name": case.name,
                "test.case.result.status": case.status,
                "test.class.name": case.classname,
                "test.group.name": case.suite,
                "test.duration.ms": round(case.duration_s * 1000, 1),
                "test.message": case.message,
                **case.attributes,
            }),
        })
    return {"resourceLogs": [{
        "resource": {"attributes": attributes(resource)},
        "scopeLogs": [{"scope": SCOPE, "logRecords": records}],
    }]}


def build_metrics(cases: List[TestCase], resource: Dict[str, object], suite: str,
                  branch: str, now_nano: int) -> dict:
    counts = Counter(case.status for case in cases)
    common = {"test.suite.name": suite, "vcs.ref.head.name": branch}
    points = [
        {"timeUnixNano": str(now_nano), "asInt": str(counts.get(status, 0)),
         "attributes": attributes({**common, "test.case.result.status": status})}
        for status in ("pass", "fail", "skipped")
    ]
    duration = sum(case.duration_s for case in cases)
    run_status = "fail" if counts.get("fail") else "pass"
    return {"resourceMetrics": [{
        "resource": {"attributes": attributes(resource)},
        "scopeMetrics": [{"scope": SCOPE, "metrics": [
            {"name": "test.run.cases", "unit": "{test}",
             "description": "Test cases in this run, by result", "gauge": {"dataPoints": points}},
            {"name": "test.run.duration", "unit": "s",
             "description": "Sum of test case durations in this run",
             "gauge": {"dataPoints": [{"timeUnixNano": str(now_nano), "asDouble": round(duration, 3),
                                       "attributes": attributes({**common, "test.suite.run.status": run_status})}]}},
        ]}],
    }]}


def parse_headers(text: str) -> Dict[str, str]:
    """OTEL_EXPORTER_OTLP_HEADERS format: key1=value1,key2=value2 (values URL-encoded)."""
    headers = {}
    for pair in (text or "").split(","):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        headers[urllib.parse.unquote(key.strip())] = urllib.parse.unquote(value.strip())
    return headers


def post(endpoint: str, signal: str, payload: dict, headers: Dict[str, str],
         timeout: float = 15, retries: int = 2) -> Tuple[int, str]:
    url = endpoint.rstrip("/") + f"/v1/{signal}"
    body = json.dumps(payload).encode()
    request_headers = {"Content-Type": "application/json", **headers}
    last: Optional[Tuple[int, str]] = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode(errors="replace")
        except urllib.error.HTTPError as error:
            last = (error.code, error.read().decode(errors="replace"))
            if error.code != 429 and error.code < 500:
                return last
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = (0, str(error))
        if attempt < retries:
            time.sleep(2 ** attempt)
    return last
