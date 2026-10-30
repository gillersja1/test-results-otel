import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from parsers import detect_format, parse  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    return os.path.join(FIXTURES, name)


def by_name(cases):
    return {case.name: case for case in cases}


class DetectFormatTests(unittest.TestCase):
    def test_detects_each_format_from_the_file(self):
        self.assertEqual(detect_format(fixture("sample.trx")), "trx")
        self.assertEqual(detect_format(fixture("sample-junit.xml")), "junit")
        self.assertEqual(detect_format(fixture("sample-k6.json")), "k6")
        self.assertEqual(detect_format(fixture("sample.jtl")), "jmeter")

    def test_unknown_extension_asks_for_the_format_input(self):
        with self.assertRaisesRegex(ValueError, "set the format input"):
            detect_format("results.txt")

    def test_unknown_format_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown format"):
            parse(fixture("sample.trx"), "nunit2")


class TrxTests(unittest.TestCase):
    def setUp(self):
        self.cases = by_name(parse(fixture("sample.trx")))

    def test_reads_status_class_and_duration(self):
        login = self.cases["LoginWorks"]
        self.assertEqual(login.status, "pass")
        self.assertEqual(login.classname, "Shop.Tests.LoginTests")
        self.assertEqual(login.suite, "LoginTests")
        self.assertAlmostEqual(login.duration_s, 1.25)

    def test_failure_keeps_the_assertion_message(self):
        cart = self.cases["CartTotalIsRight"]
        self.assertEqual(cart.status, "fail")
        self.assertIn("But was:  41", cart.message)

    def test_not_executed_counts_as_skipped(self):
        self.assertEqual(self.cases["NeedsADatabase"].status, "skipped")

    def test_start_time_keeps_its_offset(self):
        # 2026-10-30T11:33:47.869035-04:00 is 15:33:47.869035 UTC.
        self.assertEqual(self.cases["LoginWorks"].start_unix_nano, 1793374427869035008)

    def test_data_rows_replace_their_parent(self):
        self.assertNotIn("PriceRows", self.cases)
        self.assertEqual(self.cases["PriceRows (1)"].status, "pass")
        self.assertEqual(self.cases["PriceRows (2)"].status, "fail")
        self.assertEqual(self.cases["PriceRows (2)"].classname, "Shop.Tests.CartTests")


class JUnitTests(unittest.TestCase):
    def setUp(self):
        self.cases = by_name(parse(fixture("sample-junit.xml")))

    def test_reads_every_case_in_every_suite(self):
        self.assertEqual(len(self.cases), 4)
        self.assertEqual(self.cases["logs in with a valid user"].suite, "login.spec.ts")
        self.assertEqual(self.cases["DELETE returns 201"].suite, "api")

    def test_failure_error_and_skipped(self):
        self.assertEqual(self.cases["rejects a locked user"].status, "fail")
        self.assertEqual(self.cases["rejects a locked user"].message, "expected error banner to be visible")
        self.assertEqual(self.cases["DELETE returns 201"].status, "fail")
        self.assertEqual(self.cases["GET /booking returns 200"].status, "skipped")

    def test_suite_timestamp_becomes_the_start_time(self):
        self.assertEqual(self.cases["logs in with a valid user"].start_unix_nano, 1793372400000000000)
        self.assertIsNone(self.cases["DELETE returns 201"].start_unix_nano)


class K6Tests(unittest.TestCase):
    def setUp(self):
        self.cases = by_name(parse(fixture("sample-k6.json")))

    def test_each_check_is_a_case(self):
        good = self.cases["list: status is 200"]
        self.assertEqual(good.status, "pass")
        self.assertEqual(good.suite, "::list users")
        bad = self.cases["list: returns 6 users"]
        self.assertEqual(bad.status, "fail")
        self.assertEqual(bad.message, "497 passed, 3 failed")

    def test_each_threshold_is_a_case(self):
        self.assertEqual(self.cases["http_req_duration: p(95)<500"].status, "pass")
        self.assertEqual(self.cases["http_req_failed: rate<0.01"].status, "fail")
        self.assertEqual(len(self.cases), 4)

    def test_rejects_json_that_isnt_a_k6_summary(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.write(handle, b"{}")
        os.close(handle)
        self.addCleanup(os.remove, path)
        with self.assertRaisesRegex(ValueError, "k6 summary"):
            parse(path, "k6")


class JMeterTests(unittest.TestCase):
    def setUp(self):
        self.cases = by_name(parse(fixture("sample.jtl")))

    def test_one_case_per_sampler_label(self):
        self.assertEqual(sorted(self.cases), ["GET /api/users", "POST /api/login"])

    def test_sampler_with_errors_fails_and_names_the_codes(self):
        login = self.cases["POST /api/login"]
        self.assertEqual(login.status, "fail")
        self.assertEqual(login.message, "1 of 2 samples failed (codes: 429)")
        self.assertEqual(login.attributes["jmeter.errors"], 1)

    def test_timings(self):
        users = self.cases["GET /api/users"]
        self.assertEqual(users.status, "pass")
        self.assertAlmostEqual(users.duration_s, 0.15)
        self.assertEqual(users.attributes["jmeter.p95_ms"], 180)
        self.assertEqual(users.start_unix_nano, 1793374427000 * 1_000_000)


if __name__ == "__main__":
    unittest.main()
