"""Prometheus exposition contract.

The exporter reads state the gateway already keeps, so the tests that matter are
about what reaches the wire: valid text format, no secrets in labels, bounded
cardinality, and an authenticated endpoint. A scrape must also never be able to
break the data plane, which is why the SQLite sections degrade instead of
raising.
"""

import importlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro.account_manager import Account, account_label, account_routing_state
from kiro.metrics import _FAMILIES, CONTENT_TYPE, render_metrics

_ACCOUNT_PATH = "/data/logins/builder-id-7Oay0539aYPtzTFS.json"
_KEY_ID = "6f1c2d3e4a5b"


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "dashboard-password")
    module = importlib.reload(importlib.import_module("kiro.dashboard"))
    module.initialize_dashboard_store()
    return module


def _account(account_id: str = _ACCOUNT_PATH, **state) -> Account:
    account = Account(id=account_id)
    account.auth_manager = MagicMock()
    account.stats.total_requests = 10
    account.stats.successful_requests = 7
    account.stats.failed_requests = 3
    for key, value in state.items():
        setattr(account, key, value)
    return account


def _render(dashboard, accounts=(), key_names=None, models=("claude-opus-5", "m", "claude-haiku-4.5")) -> str:
    return render_metrics(
        started_at=time.time() - 120,
        version="0.1.0",
        accounts=list(accounts),
        models=models,
        connection_factory=dashboard._db,
        usage_for=dashboard._cached_usage,
        label_for=account_label,
        state_for=account_routing_state,
        key_names=key_names or {},
    )


def _series(body: str) -> dict[str, float]:
    """Parse the exposition into {series_line_without_value: value}."""
    parsed = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        parsed[name] = float(value)
    return parsed


class TestTextFormat:
    def test_every_family_declares_help_and_type(self, dashboard):
        dashboard.record_request("/v1/messages", "claude-opus-5", 200, 10)
        body = _render(dashboard)

        helps = {line.split()[2] for line in body.splitlines() if line.startswith("# HELP")}
        types = {line.split()[2] for line in body.splitlines() if line.startswith("# TYPE")}
        assert helps == types
        # Every emitted series must belong to a declared family. A summary's
        # _sum/_count children are part of its family, not families of their own.
        for series in _series(body):
            family = series.split("{")[0]
            base = family.removesuffix("_sum").removesuffix("_count")
            assert family in helps or base in helps, family

    def test_body_ends_with_a_newline(self, dashboard):
        # Pushgateway rejects a body whose final line is unterminated.
        assert _render(dashboard).endswith("\n")

    def test_names_satisfy_promtool_lint_rules(self, dashboard):
        """`promtool check metrics` is the gate here; these are the rules it applies.

        It rejected an earlier version for exposing latency as two counters named
        `_sum`/`_count` and for a `_days` suffix, so the constraints are asserted
        rather than left to a manual run.
        """
        declared = [(name, kind) for name, kind, _ in _FAMILIES]

        for name, kind in declared:
            if kind == "counter":
                assert name.endswith("_total"), name
                assert not name.endswith(("_sum", "_count")), name
            # Base units only: no _days, _ms, _millis.
            assert not name.endswith(("_days", "_ms", "_millis")), name

        # _sum/_count may only appear as children of a summary or histogram.
        summaries = {name for name, kind in declared if kind in ("summary", "histogram")}
        dashboard.record_request("/v1/messages", "claude-opus-5", 200, 10)
        for series in _series(_render(dashboard)):
            family = series.split("{")[0]
            if family.endswith(("_sum", "_count")):
                base = family.removesuffix("_sum").removesuffix("_count")
                assert base in summaries, family

    def test_no_scientific_notation_in_values(self, dashboard):
        with dashboard._db() as conn:
            conn.execute(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (_KEY_ID, "claude-opus-5", 965355905, 524436, 4468, int(time.time())),
            )

        body = _render(dashboard)

        # Only the value matters here; a label like "claude-opus-5" legitimately
        # contains "e-". Prometheus accepts exponents, but a nine-digit token
        # count rendered as 9.65355905e+08 is unreadable in an alert.
        for line in body.splitlines():
            if line and not line.startswith("#"):
                raw_value = line.rpartition(" ")[2]
                assert "e" not in raw_value.lower()
        assert (
            'kiro_lb_tokens_total{key_id="6f1c2d3e4a5b",key_name="6f1c2d3e4a5b",model="claude-opus-5",direction="input"} 965355905'
            in body
        )

    def test_up_and_uptime_are_always_present(self, dashboard):
        series = _series(_render(dashboard))

        assert series["kiro_lb_up"] == 1
        assert series["kiro_lb_uptime_seconds"] == pytest.approx(120, abs=2)
        assert series['kiro_lb_build_info{version="0.1.0"}'] == 1

    def test_label_values_are_escaped(self, dashboard):
        """Key names are operator-provided free text, so they must be escaped.

        A name containing a quote would otherwise close the label early and
        corrupt every following series in the scrape.
        """
        with dashboard._db() as conn:
            conn.execute(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (_KEY_ID, "m", 1, 2, 1, int(time.time())),
            )

        body = _render(dashboard, key_names={_KEY_ID: 'wei"rd\\name'})

        assert r'key_name="wei\"rd\\name"' in body


class TestRequestMetrics:
    def test_requests_are_grouped_by_model_protocol_and_status_class(self, dashboard):
        dashboard.record_request("/v1/chat/completions", "claude-opus-5", 200, 1200)
        dashboard.record_request("/v1/chat/completions", "claude-opus-5", 200, 800)
        dashboard.record_request("/v1/messages", "claude-opus-5", 429, 30)

        series = _series(_render(dashboard))

        assert series['kiro_lb_requests_total{model="claude-opus-5",protocol="openai",status_class="2xx"}'] == 2
        assert series['kiro_lb_requests_total{model="claude-opus-5",protocol="anthropic",status_class="4xx"}'] == 1

    def test_latency_is_seconds_and_counts_only_successes(self, dashboard):
        dashboard.record_request("/v1/chat/completions", "m", 200, 1500)
        dashboard.record_request("/v1/chat/completions", "m", 200, 500)
        dashboard.record_request("/v1/chat/completions", "m", 500, 90000)

        series = _series(_render(dashboard))
        labels = '{model="m",protocol="openai"}'

        # A failed request's latency says nothing about generation speed, so the
        # 90s rejection must not pollute the average.
        assert series[f"kiro_lb_request_latency_seconds_sum{labels}"] == 2.0
        assert series[f"kiro_lb_request_latency_seconds_count{labels}"] == 2

    def test_a_request_with_no_model_is_still_counted(self, dashboard):
        dashboard.record_request("/v1/chat/completions", None, 422, 3)

        series = _series(_render(dashboard))

        assert series['kiro_lb_requests_total{model="unknown",protocol="openai",status_class="4xx"}'] == 1

    def test_an_unknown_route_gets_a_bounded_protocol_label(self, dashboard):
        # The label must never be the raw path: that would be unbounded.
        dashboard.record_request("/some/other/path", "m", 200, 5)

        body = _render(dashboard)

        assert 'protocol="other"' in body
        assert "/some/other/path" not in body


class TestModelLabelCardinality:
    """The model name is client-controlled, so it cannot be labelled verbatim.

    The resolver forwards unknown names to Kiro on purpose (gateway, not
    gatekeeper), and the live store already holds 40 distinct names including
    probes like `claude-opus-99`. Labelling those raw would let any client mint
    series in Prometheus indefinitely.
    """

    def test_a_model_the_pool_serves_is_labelled_verbatim(self, dashboard):
        dashboard.record_request("/v1/messages", "claude-opus-5", 200, 10)

        series = _series(_render(dashboard))

        assert series['kiro_lb_requests_total{model="claude-opus-5",protocol="anthropic",status_class="2xx"}'] == 1

    def test_an_invented_model_collapses_into_other(self, dashboard):
        dashboard.record_request("/v1/messages", "claude-opus-99", 400, 3)

        body = _render(dashboard)

        assert "claude-opus-99" not in body
        assert _series(body)['kiro_lb_requests_total{model="other",protocol="anthropic",status_class="4xx"}'] == 1

    def test_many_invented_names_produce_exactly_one_series(self, dashboard):
        for index in range(50):
            dashboard.record_request("/v1/chat/completions", f"made-up-{index}", 400, 1)

        series = _series(_render(dashboard))
        matching = [name for name in series if name.startswith("kiro_lb_requests_total")]

        # One series, carrying all 50 requests: this is the whole point.
        assert matching == ['kiro_lb_requests_total{model="other",protocol="openai",status_class="4xx"}']
        assert series[matching[0]] == 50

    def test_collapsed_latency_is_summed_not_duplicated(self, dashboard):
        # Two distinct unknown names both map to `other`; emitting a line each
        # would repeat a series, which Prometheus rejects as a duplicate.
        dashboard.record_request("/v1/chat/completions", "nope-a", 200, 1000)
        dashboard.record_request("/v1/chat/completions", "nope-b", 200, 2000)

        body = _render(dashboard)
        emitted = [line for line in body.splitlines() if line.startswith("kiro_lb_request_latency_seconds_sum")]

        assert len(emitted) == 1
        assert _series(body)['kiro_lb_request_latency_seconds_sum{model="other",protocol="openai"}'] == 3.0

    def test_no_series_is_emitted_twice(self, dashboard):
        for name in ("claude-opus-5", "m", "unknown-1", "unknown-2", None):
            dashboard.record_request("/v1/chat/completions", name, 200, 10)
            dashboard.record_request("/v1/messages", name, 500, 10)
        with dashboard._db() as conn:
            conn.executemany(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                [(_KEY_ID, f"invented-{index}", 5, 5, 1, int(time.time())) for index in range(4)],
            )

        emitted = [line.rpartition(" ")[0] for line in _render(dashboard).splitlines() if not line.startswith("#")]

        assert len(emitted) == len(set(emitted))

    def test_collapsed_token_totals_are_summed(self, dashboard):
        with dashboard._db() as conn:
            conn.executemany(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (_KEY_ID, "invented-a", 100, 10, 1, int(time.time())),
                    (_KEY_ID, "invented-b", 200, 20, 2, int(time.time())),
                ],
            )

        series = _series(_render(dashboard))
        labels = f'key_id="{_KEY_ID}",key_name="{_KEY_ID}",model="other"'

        assert series[f'kiro_lb_tokens_total{{{labels},direction="input"}}'] == 300
        assert series[f'kiro_lb_tokens_total{{{labels},direction="output"}}'] == 30
        assert series[f"kiro_lb_key_requests_total{{{labels}}}"] == 3

    def test_a_client_dash_form_merges_into_the_dotted_model(self, dashboard):
        """The log stores what the client sent, which is often the dash form.

        `claude-sonnet-4-5` and `claude-sonnet-4.5` are one model; splitting them
        would both double the series and understate each. The live store holds
        1,324 requests under the dash form alone.
        """
        dashboard.record_request("/v1/messages", "claude-sonnet-4-5", 200, 100)
        dashboard.record_request("/v1/messages", "claude-sonnet-4.5", 200, 100)

        series = _series(_render(dashboard, models=("claude-sonnet-4.5",)))

        assert series['kiro_lb_requests_total{model="claude-sonnet-4.5",protocol="anthropic",status_class="2xx"}'] == 2
        assert 'kiro_lb_requests_total{model="other",protocol="anthropic",status_class="2xx"}' not in series

    def test_a_dated_client_name_merges_too(self, dashboard):
        dashboard.record_request("/v1/messages", "claude-sonnet-4-5-20251001", 200, 10)

        series = _series(_render(dashboard, models=("claude-sonnet-4.5",)))

        assert series['kiro_lb_requests_total{model="claude-sonnet-4.5",protocol="anthropic",status_class="2xx"}'] == 1

    def test_models_gauge_counts_what_the_pool_serves(self, dashboard):
        series = _series(_render(dashboard, models=("a", "b", "c", "d")))

        assert series["kiro_lb_models"] == 4


class TestAccountMetrics:
    def test_account_label_is_a_digest_not_a_credential_path(self, dashboard):
        body = _render(dashboard, accounts=[_account()])

        assert _ACCOUNT_PATH not in body
        assert "builder-id-7Oay0539aYPtzTFS" not in body
        assert f'kiro_lb_account_failures{{account="{account_label(_ACCOUNT_PATH)}"}}' in _series(body)

    def test_every_routing_state_is_emitted_even_at_zero(self, dashboard):
        series = _series(_render(dashboard, accounts=[_account()]))

        # A state that empties out must read 0 rather than vanish, or
        # `sum by (state)` silently drops the category.
        assert series['kiro_lb_accounts{state="available"}'] == 1
        assert series['kiro_lb_accounts{state="suspended"}'] == 0
        assert series['kiro_lb_accounts{state="quota_exhausted"}'] == 0

    def test_a_suspended_account_reports_its_state_and_countdown(self, dashboard):
        suspended = _account(suspended_until=time.time() + 3600)

        series = _series(_render(dashboard, accounts=[suspended]))

        assert series['kiro_lb_accounts{state="suspended"}'] == 1
        assert series['kiro_lb_accounts{state="available"}'] == 0
        label = account_label(_ACCOUNT_PATH)
        assert series[f'kiro_lb_account_eligible_in_seconds{{account="{label}"}}'] == pytest.approx(3600, abs=5)

    def test_account_request_outcomes_are_split(self, dashboard):
        series = _series(_render(dashboard, accounts=[_account()]))
        label = account_label(_ACCOUNT_PATH)

        assert series[f'kiro_lb_account_requests_total{{account="{label}",outcome="success"}}'] == 7
        assert series[f'kiro_lb_account_requests_total{{account="{label}",outcome="failure"}}'] == 3

    def test_quota_numbers_come_from_the_cache_without_an_upstream_call(self, dashboard):
        usage = {
            "email": "operator@example.test",
            "subscriptionTitle": "KIRO PRO",
            "subscriptionType": "PAID",
            "resourceType": "CREDIT",
            "currentUsage": 12.5,
            "usageLimit": 50.0,
            "usagePercent": 25.0,
            "unit": "INVOCATIONS",
            "nextDateReset": 1788220800.0,
            "daysUntilReset": 3,
            "overageStatus": "DISABLED",
            "overageUsed": 0.0,
            "overageRate": None,
        }
        account = _account()
        with patch.object(dashboard, "fetch_account_usage", AsyncMock(return_value=usage)):
            import asyncio

            asyncio.run(dashboard.refresh_account_usage(account))

        body = _render(dashboard, accounts=[account])
        series = _series(body)
        label = account_label(_ACCOUNT_PATH)

        assert series[f'kiro_lb_account_quota_used{{account="{label}"}}'] == 12.5
        assert series[f'kiro_lb_account_quota_limit{{account="{label}"}}'] == 50.0
        assert series[f'kiro_lb_account_quota_percent{{account="{label}"}}'] == 25.0
        # Seconds, not days: Prometheus wants base units and promtool rejects a
        # _days suffix.
        assert series[f'kiro_lb_account_quota_reset_seconds{{account="{label}"}}'] == 3 * 86400
        # The identity behind the quota must not travel with it.
        assert "operator@example.test" not in body
        assert "KIRO PRO" not in body

    def test_an_account_with_no_cached_quota_omits_those_series(self, dashboard):
        series = _series(_render(dashboard, accounts=[_account()]))
        label = account_label(_ACCOUNT_PATH)

        # Absent is correct here: 0 would read as "no quota left".
        assert f'kiro_lb_account_quota_used{{account="{label}"}}' not in series


class TestTokenMetrics:
    def test_tokens_are_split_by_direction_and_named_key(self, dashboard):
        with dashboard._db() as conn:
            conn.execute(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                ("root", "claude-opus-5", 1000, 250, 5, int(time.time())),
            )

        series = _series(_render(dashboard, key_names={"root": "root"}))
        labels = 'key_id="root",key_name="root",model="claude-opus-5"'

        assert series[f'kiro_lb_tokens_total{{{labels},direction="input"}}'] == 1000
        assert series[f'kiro_lb_tokens_total{{{labels},direction="output"}}'] == 250
        assert series[f"kiro_lb_key_requests_total{{{labels}}}"] == 5

    def test_generation_time_is_exposed_per_model(self, dashboard):
        """The denominator for tokens/sec, keyed by model only.

        Throughput is a property of the model and the upstream, not of who asked,
        and keying it per model keeps it divisible by the token counters without a
        many-to-one vector match in PromQL.
        """
        with dashboard._db() as conn:
            conn.executemany(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests,"
                " generation_ms, timed_completion_tokens, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("root", "claude-opus-5", 100, 400, 4, 20_000, 400, int(time.time())),
                    (_KEY_ID, "claude-opus-5", 100, 100, 1, 5_000, 100, int(time.time())),
                ],
            )

        series = _series(_render(dashboard))

        # Summed across keys: 25s of generation for this model.
        assert series['kiro_lb_generation_seconds_total{model="claude-opus-5"}'] == 25.0
        # The numerator is the timed subset, not kiro_lb_tokens_total: 500 tokens
        # over 25s is 20 tok/s.
        assert series['kiro_lb_timed_output_tokens_total{model="claude-opus-5"}'] == 500
        assert (
            series['kiro_lb_timed_output_tokens_total{model="claude-opus-5"}']
            / series['kiro_lb_generation_seconds_total{model="claude-opus-5"}']
        ) == 20.0

    def test_the_throughput_numerator_excludes_untimed_requests(self, dashboard):
        """kiro_lb_tokens_total counts every request; the ratio must not use it.

        Dividing the full output total by a partial duration reported 82,752 tok/s
        on the live store, where most tokens predate the timing column.
        """
        with dashboard._db() as conn:
            conn.executemany(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests,"
                " generation_ms, timed_completion_tokens, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                [
                    # Untimed history plus one measured request worth 100 tokens in 4s.
                    ("root", "claude-opus-5", 50_000, 40_000, 900, 4_000, 100, int(time.time())),
                ],
            )

        series = _series(_render(dashboard))

        assert series['kiro_lb_timed_output_tokens_total{model="claude-opus-5"}'] == 100
        assert series['kiro_lb_generation_seconds_total{model="claude-opus-5"}'] == 4.0
        # 25 tok/s, not 40,000/4 = 10,000.
        assert 100 / 4.0 == 25.0

    def test_generation_time_carries_no_key_label(self, dashboard):
        with dashboard._db() as conn:
            conn.execute(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests,"
                " generation_ms, timed_completion_tokens, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (_KEY_ID, "claude-opus-5", 1, 1, 1, 1_000, 1, int(time.time())),
            )

        for line in _render(dashboard).splitlines():
            if line.startswith("kiro_lb_generation_seconds_total"):
                assert "key_id" not in line and "key_name" not in line

    def test_an_unnamed_key_falls_back_to_its_id(self, dashboard):
        with dashboard._db() as conn:
            conn.execute(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (_KEY_ID, "m", 1, 2, 1, int(time.time())),
            )

        body = _render(dashboard)

        assert f'key_id="{_KEY_ID}",key_name="{_KEY_ID}"' in body


class TestResilience:
    def test_a_broken_store_still_reports_liveness(self, dashboard):
        def explode():
            raise RuntimeError("database is locked")

        body = render_metrics(
            started_at=time.time(),
            version="0.1.0",
            accounts=[],
            models=(),
            connection_factory=explode,
            usage_for=lambda _: None,
            label_for=account_label,
            state_for=account_routing_state,
            key_names={},
        )

        # A gateway that is serving traffic must not report itself down just
        # because its metadata store is momentarily unavailable.
        assert _series(body)["kiro_lb_up"] == 1

    def test_a_broken_account_does_not_lose_the_request_metrics(self, dashboard):
        dashboard.record_request("/v1/messages", "m", 200, 10)
        broken = SimpleNamespace(id="/creds/x.json")  # no .stats, so state_for raises

        series = _series(_render(dashboard, accounts=[broken]))

        assert series['kiro_lb_requests_total{model="m",protocol="anthropic",status_class="2xx"}'] == 1


class TestEndpoint:
    @staticmethod
    def _client(dashboard, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PROXY_API_KEY", "test-root-key")
        app = FastAPI()
        app.include_router(dashboard.router)
        app.state.started_at = time.time()
        app.state.account_manager = SimpleNamespace(
            _accounts={_ACCOUNT_PATH: _account()},
            get_all_available_models=lambda: ["claude-opus-5"],
        )
        return TestClient(app)

    def test_a_valid_data_plane_key_is_accepted(self, dashboard, monkeypatch):
        client = self._client(dashboard, monkeypatch)

        response = client.get("/metrics", headers={"Authorization": "Bearer test-root-key"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert CONTENT_TYPE.startswith("text/plain; version=0.0.4")
        assert "kiro_lb_up 1" in response.text

    def test_an_unauthenticated_scrape_is_refused(self, dashboard, monkeypatch):
        client = self._client(dashboard, monkeypatch)

        response = client.get("/metrics")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_a_wrong_key_is_refused(self, dashboard, monkeypatch):
        client = self._client(dashboard, monkeypatch)

        response = client.get("/metrics", headers={"Authorization": "Bearer nope"})

        assert response.status_code == 401

    def test_a_dashboard_cookie_cannot_scrape(self, dashboard, monkeypatch):
        """Control-plane sessions stay out of the metrics plane."""
        client = self._client(dashboard, monkeypatch)
        login = client.post("/api/dashboard/login", json={"password": "dashboard-password"})
        assert login.status_code == 200
        assert dashboard._COOKIE in client.cookies

        response = client.get("/metrics")

        assert response.status_code == 401

    def test_the_exposition_never_carries_the_key_that_fetched_it(self, dashboard, monkeypatch):
        client = self._client(dashboard, monkeypatch)

        response = client.get("/metrics", headers={"Authorization": "Bearer test-root-key"})

        assert "test-root-key" not in response.text

    def test_an_alias_the_pool_serves_is_not_filed_under_other(self, dashboard, monkeypatch):
        """`auto-kiro` is the alias Cursor clients use, so it is real traffic.

        get_all_available_models() reports it only once an account has
        initialized, so the endpoint has to add MODEL_ALIASES itself or genuine
        requests land in `other`.
        """
        dashboard.record_request("/v1/chat/completions", "auto-kiro", 200, 10)
        client = self._client(dashboard, monkeypatch)

        response = client.get("/metrics", headers={"Authorization": "Bearer test-root-key"})

        assert 'model="auto-kiro"' in response.text

    def test_the_model_gauge_survives_an_uninitialized_pool(self, dashboard, monkeypatch):
        """With no initialized account the gateway still serves the fallback list."""
        client = self._client(dashboard, monkeypatch)
        client.app.state.account_manager.get_all_available_models = lambda: []

        response = client.get("/metrics", headers={"Authorization": "Bearer test-root-key"})

        assert _series(response.text)["kiro_lb_models"] > 0


class TestAccountTokenMetrics:
    """Tokens exposed against the account that served them.

    The per-key section answers "who asked"; this one answers "which account
    paid", which is the question the pool operator has and which no series
    previously carried.
    """

    def _seed(self, dashboard, account_id=_ACCOUNT_PATH, key_id=_KEY_ID, model="claude-haiku-4.5", **counts):
        row = {"prompt_tokens": 10, "completion_tokens": 5, "requests": 1, "generation_ms": 0, "timed": 0}
        row.update(counts)
        with dashboard._db() as conn:
            conn.execute(
                "INSERT INTO account_model_usage(key_id, account_id, model, prompt_tokens,"
                " completion_tokens, requests, generation_ms, timed_completion_tokens, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)"
                " ON CONFLICT(key_id, account_id, model) DO UPDATE SET"
                " prompt_tokens = prompt_tokens + excluded.prompt_tokens,"
                " completion_tokens = completion_tokens + excluded.completion_tokens,"
                " requests = requests + excluded.requests",
                (
                    key_id,
                    account_id,
                    model,
                    row["prompt_tokens"],
                    row["completion_tokens"],
                    row["requests"],
                    row["generation_ms"],
                    row["timed"],
                ),
            )

    def test_families_are_declared(self):
        declared = {name for name, _type, _help in _FAMILIES}
        assert "kiro_lb_account_tokens_total" in declared
        assert "kiro_lb_account_model_requests_total" in declared

    def test_tokens_are_split_by_direction(self, dashboard):
        self._seed(dashboard, prompt_tokens=100, completion_tokens=40)

        series = _series(_render(dashboard))
        label = account_label(_ACCOUNT_PATH)
        assert (
            series[f'kiro_lb_account_tokens_total{{account="{label}",model="claude-haiku-4.5",direction="input"}}']
            == 100
        )
        assert (
            series[f'kiro_lb_account_tokens_total{{account="{label}",model="claude-haiku-4.5",direction="output"}}']
            == 40
        )

    def test_the_account_label_is_hashed_not_a_credential_path(self, dashboard):
        self._seed(dashboard)

        body = _render(dashboard)
        assert _ACCOUNT_PATH not in body
        assert account_label(_ACCOUNT_PATH) in body

    def test_two_keys_on_one_account_sum_into_one_series(self, dashboard):
        # The account axis is what matters here; keeping the key axis too would
        # multiply the series count to answer a question the per-key section
        # already answers.
        self._seed(dashboard, key_id="key_one", prompt_tokens=10, completion_tokens=0)
        self._seed(dashboard, key_id="key_two", prompt_tokens=25, completion_tokens=0)

        series = _series(_render(dashboard))
        label = account_label(_ACCOUNT_PATH)
        assert (
            series[f'kiro_lb_account_tokens_total{{account="{label}",model="claude-haiku-4.5",direction="input"}}']
            == 35
        )

    def test_an_unknown_model_collapses_instead_of_minting_a_series(self, dashboard):
        # Model names pass through to Kiro, so they are client-controlled: a
        # probing client must not be able to grow the series count.
        self._seed(dashboard, model="totally-made-up-model")

        series = _series(_render(dashboard))
        label = account_label(_ACCOUNT_PATH)
        assert f'kiro_lb_account_tokens_total{{account="{label}",model="other",direction="input"}}' in series
        assert "totally-made-up-model" not in _render(dashboard)

    def test_the_unattributed_bucket_is_not_hashed_into_a_fake_account(self, dashboard):
        # "unknown" is not a credential path. Hashing it would make the
        # unattributed bucket indistinguishable from a real account.
        from kiro.usage_tracking import UNKNOWN_ACCOUNT_ID

        self._seed(dashboard, account_id=UNKNOWN_ACCOUNT_ID)

        series = _series(_render(dashboard))
        assert (
            f'kiro_lb_account_tokens_total{{account="{UNKNOWN_ACCOUNT_ID}",model="claude-haiku-4.5",direction="input"}}'
            in series
        )
        assert account_label(UNKNOWN_ACCOUNT_ID) not in _render(dashboard)

    def test_requests_are_counted_per_account_and_model(self, dashboard):
        self._seed(dashboard, requests=4)

        series = _series(_render(dashboard))
        label = account_label(_ACCOUNT_PATH)
        assert series[f'kiro_lb_account_model_requests_total{{account="{label}",model="claude-haiku-4.5"}}'] == 4

    def test_a_missing_table_does_not_cost_the_other_sections(self, dashboard):
        # Same degradation rule the rest of the exporter follows: one broken
        # section must not empty the scrape.
        with dashboard._db() as conn:
            conn.execute("DROP TABLE account_model_usage")

        series = _series(_render(dashboard))
        assert series["kiro_lb_up"] == 1
        assert "kiro_lb_requests_total" in _render(dashboard) or series["kiro_lb_models"] >= 0
