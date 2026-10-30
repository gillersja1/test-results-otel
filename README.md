# test-results-otel

[![CI](https://github.com/gillersja1/test-results-otel/actions/workflows/ci.yml/badge.svg)](https://github.com/gillersja1/test-results-otel/actions/workflows/ci.yml)

A GitHub Action that sends test results to any OpenTelemetry (OTLP) backend,
such as Grafana Cloud, so every repo's test runs land on one dashboard.

It reads the results file your tests already write and sends:

- **one log line per test**: `PASS`, `FAIL` (with the assertion message) or `SKIP`,
  tagged with the repo, branch, commit, workflow and a link to the run
- **run totals as metrics**: test counts by result, and total duration

| Format | Written by | Becomes one "test" per |
|---|---|---|
| TRX (`.trx`) | `dotnet test --logger trx` | test (data rows count separately) |
| JUnit XML (`.xml`) | Playwright, Newman, Gradle, pytest, Jest, ... | `<testcase>` |
| k6 summary (`.json`) | `handleSummary` writing `JSON.stringify(data)` | check, and threshold |
| JMeter JTL (`.jtl`, `.csv`) | `jmeter -n -l results.jtl` (CSV with a header) | sampler label |

It's plain Python with no dependencies, so it runs on Linux, Windows and macOS
runners without an install step.

## Usage

Add a step after your tests. `if: always()` matters: without it, the step is
skipped exactly when tests fail, which is when you most want the data.

```yaml
      - name: Run tests
        run: dotnet test --logger "trx;LogFileName=results.trx"

      - name: Send test results to Grafana
        if: always()
        uses: gillersja1/test-results-otel@v1
        with:
          results: "**/TestResults/*.trx"
          suite: ui
          otlp-endpoint: ${{ secrets.OTEL_EXPORTER_OTLP_ENDPOINT }}
          otlp-headers: ${{ secrets.OTEL_EXPORTER_OTLP_HEADERS }}
```

| Input | Default | |
|---|---|---|
| `results` | (required) | Glob pattern(s), one per line. `**` works. |
| `format` | `auto` | `trx`, `junit`, `k6` or `jmeter`; `auto` goes by extension. |
| `otlp-endpoint` | `$OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP base URL (the action adds `/v1/logs` and `/v1/metrics`). |
| `otlp-headers` | `$OTEL_EXPORTER_OTLP_HEADERS` | `key=value,key=value`, values URL-encoded. |
| `service-name` | repo name | `service.name` on everything sent. |
| `suite` | file's own suite | A label such as `ui`, `api` or `load`, for repos with several suites. |
| `dry-run` | `false` | Write the payloads to `$RUNNER_TEMP` instead of sending. |
| `fail-on-error` | `false` | Fail the step if sending fails. |

Telemetry should never break a build, so by default every problem is a warning:
no matching files, an unreadable file, or a rejected upload. With no endpoint
set (a fork, or secrets not added yet) it prints a notice and sends nothing.

## Setting up Grafana Cloud

1. In your Grafana Cloud stack, go to **Connections → Add new connection →
   OpenTelemetry (OTLP)** and create a token.
2. Grafana shows two environment variables. Add each one as a repository
   secret with the same name:
   - `OTEL_EXPORTER_OTLP_ENDPOINT` (like `https://otlp-gateway-prod-us-east-0.grafana.net/otlp`)
   - `OTEL_EXPORTER_OTLP_HEADERS` (like `Authorization=Basic%20...`)

## Finding the data

Logs (Loki). Each test is one line; attributes are structured metadata, with
dots turned into underscores:

```logql
# Every failure across all repos in the last day
{service_namespace="ci"} | test_case_result_status="fail"

# Failures in one repo, with the test name and run link
{service_name="PostmanReports"} | test_case_result_status="fail"
  | line_format "{{.test_case_name}}  {{.cicd_pipeline_run_url_full}}"
```

Metrics (Prometheus/Mimir), one set of points per run:

```promql
# Failed tests per repo
sum by (job) (test_run_cases{test_case_result_status="fail"})
```

## What gets sent

Every record carries these resource attributes, named after the OpenTelemetry
semantic conventions: `service.name`, `service.namespace` (`ci`),
`cicd.pipeline.name`, `cicd.pipeline.run.id`, `cicd.pipeline.run.url.full`,
`cicd.pipeline.task.name`, `vcs.repository.url.full`, `vcs.ref.head.name`,
`vcs.ref.head.revision`.

Each log record adds `test.case.name`, `test.case.result.status`
(`pass`/`fail`/`skipped`), `test.suite.name`, `test.class.name`,
`test.duration.ms` and `test.message`. k6 and JMeter records also carry their
own numbers (`k6.check.fails`, `jmeter.p95_ms`, ...).

Nothing else leaves the runner: no source code, no stack traces, no standard
output.

## Developing

```bash
python -m unittest discover -s tests -v
```

CI runs the unit tests on Linux, Windows and macOS, then runs the action
itself over the sample files in `tests/fixtures`.
