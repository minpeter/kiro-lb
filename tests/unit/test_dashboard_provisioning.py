"""The provisioned Grafana dashboard must match what the exporter emits.

`deploy/grafana/kiro-lb.json` is checked in so that renaming a metric cannot
silently empty a panel. Without these assertions the file would be a copy of
production with no way to notice it had drifted, which is the only reason to keep
it in the repo at all.

The exporter has already been through one such rename: latency moved from two
counters to a summary and the quota reset changed from days to seconds.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro.metrics import _FAMILIES

_REPO = Path(__file__).resolve().parents[2]
_DASHBOARD = _REPO / "deploy" / "grafana" / "kiro-lb.json"
_PROBE = _REPO / "deploy" / "pushgateway"

# Pushgateway owns these two labels (honor_labels: true), and the export script
# sets them in the PUT path.
_PUSH_JOB = "kiro-lb-usage"


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(_DASHBOARD.read_text())


@pytest.fixture(scope="module")
def exprs(dashboard) -> list[tuple[str, str]]:
    found = []
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        for target in panel.get("targets", []):
            if "expr" in target:
                found.append((panel["title"], target["expr"]))
    return found


def _emitted_series() -> set[str]:
    """Every series name the exporter can produce, including summary children."""
    names = set()
    for name, kind, _ in _FAMILIES:
        names.add(name)
        if kind in ("summary", "histogram"):
            names.update({f"{name}_sum", f"{name}_count"})
        if kind == "histogram":
            names.add(f"{name}_bucket")
    return names


class TestMetricNames:
    def test_every_queried_metric_is_emitted(self, exprs):
        emitted = _emitted_series()
        referenced = set()
        for _, expr in exprs:
            referenced.update(re.findall(r"\bkiro_lb_[a-z_]+", expr))

        unknown = referenced - emitted
        assert not unknown, f"dashboard queries metrics the exporter does not emit: {sorted(unknown)}"

    def test_the_dashboard_actually_queries_something(self, exprs):
        # Guards against a truncated or placeholder file passing the test above.
        assert len(exprs) > 20
        assert any("kiro_lb_tokens_total" in expr for _, expr in exprs)
        assert any("kiro_lb_accounts" in expr for _, expr in exprs)

    def test_no_stale_metric_names_survive(self, exprs):
        """Names the exporter used before promtool forced a rename."""
        joined = " ".join(expr for _, expr in exprs)

        assert "kiro_lb_account_quota_reset_days" not in joined
        # These were counters named _sum/_count; they are now a summary's children,
        # which is only valid alongside the summary family itself.
        assert "kiro_lb_request_latency_seconds_total" not in joined

    def test_label_names_match_the_exporter(self, exprs):
        """A label typo empties a panel just as quietly as a metric typo."""
        known = {
            "job",
            "instance",
            "model",
            "protocol",
            "status_class",
            "direction",
            "key_id",
            "key_name",
            "account",
            "state",
            "outcome",
            "version",
        }
        for title, expr in exprs:
            for label in re.findall(r"(?:by|without)\s*\(([^)]*)\)", expr):
                for name in (part.strip() for part in label.split(",")):
                    if name:
                        assert name in known, f"{title}: unknown label {name!r}"
            for name in re.findall(r"[{,]\s*([a-z_]+)\s*=~?\"", expr):
                assert name in known, f"{title}: unknown label {name!r}"


class TestPushgatewayContract:
    def test_every_query_filters_on_the_push_job(self, exprs):
        """Without the job filter a panel would also match any future exporter."""
        for title, expr in exprs:
            assert f'job="{_PUSH_JOB}"' in expr, f"{title} does not filter on the push job"

    def test_the_export_script_pushes_to_that_job(self):
        script = (_PROBE / "export_usage.sh").read_text()

        assert f"USAGE_JOB:-{_PUSH_JOB}" in script
        # PUT replaces the grouping; POST would merge and leave stale series for a
        # model or account that has since disappeared.
        assert "--request PUT" in script
        assert "--request POST" not in script

    def test_the_script_requires_its_key_rather_than_pushing_nothing(self):
        script = (_PROBE / "export_usage.sh").read_text()

        # `${VAR:?message}` aborts on an unset key. Silently pushing an empty body
        # would wipe the grouping in Pushgateway.
        assert "${KIRO_LB_API_KEY:?" in script
        assert "set -eu" in script

    def test_no_credential_is_committed(self):
        assert not (_PROBE / "kiro-lb.env").exists() or "kiro-lb.env" in (_REPO / ".gitignore").read_text()
        example = (_PROBE / "kiro-lb.env.example").read_text()
        assert "replace-me" in example

    def test_the_timer_interval_is_shorter_than_the_dashboard_rate_window(self):
        """A rate window must span several pushes or it can read as zero."""
        timer = (_PROBE / "kiro-lb-usage-export.timer").read_text()
        seconds = int(re.search(r"OnUnitActiveSec=(\d+)s", timer).group(1))

        assert seconds <= 60
        # The dashboard uses 5m windows; assert the margin is real, not incidental.
        assert seconds * 4 <= 5 * 60


class TestDashboardShape:
    def test_uid_and_title_are_stable(self, dashboard):
        # The URL /d/kiro-lb/... and the README both depend on these.
        assert dashboard["uid"] == "kiro-lb"
        assert dashboard["title"] == "kiro-lb Gateway"

    def test_panels_have_unique_ids(self, dashboard):
        ids = [p["id"] for p in dashboard["panels"]]
        assert len(ids) == len(set(ids))

    def test_rate_windows_span_several_pushes(self, exprs):
        """`$__rate_interval` would follow the dashboard's zoom, not the push rate."""
        for title, expr in exprs:
            assert "$__rate_interval" not in expr, f"{title} uses $__rate_interval"
            for window in re.findall(r"\[(\d+)([smh])\]", expr):
                value, unit = int(window[0]), window[1]
                as_seconds = value * {"s": 1, "m": 60, "h": 3600}[unit]
                assert as_seconds >= 120, f"{title}: {value}{unit} window is too short for a 30s push"

    def test_latency_is_a_mean_not_a_fake_quantile(self, exprs):
        """The summary carries no quantiles, so a p95 panel would be a lie."""
        joined = " ".join(expr for _, expr in exprs)

        assert "histogram_quantile" not in joined
        assert 'quantile="' not in joined
        # The mean is present and divides sum by count.
        assert any(
            "kiro_lb_request_latency_seconds_sum" in expr and "kiro_lb_request_latency_seconds_count" in expr
            for _, expr in exprs
        )

    def test_divisions_guard_against_a_zero_denominator(self, exprs):
        for title, expr in exprs:
            if "/" in expr and "kiro_lb" in expr:
                assert "clamp_min" in expr, f"{title} divides without clamping the denominator"

    def test_token_panels_use_the_native_short_unit(self, dashboard):
        """Nine-digit token counts are unreadable raw; `short` renders 1.06 Bil."""
        checked = 0
        for panel in dashboard["panels"]:
            if panel["type"] == "row":
                continue
            queries = " ".join(t.get("expr", "") for t in panel.get("targets", []))
            if "kiro_lb_tokens_total" not in queries:
                continue
            checked += 1
            if panel["type"] == "table":
                units = [
                    prop["value"]
                    for override in panel["fieldConfig"]["overrides"]
                    for prop in override["properties"]
                    if prop["id"] == "unit"
                ]
                assert "short" in units, panel["title"]
            else:
                assert panel["fieldConfig"]["defaults"].get("unit") == "short", panel["title"]
        assert checked >= 5

    def test_request_counts_stay_unscaled(self, dashboard):
        """`23.1 K` loses more than it saves at four or five digits."""
        for panel in dashboard["panels"]:
            if panel["type"] != "timeseries":
                continue
            queries = " ".join(t.get("expr", "") for t in panel.get("targets", []))
            if "kiro_lb_requests_total" in queries and "tokens" not in queries:
                assert panel["fieldConfig"]["defaults"].get("unit") != "short", panel["title"]

    def test_no_secret_material_is_embedded(self, dashboard):
        raw = json.dumps(dashboard)

        for needle in ("Authorization", "Bearer ", "klb_", "PROXY_API_KEY", "password", "refreshToken"):
            assert needle not in raw, needle

    def test_account_identifiers_are_never_credential_paths(self, dashboard):
        raw = json.dumps(dashboard)

        # The exporter labels accounts with a short digest; a path or an email
        # appearing here would mean the exposition leaked one.
        assert "/data/logins" not in raw
        assert "builder-id-" not in raw
        assert "@" not in re.sub(r"https?://[^\"]*", "", raw)
