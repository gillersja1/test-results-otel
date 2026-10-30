"""Grafana alert rules for the test results and the live Playwright Dungeon Crawl.

Applies, through Grafana's alerting provisioning API:
  - an email contact point and a notification policy that routes to it
  - an "Alerts" folder with two rule groups: ci (test results) and dungeon (live site)

Usage:
  GRAFANA_URL=https://<stack>.grafana.net GRAFANA_TOKEN=<service account token> \\
  ALERT_EMAIL=you@example.com python alerts/alerts.py [--dry-run]

The token needs the Editor role. Running it again updates everything in place.
"""

import json
import os
import sys
import urllib.error
import urllib.request

PROM = "grafanacloud-prom"
LOKI = "grafanacloud-logs"
FOLDER = {"uid": "test-results-alerts", "title": "Alerts"}
CONTACT_POINT = "Email"

DAY = 86400

# --- Queries --------------------------------------------------------------------

# Latest run of every repo and suite on its default branch. Known-defect suites
# are expected to fail, and the action's self-test fails on purpose.
CI_FAILING = (
    'sum by (service_name, test_suite_name) (last_over_time(test_run_cases{service_namespace="ci", '
    'vcs_ref_head_name=~"main|master", test_suite_name!~"known-defects.*|self-test", '
    'test_case_result_status="fail"}[7d]))'
)

# A known-defect test that passed in the last 6 hours without failing, but failed
# at some point this week: the bug it asserts against has probably been fixed.
_KD = '{service_namespace="ci"} | test_suite_name=~"known-defects.*" | vcs_ref_head_name=~"main|master"'
_KD_BY = "service_name, test_suite_name, test_group_name, test_case_name"
KNOWN_DEFECT_FIXED = (
    f'(sum by ({_KD_BY}) (count_over_time({_KD} | test_case_result_status="pass" [6h]))'
    f' unless sum by ({_KD_BY}) (count_over_time({_KD} | test_case_result_status="fail" [6h])))'
    f' and sum by ({_KD_BY}) (count_over_time({_KD} | test_case_result_status="fail" [7d]))'
)

# Error (17) and above: unhandled exceptions are logged at Error by ASP.NET Core.
DUNGEON_ERRORS = 'sum(count_over_time({service_name="playwright-dungeon-crawl"} | severity_number >= 17 [5m]))'

DUNGEON_P95 = (
    'histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{'
    'service_name="playwright-dungeon-crawl", http_route!="", http_route!~"/health|.*[.].*"}[10m])))'
)

# Per room over the last day, only once there are enough answers to mean something.
_ANSWERS = '{service_name="playwright-dungeon-crawl"} | scope_name="PlaywrightDungeonCrawl.Rooms" |= "Answer in "'
DUNGEON_SOLVE_RATE = (
    f'((sum by (Room) (count_over_time({_ANSWERS} | Outcome="correct" [24h]))'
    f' or (0 * sum by (Room) (count_over_time({_ANSWERS} [24h]))))'
    f' / sum by (Room) (count_over_time({_ANSWERS} [24h])))'
    f' and (sum by (Room) (count_over_time({_ANSWERS} [24h])) >= 10)'
)


# --- Rules ----------------------------------------------------------------------

def rule(uid, title, group, datasource, expr, seconds, op, threshold, summary, description,
         duration="0s", severity="warning"):
    model = {"refId": "A", "expr": expr}
    model.update({"queryType": "instant"} if datasource == LOKI else {"instant": True, "range": False})
    return {
        "uid": uid,
        "title": title,
        "ruleGroup": group,
        "folderUID": FOLDER["uid"],
        "condition": "C",
        "data": [
            {"refId": "A", "datasourceUid": datasource, "relativeTimeRange": {"from": seconds, "to": 0}, "model": model},
            {"refId": "B", "datasourceUid": "__expr__", "relativeTimeRange": {"from": 0, "to": 0},
             "model": {"refId": "B", "type": "reduce", "expression": "A", "reducer": "last",
                       "settings": {"mode": "dropNN"}}},
            {"refId": "C", "datasourceUid": "__expr__", "relativeTimeRange": {"from": 0, "to": 0},
             "model": {"refId": "C", "type": "threshold", "expression": "B",
                       "conditions": [{"evaluator": {"type": op, "params": [threshold]}}]}},
        ],
        # No data means nothing to report (no runs, no traffic), not a problem.
        "noDataState": "OK",
        "execErrState": "Error",
        "for": duration,
        "annotations": {"summary": summary, "description": description},
        "labels": {"severity": severity, "source": group},
    }


RULES = [
    rule("ci-tests-failing", "CI tests failing on main", "ci", PROM, CI_FAILING, 7 * DAY, "gt", 0,
         "{{ $labels.service_name }} ({{ $labels.test_suite_name }}): {{ $values.B }} test(s) failing on main",
         "The latest run of this suite on the default branch has failing tests. "
         "Open the dashboard's Latest failures panel for the messages and the CI run.",
         severity="critical"),
    rule("known-defect-fixed", "Known defect now passes", "ci", LOKI, KNOWN_DEFECT_FIXED, 7 * DAY, "gt", 0,
         "Known defect may be fixed: {{ $labels.test_case_name }} ({{ $labels.service_name }}, {{ $labels.test_suite_name }})",
         "This known-defect test failed earlier this week and now passes on main. If the bug in the system "
         "under test is fixed, move the test out of the known-defects suite.",
         severity="info"),
    rule("dungeon-errors", "Dungeon site errors", "dungeon", LOKI, DUNGEON_ERRORS, 300, "gt", 0,
         "Playwright Dungeon Crawl logged {{ $values.B }} error(s) in the last 5 minutes",
         "Error-level logs from the live site, usually an unhandled exception behind an HTTP 500. "
         "Search the logs for service_name=\"playwright-dungeon-crawl\" to see them.",
         severity="critical"),
    rule("dungeon-slow", "Dungeon site slow", "dungeon", PROM, DUNGEON_P95, 600, "gt", 2,
         "Playwright Dungeon Crawl p95 response time is {{ humanizeDuration $values.B }}",
         "95% of page and API requests took longer than 2 seconds for 10 minutes "
         "(health checks and static files left out).",
         duration="10m"),
    rule("dungeon-players-struggling", "Dungeon players struggling", "dungeon", LOKI, DUNGEON_SOLVE_RATE, DAY,
         "lt", 0.2,
         "Only {{ humanizePercentage $values.B }} of answers in {{ $labels.Room }} were correct in the last day",
         "At least 10 answers, under 20% correct. The room may be too hard, or its lesson and nudges "
         "may need work.",
         severity="info"),
]

GROUP_INTERVALS = {"ci": 300, "dungeon": 60}


# --- Apply ----------------------------------------------------------------------

def contact_point(email):
    return {"uid": "test-results-email", "name": CONTACT_POINT, "type": "email",
            "settings": {"addresses": email, "singleEmail": False}, "disableResolveMessage": False}


POLICY = {
    "receiver": CONTACT_POINT,
    "group_by": ["grafana_folder", "alertname"],
    # Batch alerts that fire together, and remind at most once a day while one stays firing.
    "group_wait": "1m",
    "group_interval": "10m",
    "repeat_interval": "24h",
}


class Grafana:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method, path, body=None, ok_missing=False):
        request = urllib.request.Request(
            self.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     # Lets the rules stay editable in Grafana's UI.
                     "X-Disable-Provenance": "true"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as error:
            if ok_missing and error.code == 404:
                return None
            sys.exit(f"{method} {path}: HTTP {error.code} {error.read().decode()[:500]}")


def apply(grafana, email):
    # Looking up a missing folder by uid is a 403, not a 404, so check the list.
    if not any(f["uid"] == FOLDER["uid"] for f in grafana.call("GET", "/api/folders?limit=1000")):
        grafana.call("POST", "/api/folders", FOLDER)
    print("folder:", FOLDER["title"])

    point = contact_point(email)
    existing = grafana.call("GET", "/api/v1/provisioning/contact-points")
    if any(p.get("uid") == point["uid"] for p in existing):
        grafana.call("PUT", f"/api/v1/provisioning/contact-points/{point['uid']}", point)
    else:
        grafana.call("POST", "/api/v1/provisioning/contact-points", point)
    print("contact point:", CONTACT_POINT)

    grafana.call("PUT", "/api/v1/provisioning/policies", POLICY)
    print("notification policy: everything ->", CONTACT_POINT)

    for item in RULES:
        if grafana.call("GET", f"/api/v1/provisioning/alert-rules/{item['uid']}", ok_missing=True) is None:
            grafana.call("POST", "/api/v1/provisioning/alert-rules", item)
        else:
            grafana.call("PUT", f"/api/v1/provisioning/alert-rules/{item['uid']}", item)
        print("rule:", item["ruleGroup"], "/", item["title"])

    for group, interval in GROUP_INTERVALS.items():
        current = grafana.call("GET", f"/api/v1/provisioning/folder/{FOLDER['uid']}/rule-groups/{group}")
        current["interval"] = interval
        grafana.call("PUT", f"/api/v1/provisioning/folder/{FOLDER['uid']}/rule-groups/{group}", current)
        print(f"group {group}: every {interval}s")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        print(json.dumps({"contact_point": contact_point(os.environ.get("ALERT_EMAIL", "you@example.com")),
                          "policy": POLICY, "rules": RULES}, indent=2))
        sys.exit(0)
    missing = [name for name in ("GRAFANA_URL", "GRAFANA_TOKEN", "ALERT_EMAIL") if not os.environ.get(name)]
    if missing:
        sys.exit("Set " + ", ".join(missing))
    apply(Grafana(os.environ["GRAFANA_URL"], os.environ["GRAFANA_TOKEN"]), os.environ["ALERT_EMAIL"])
