"""Builds ci-test-results.json, the Grafana dashboard for test-results-otel.

The dashboard is generated so it stays valid JSON and the queries share one set
of filters. Run: python dashboards/build_dashboard.py
"""

import json
import os

PROM = {"type": "prometheus", "uid": "${prometheus}"}
LOKI = {"type": "loki", "uid": "${loki}"}

# Known-defect suites assert correct behaviour and are expected to fail, so the
# headline numbers leave them out and they get their own panel. The action's
# own self-test sends sample files that fail on purpose.
GATE = 'test_suite_name!~"known-defects.*|self-test"'
KNOWN = 'test_suite_name=~"known-defects.*"'
PROM_SEL = 'service_namespace="ci", service_name=~"$repo", vcs_ref_head_name=~"$branch"'
LOKI_SEL = '{service_namespace="ci", service_name=~"$repo"} | vcs_ref_head_name=~"$branch"'


def prom(expr, legend="", instant=False, ref="A"):
    target = {"datasource": PROM, "refId": ref, "expr": expr, "legendFormat": legend}
    if instant:
        target.update(instant=True, range=False, format="table")
    return target


def loki(expr, legend="", instant=False, ref="A"):
    target = {"datasource": LOKI, "refId": ref, "expr": expr, "legendFormat": legend,
              "queryType": "instant" if instant else "range"}
    return target


def last(status, suites=GATE):
    """Each run writes one point per series, so the latest point is the latest run."""
    return f'sum(last_over_time(test_run_cases{{{PROM_SEL}, {suites}, test_case_result_status=~"{status}"}}[$__range]))'


panels = []


def add(panel, x, y, w, h):
    panel["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
    panel["id"] = len(panels) + 1
    panels.append(panel)


def stat(title, target, description, unit="none", thresholds=None, decimals=None):
    panel = {
        "type": "stat", "title": title, "description": description,
        "datasource": target["datasource"], "targets": [target],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "textMode": "value"},
        "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "thresholds"},
                                     "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "blue", "value": None}]}},
                        "overrides": []},
    }
    if decimals is not None:
        panel["fieldConfig"]["defaults"]["decimals"] = decimals
    return panel


GREEN_AT_ZERO = [{"color": "green", "value": None}, {"color": "red", "value": 1}]

# --- Row 1: headline numbers ------------------------------------------------
add(stat("Pass rate (latest runs)",
         prom(f'{last("pass")} / {last("pass|fail")}'),
         "Passed / (passed + failed) across the latest run of every repo and suite. Skipped tests and known-defect suites are left out.",
         unit="percentunit", decimals=1,
         thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.9}, {"color": "green", "value": 0.99}]),
    0, 0, 6, 4)
add(stat("Failing now",
         prom(last("fail")),
         "Failed tests in the latest run of every repo and suite (known defects left out).",
         thresholds=GREEN_AT_ZERO),
    6, 0, 6, 4)
add(stat("Test executions",
         loki(f'sum(count_over_time({LOKI_SEL} | {GATE} [$__range]))', instant=True),
         "Every test result received in the time range, across all runs."),
    12, 0, 6, 4)
add(stat("Known defects still failing",
         prom(last("fail", KNOWN)),
         "Tests that assert correct behaviour against bugs in the systems under test. They pass once the bug is fixed.",
         thresholds=[{"color": "purple", "value": None}]),
    18, 0, 6, 4)

# --- Row 2: by repo -----------------------------------------------------------
pass_by_repo = (
    f'sum by (service_name) (last_over_time(test_run_cases{{{PROM_SEL}, {GATE}, test_case_result_status="pass"}}[$__range]))'
    f' / sum by (service_name) (last_over_time(test_run_cases{{{PROM_SEL}, {GATE}, test_case_result_status=~"pass|fail"}}[$__range]))'
)
add({
    "type": "bargauge", "title": "Pass rate by repo (latest run)",
    "datasource": PROM, "targets": [{**prom(pass_by_repo, "{{service_name}}", instant=True), "format": "time_series"}],
    "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "minVizHeight": 16},
    "fieldConfig": {"defaults": {"unit": "percentunit", "min": 0, "max": 1, "decimals": 0,
                                 "color": {"mode": "thresholds"},
                                 "thresholds": {"mode": "absolute", "steps": [
                                     {"color": "red", "value": None}, {"color": "orange", "value": 0.9}, {"color": "green", "value": 0.99}]}},
                    "overrides": []},
}, 0, 4, 10, 10)

add({
    "type": "timeseries", "title": "Failed tests over time",
    "description": "Failed test results received, per repo (known defects left out).",
    "datasource": LOKI,
    "targets": [loki(f'sum by (service_name) (count_over_time({LOKI_SEL} | {GATE} | test_case_result_status="fail" [$__auto]))',
                     "{{service_name}}")],
    "fieldConfig": {"defaults": {"custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}},
                                 "unit": "none", "decimals": 0, "noValue": "No failed tests in this time range"},
                    "overrides": []},
    "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
}, 10, 4, 14, 10)

# --- Row 3: what failed -------------------------------------------------------
add({
    "type": "logs", "title": "Latest failures",
    "description": "Newest first. Expand a line for the full message and the link to the CI run.",
    "datasource": LOKI,
    "targets": [loki(
        f'{LOKI_SEL} | {GATE} | test_case_result_status="fail" '
        '| line_format "{{.service_name}} · {{.test_suite_name}} · {{.test_case_name}} — {{.test_message}}  ({{.cicd_pipeline_run_url_full}})" '
        # Grafana prints every remaining field after the line; the line already has what matters.
        '| keep service_name')],
    "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True, "showLabels": False, "showCommonLabels": False,
                "dedupStrategy": "none", "prettifyLogMessage": False},
}, 0, 14, 24, 9)

flaky = (
    'count by (service_name, test_case_name) ('
    f'sum by (service_name, test_case_name, test_case_result_status) (count_over_time({LOKI_SEL} | {GATE} '
    '| test_case_result_status=~"pass|fail" [$__range]))) > 1'
)
add({
    "type": "table", "title": "Flaky tests",
    "description": "Tests that both passed and failed in the time range.",
    "datasource": LOKI, "targets": [loki(flaky, instant=True)],
    "transformations": [{"id": "labelsToFields", "options": {"mode": "columns"}},
                        {"id": "organize", "options": {"excludeByName": {"Time": True, "Value #A": True},
                                                       "renameByName": {"service_name": "Repo", "test_case_name": "Test"}}}],
    "fieldConfig": {"defaults": {"noValue": "No flaky tests in this time range"}, "overrides": []},
    "options": {"showHeader": True},
}, 0, 23, 12, 9)

slowest = (
    'topk(10, max by (service_name, test_case_name) (max_over_time('
    f'{LOKI_SEL} | {GATE} | test_suite_name!="load" | unwrap test_duration_ms [$__range])))'
)
add({
    "type": "table", "title": "Slowest tests",
    "description": "Longest single run of each test in the time range (load tests left out).",
    "datasource": LOKI, "targets": [loki(slowest, instant=True)],
    "transformations": [{"id": "labelsToFields", "options": {"mode": "columns"}},
                        {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                       "renameByName": {"service_name": "Repo", "test_case_name": "Test", "Value #A": "Slowest (ms)"}}},
                        {"id": "sortBy", "options": {"sort": [{"field": "Slowest (ms)", "desc": True}]}}],
    "fieldConfig": {"defaults": {"unit": "ms", "decimals": 0}, "overrides": []},
    "options": {"showHeader": True},
}, 12, 23, 12, 9)

# --- Row 4: known defects and load ---------------------------------------------
add({
    "type": "table", "title": "Known defects",
    "description": "Each known-defect test's latest result. A pass means the bug in the system under test was fixed.",
    "datasource": LOKI,
    "targets": [loki(
        f'sum by (service_name, test_suite_name, test_case_name, test_case_result_status) '
        f'(count_over_time({LOKI_SEL} | {KNOWN} [$__range]))', instant=True)],
    "transformations": [{"id": "labelsToFields", "options": {"mode": "columns"}},
                        {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                       "renameByName": {"service_name": "Repo", "test_suite_name": "Suite",
                                                                        "test_case_name": "Test", "test_case_result_status": "Result",
                                                                        "Value #A": "Runs"}}}],
    "fieldConfig": {"defaults": {}, "overrides": [
        {"matcher": {"id": "byName", "options": "Result"},
         "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                        {"id": "mappings", "value": [{"type": "value", "options": {
                            "fail": {"color": "purple", "text": "still failing"},
                            "pass": {"color": "green", "text": "fixed?"}}}]}]}]},
    "options": {"showHeader": True},
}, 0, 32, 12, 9)

# Load-test repos send their suite as "load": k6 checks and thresholds, and one
# case per JMeter sampler.
LOAD = f'{LOKI_SEL} | test_suite_name="load"'

add({
    "type": "table", "title": "Load test checks",
    "description": "Every k6 check and threshold, and every JMeter sampler, with how many runs it passed or failed in.",
    "datasource": LOKI,
    "targets": [loki(
        'sum by (service_name, test_group_name, test_case_name, test_case_result_status) '
        f'(count_over_time({LOAD} [$__range]))', instant=True)],
    "transformations": [{"id": "labelsToFields", "options": {"mode": "columns"}},
                        {"id": "organize", "options": {
                            "excludeByName": {"Time": True},
                            "indexByName": {"service_name": 0, "test_group_name": 1, "test_case_name": 2,
                                            "test_case_result_status": 3, "Value #A": 4},
                            "renameByName": {"service_name": "Repo", "test_group_name": "Group",
                                             "test_case_name": "Check", "test_case_result_status": "Result",
                                             "Value #A": "Runs"}}},
                        {"id": "sortBy", "options": {"sort": [{"field": "Result"}]}}],
    "fieldConfig": {"defaults": {"noValue": "No load test runs in this time range"}, "overrides": [
        {"matcher": {"id": "byName", "options": "Result"},
         "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                        {"id": "mappings", "value": [{"type": "value", "options": {
                            "fail": {"color": "red", "text": "failed"},
                            "pass": {"color": "green", "text": "passed"}}}]}]},
        {"matcher": {"id": "byName", "options": "Repo"},
         "properties": [{"id": "custom.width", "value": 190}]}]},
    "options": {"showHeader": True},
}, 12, 32, 12, 9)

add({
    "type": "timeseries", "title": "Load test response times (JMeter)",
    "description": "p95 and average response time per sampler, one point per run.",
    "datasource": LOKI,
    "targets": [
        loki(f'max by (test_case_name) (max_over_time({LOAD} | unwrap jmeter_p95_ms | __error__="" [$__auto]))',
             "{{test_case_name}} p95", ref="A"),
        loki(f'max by (test_case_name) (max_over_time({LOAD} | unwrap jmeter_avg_ms | __error__="" [$__auto]))',
             "{{test_case_name}} avg", ref="B"),
    ],
    "fieldConfig": {"defaults": {"unit": "ms", "noValue": "No JMeter runs in this time range",
                                 "custom": {"drawStyle": "points", "pointSize": 8, "showPoints": "always"}},
                    "overrides": [{"matcher": {"id": "byFrameRefID", "options": "B"},
                                   "properties": [{"id": "custom.pointSize", "value": 5}]}]},
    "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"]},
                "tooltip": {"mode": "multi"}},
}, 0, 41, 24, 9)

add({
    "type": "timeseries", "title": "Suite duration per run",
    "description": "Sum of test durations in each run, per repo and suite.",
    "datasource": PROM,
    "targets": [prom(f'sum by (service_name, test_suite_name) (test_run_duration_seconds{{{PROM_SEL}}})',
                     "{{service_name}} / {{test_suite_name}}")],
    "fieldConfig": {"defaults": {"unit": "s", "custom": {"drawStyle": "points", "pointSize": 7, "showPoints": "always"}},
                    "overrides": []},
    "options": {"legend": {"displayMode": "table", "placement": "right"}, "tooltip": {"mode": "multi"}},
}, 0, 50, 24, 9)


def datasource_var(name, label, kind, regex=""):
    return {"name": name, "label": label, "type": "datasource", "query": kind, "hide": 0,
            "refresh": 1, "current": {}, "options": [], "regex": regex}


def query_var(name, label, label_name, default=None):
    return {"name": name, "label": label, "type": "query", "datasource": PROM,
            "query": {"query": f"label_values(test_run_cases, {label_name})", "refId": f"{name}-values"},
            "definition": f"label_values(test_run_cases, {label_name})",
            "refresh": 2, "includeAll": True, "multi": True, "allValue": ".*",
            "current": ({"selected": True, "text": default, "value": default} if default
                        else {"selected": True, "text": ["All"], "value": ["$__all"]}),
            "options": [], "sort": 1, "hide": 0}


dashboard = {
    "title": "CI test results",
    "uid": "test-results-otel",
    "description": "Test results from every repo's CI, sent by gillersja1/test-results-otel.",
    "tags": ["ci", "tests", "opentelemetry"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "time": {"from": "now-7d", "to": "now"},
    "refresh": "",
    "schemaVersion": 39,
    "templating": {"list": [
        datasource_var("prometheus", "Metrics", "prometheus"),
        # Grafana Cloud also has Loki sources for alert history and usage; skip those.
        datasource_var("loki", "Logs", "loki", "/^(?!.*(alert-state-history|usage-insights)).*$/"),
        query_var("repo", "Repo", "service_name"),
        # Default branches only, so PR branches don't double-count; pick All to include them.
        query_var("branch", "Branch", "vcs_ref_head_name", default=["main", "master"]),
    ]},
    "links": [{"title": "test-results-otel", "type": "link", "url": "https://github.com/gillersja1/test-results-otel",
               "targetBlank": True, "icon": "external link"}],
    "panels": panels,
}

def public_copy(source: dict) -> dict:
    """The same dashboard without variables, for Grafana's "Share externally".

    Externally shared dashboards can't use variables, so the data sources are
    chosen once at import (the __inputs below) and the filters are fixed: every
    repo, default branches only.
    """
    text = json.dumps(source)
    for old, new in (("${prometheus}", "${DS_PROMETHEUS}"), ("${loki}", "${DS_LOKI}"),
                     ('=~\\"$repo\\"', '=~\\".*\\"'), ('=~\\"$branch\\"', '=~\\"main|master\\"')):
        text = text.replace(old, new)
    copy = json.loads(text)
    leftover = [v for v in ("$repo", "$branch", "${prometheus}", "${loki}") if v in text]
    assert not leftover, f"variables left in the public copy: {leftover}"
    copy.update(
        title="CI test results (public)",
        uid="test-results-otel-public",
        description=source["description"] + " Read-only copy for sharing: all repos, main/master branches.",
        templating={"list": []},
        __inputs=[
            {"name": "DS_PROMETHEUS", "label": "Metrics (Prometheus)", "type": "datasource",
             "pluginId": "prometheus", "pluginName": "Prometheus"},
            {"name": "DS_LOKI", "label": "Logs (Loki)", "type": "datasource",
             "pluginId": "loki", "pluginName": "Loki"},
        ],
    )
    return copy


here = os.path.dirname(os.path.abspath(__file__))
for name, content in (("ci-test-results.json", dashboard), ("ci-test-results-public.json", public_copy(dashboard))):
    out = os.path.join(here, name)
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(content, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"wrote {out} ({len(panels)} panels)")
