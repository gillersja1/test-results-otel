import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
import otlp  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class FakeCollector(BaseHTTPRequestHandler):
    """Records what it receives. Set status to make it answer with an error."""

    received = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeCollector.received.append((self.path, dict(self.headers), json.loads(body)))
        self.send_response(FakeCollector.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def attrs(items):
    return {a["key"]: list(a["value"].values())[0] for a in items}


class SendingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeCollector)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/otlp"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeCollector.received = []
        FakeCollector.status = 200

    def run_action(self, **env):
        settings = {
            "TRO_RESULTS": os.path.join(FIXTURES, "sample.trx") + "\n" + os.path.join(FIXTURES, "sample-junit.xml"),
            "TRO_ENDPOINT": self.endpoint,
            "TRO_HEADERS": "Authorization=Basic%20abc123,X-Scope=ci",
            "TRO_SUITE": "unit",
            "GITHUB_REPOSITORY": "someone/ShopTests",
            "GITHUB_RUN_ID": "42",
            "GITHUB_REF_NAME": "main",
            "GITHUB_SHA": "abc",
        }
        settings.update(env)
        output = io.StringIO()
        with mock.patch.dict(os.environ, settings, clear=True), redirect_stdout(output):
            code = main.run()
        return code, output.getvalue()

    def test_sends_logs_and_metrics_with_the_headers(self):
        code, _ = self.run_action()
        self.assertEqual(code, 0)
        paths = [path for path, _, _ in FakeCollector.received]
        self.assertEqual(paths, ["/otlp/v1/logs", "/otlp/v1/metrics"])
        headers = FakeCollector.received[0][1]
        self.assertEqual(headers["Authorization"], "Basic abc123")
        self.assertEqual(headers["X-Scope"], "ci")

    def test_one_log_record_per_test_with_ci_context(self):
        self.run_action()
        logs = FakeCollector.received[0][2]["resourceLogs"][0]
        resource = attrs(logs["resource"]["attributes"])
        self.assertEqual(resource["service.name"], "ShopTests")
        self.assertEqual(resource["cicd.pipeline.run.url.full"], "https://github.com/someone/ShopTests/actions/runs/42")
        records = logs["scopeLogs"][0]["logRecords"]
        self.assertEqual(len(records), 9)  # 5 from the TRX, 4 from the JUnit file
        failed = [r for r in records if r["severityText"] == "ERROR"]
        self.assertIn("FAIL CartTotalIsRight: Expected: 42", [r["body"]["stringValue"] for r in failed])
        self.assertEqual(attrs(failed[0]["attributes"])["test.suite.name"], "unit")

    def test_metrics_count_each_result(self):
        self.run_action()
        metrics = FakeCollector.received[1][2]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        cases = next(m for m in metrics if m["name"] == "test.run.cases")
        counts = {attrs(p["attributes"])["test.case.result.status"]: int(p["asInt"]) for p in cases["gauge"]["dataPoints"]}
        self.assertEqual(counts, {"pass": 3, "fail": 4, "skipped": 2})

    def test_missing_endpoint_sends_nothing_and_passes(self):
        code, output = self.run_action(TRO_ENDPOINT="")
        self.assertEqual(code, 0)
        self.assertEqual(FakeCollector.received, [])
        self.assertIn("No OTLP endpoint set", output)

    def test_falls_back_to_the_standard_otel_variables(self):
        self.run_action(TRO_ENDPOINT="", TRO_HEADERS="", OTEL_EXPORTER_OTLP_ENDPOINT=self.endpoint,
                        OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20fromenv")
        self.assertEqual(FakeCollector.received[0][1]["Authorization"], "Basic fromenv")

    def test_rejected_upload_warns_but_passes_by_default(self):
        FakeCollector.status = 401
        code, output = self.run_action()
        self.assertEqual(code, 0)
        self.assertIn("Sending logs failed: HTTP 401", output)
        self.assertEqual(len(FakeCollector.received), 2)  # 4xx isn't retried

    def test_rejected_upload_fails_when_asked(self):
        FakeCollector.status = 401
        code, _ = self.run_action(TRO_FAIL_ON_ERROR="true")
        self.assertEqual(code, 1)

    def test_no_matching_files_warns_and_passes(self):
        code, output = self.run_action(TRO_RESULTS="nowhere/*.trx")
        self.assertEqual(code, 0)
        self.assertIn("No results files matched", output)

    def test_dry_run_writes_payloads_instead_of_sending(self):
        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp)
        code, _ = self.run_action(TRO_DRY_RUN="true", RUNNER_TEMP=temp)
        self.assertEqual(code, 0)
        self.assertEqual(FakeCollector.received, [])
        self.assertEqual(sorted(os.listdir(temp)), ["test-results-otel-logs.json", "test-results-otel-metrics.json"])


class HeaderTests(unittest.TestCase):
    def test_parses_and_url_decodes(self):
        self.assertEqual(otlp.parse_headers("Authorization=Basic%20a%3Db, x = y"),
                         {"Authorization": "Basic a=b", "x": "y"})

    def test_ignores_empty_and_malformed(self):
        self.assertEqual(otlp.parse_headers(""), {})
        self.assertEqual(otlp.parse_headers("nonsense"), {})


if __name__ == "__main__":
    unittest.main()
