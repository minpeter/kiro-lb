# -*- coding: utf-8 -*-
"""Prometheus exposition for kiro-lb.

Everything here is derived from state the gateway already keeps: the dashboard
SQLite store (request logs, token totals per key and per account, cached account
quota) and the
live AccountManager pool. Nothing new is recorded to serve this endpoint, so
scraping cannot change the gateway's behaviour.

Three rules shape the output:

- No secrets in labels. Account IDs are credential paths and API keys are
  hashed, so both are exposed through the same short digest the dashboard shows
  (`account_label`, the key's public id). Emails and subscription titles stay
  out entirely.
- Bounded cardinality. Labels are limited to model, protocol, status class,
  routing state, and those opaque ids. Nothing derived from request content,
  error text, or a timestamp becomes a label. The `model` label is especially
  dangerous: the gateway passes unknown model names straight through to Kiro, so
  it is client-controlled and a probing client would otherwise mint a new series
  per name. Only models the pool actually serves are labelled; the rest collapse
  into `other`.
- `job` and `instance` are not set here. The homelab pushes this exposition
  through Pushgateway, which owns those two labels (`honor_labels: true` on the
  Prometheus side).
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Iterator

from kiro.model_resolver import normalize_model_name
from kiro.usage_tracking import UNKNOWN_ACCOUNT_ID

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_ROUTE_PROTOCOL = {
    "/v1/chat/completions": "openai",
    "/v1/models": "openai",
    "/v1/messages": "anthropic",
    "/v1/messages/count_tokens": "anthropic",
}

# Counters are cumulative for the life of the store; gauges describe right now.
# Declared here so the HELP/TYPE preamble stays next to the queries that fill it.
_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("kiro_lb_up", "gauge", "Always 1; scrape liveness for the gateway process."),
    ("kiro_lb_uptime_seconds", "gauge", "Seconds since the gateway process started."),
    ("kiro_lb_build_info", "gauge", "Gateway version, as a label on a constant 1."),
    ("kiro_lb_requests_total", "counter", "Data-plane requests by model, protocol and status class."),
    # A summary, not two counters: the wire names _sum/_count belong to a summary
    # or histogram, and promtool rejects them on a plain counter. No quantiles
    # are emitted, which is valid and keeps the series count flat.
    ("kiro_lb_request_latency_seconds", "summary", "Latency of successful requests by model and protocol."),
    ("kiro_lb_tokens_total", "counter", "Tokens attributed to an API key, by model and direction."),
    (
        "kiro_lb_generation_seconds_total",
        "counter",
        "Upstream generation time, by model. Denominator for tokens/sec.",
    ),
    (
        "kiro_lb_timed_output_tokens_total",
        "counter",
        "Output tokens from requests that were also timed. Numerator for tokens/sec.",
    ),
    ("kiro_lb_key_requests_total", "counter", "Requests attributed to an API key, by model."),
    (
        "kiro_lb_account_tokens_total",
        "counter",
        "Tokens attributed to the serving account, by model and direction.",
    ),
    (
        "kiro_lb_account_model_requests_total",
        "counter",
        "Requests attributed to the serving account, by model.",
    ),
    ("kiro_lb_accounts", "gauge", "Accounts in the pool by routing state."),
    ("kiro_lb_account_requests_total", "counter", "Upstream requests per account by outcome."),
    ("kiro_lb_account_failures", "gauge", "Consecutive failures feeding the circuit breaker, per account."),
    ("kiro_lb_account_eligible_in_seconds", "gauge", "Seconds until an excluded account may serve traffic again."),
    ("kiro_lb_account_quota_used", "gauge", "Credits consumed this period, per account."),
    ("kiro_lb_account_quota_limit", "gauge", "Credit allowance for the period, per account."),
    ("kiro_lb_account_quota_percent", "gauge", "Percentage of the credit allowance consumed, per account."),
    ("kiro_lb_account_quota_reset_seconds", "gauge", "Seconds until the credit allowance resets, per account."),
    ("kiro_lb_models", "gauge", "Models the pool can currently serve."),
)

# Every routing state account_routing_state can return. Emitted even at zero so
# a state that empties out reads as 0 instead of the series disappearing, which
# would otherwise make `sum by (state)` silently drop a category.
_ROUTING_STATES = (
    "available",
    "uninitialized",
    "cooling_down",
    "rate_limited",
    "quota_exhausted",
    "quota_depleted",
    "suspended",
    "auth_dead",
)


def _escape(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _line(name: str, labels: dict[str, str] | None, value: float) -> str:
    if not labels:
        return f"{name} {_number(value)}"
    rendered = ",".join(f'{key}="{_escape(str(val))}"' for key, val in labels.items())
    return f"{name}{{{rendered}}} {_number(value)}"


def _number(value: float) -> str:
    """Render a value without scientific notation or a pointless .0 tail."""
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0")


def _status_class(status_code: int) -> str:
    if status_code <= 0:
        return "unknown"
    return f"{status_code // 100}xx"


def _protocol(route: str) -> str:
    return _ROUTE_PROTOCOL.get(route, "other")


def _model(value: str | None, known: frozenset[str]) -> str:
    """Map a logged model name onto a bounded label.

    A null model means the request never got far enough to resolve one. It is
    still a real request, so it is counted under an explicit placeholder rather
    than dropped or given an empty label.

    The name is normalized first, because the log stores what the client sent:
    `claude-sonnet-4-5` and `claude-sonnet-4.5` are the same model and must not
    become two series (the live store holds 1,324 requests under the dash form
    alone).

    Anything the pool still does not serve becomes `other`. This matters because
    the resolver deliberately forwards unknown names to Kiro (it is a gateway,
    not a gatekeeper), which makes this label client-controlled: without the
    clamp, a client looping over guesses like `claude-opus-99` would grow the
    series set forever.
    """
    if not value:
        return "unknown"
    if value in known:
        return value
    normalized = normalize_model_name(value)
    return normalized if normalized in known else "other"


def _preamble() -> Iterator[str]:
    for name, kind, help_text in _FAMILIES:
        yield f"# HELP {name} {help_text}"
        yield f"# TYPE {name} {kind}"


def _request_metrics(conn: Any, known: frozenset[str]) -> Iterator[str]:
    rows = conn.execute("SELECT route, model, status_code, requests FROM request_metric_rollups").fetchall()
    # Re-aggregated in Python rather than SQL: several raw model names collapse
    # into the same label, and emitting one line per raw name would repeat a
    # series, which Prometheus rejects as a duplicate.
    counted: dict[tuple[str, str, str], int] = {}
    for row in rows:
        counted_key = (_model(row["model"], known), _protocol(row["route"]), _status_class(row["status_code"]))
        counted[counted_key] = counted.get(counted_key, 0) + row["requests"]
    for (model, protocol, status_class), requests in counted.items():
        labels = {"model": model, "protocol": protocol, "status_class": status_class}
        yield _line("kiro_lb_requests_total", labels, requests)

    # Latency is summed separately: it has no status_class dimension, because a
    # rejected request's latency says nothing about generation speed.
    latency = conn.execute("SELECT route, model, requests, latency_ms FROM request_latency_rollups").fetchall()
    timed: dict[tuple[str, str], tuple[int, int]] = {}
    for row in latency:
        timed_key = (_model(row["model"], known), _protocol(row["route"]))
        previous = timed.get(timed_key, (0, 0))
        timed[timed_key] = (previous[0] + row["latency_ms"], previous[1] + row["requests"])
    for (model, protocol), (latency_ms, requests) in timed.items():
        labels = {"model": model, "protocol": protocol}
        yield _line("kiro_lb_request_latency_seconds_sum", labels, latency_ms / 1000.0)
        yield _line("kiro_lb_request_latency_seconds_count", labels, requests)


def _token_metrics(conn: Any, key_names: dict[str, str], known: frozenset[str]) -> Iterator[str]:
    rows = conn.execute(
        "SELECT key_id, model, prompt_tokens, completion_tokens, requests, generation_ms,"
        " timed_completion_tokens FROM key_model_usage"
    ).fetchall()
    totals: dict[tuple[str, str], tuple[int, int, int]] = {}
    # Generation time carries no key dimension: throughput is a property of the
    # model and the upstream, not of who asked. Keying it per model also keeps it
    # divisible by the token counters without a many-to-one vector match.
    generation: dict[str, int] = {}
    timed_output: dict[str, int] = {}
    for row in rows:
        model = _model(row["model"], known)
        usage_key = (row["key_id"], model)
        previous_usage = totals.get(usage_key, (0, 0, 0))
        totals[usage_key] = (
            previous_usage[0] + row["prompt_tokens"],
            previous_usage[1] + row["completion_tokens"],
            previous_usage[2] + row["requests"],
        )
        generation[model] = generation.get(model, 0) + (row["generation_ms"] or 0)
        timed_output[model] = timed_output.get(model, 0) + (row["timed_completion_tokens"] or 0)
    for (key_id, model), (prompt, completion, requests) in totals.items():
        labels = {"key_id": key_id, "key_name": key_names.get(key_id, key_id), "model": model}
        yield _line("kiro_lb_tokens_total", {**labels, "direction": "input"}, prompt)
        yield _line("kiro_lb_tokens_total", {**labels, "direction": "output"}, completion)
        yield _line("kiro_lb_key_requests_total", labels, requests)
    for model, generation_ms in generation.items():
        # Emitted even at zero so the series exists from the first scrape; a
        # consumer has to clamp the denominator anyway.
        yield _line("kiro_lb_generation_seconds_total", {"model": model}, generation_ms / 1000.0)
        # Paired with the duration above so tokens/sec divides two counters that
        # cover the same requests. kiro_lb_tokens_total also includes untimed
        # requests, so dividing that instead overstates throughput.
        yield _line("kiro_lb_timed_output_tokens_total", {"model": model}, timed_output.get(model, 0))


def _account_token_metrics(conn: Any, label_for: Any, known: frozenset[str]) -> Iterator[str]:
    """Tokens attributed to the account that served them, by model.

    Summed over keys, unlike ``_token_metrics``: crossing account with key would
    multiply the series count by the number of keys to answer a question nobody
    asks of Prometheus. The account label is the hashed one, matching every other
    account series so they join.
    """
    rows = conn.execute(
        "SELECT account_id, model, SUM(prompt_tokens) AS prompt_tokens,"
        " SUM(completion_tokens) AS completion_tokens, SUM(requests) AS requests"
        " FROM account_model_usage GROUP BY account_id, model"
    ).fetchall()
    totals: dict[tuple[str, str], tuple[int, int, int]] = {}
    for row in rows:
        account_id = row["account_id"]
        # UNKNOWN_ACCOUNT_ID is not a credential path and must not be hashed, or
        # the unattributed bucket would look like a real account.
        account = account_id if account_id == UNKNOWN_ACCOUNT_ID else label_for(account_id)
        usage_key = (account, _model(row["model"], known))
        previous = totals.get(usage_key, (0, 0, 0))
        totals[usage_key] = (
            previous[0] + (row["prompt_tokens"] or 0),
            previous[1] + (row["completion_tokens"] or 0),
            previous[2] + (row["requests"] or 0),
        )
    for (account, model), (prompt, completion, requests) in totals.items():
        labels = {"account": account, "model": model}
        yield _line("kiro_lb_account_tokens_total", {**labels, "direction": "input"}, prompt)
        yield _line("kiro_lb_account_tokens_total", {**labels, "direction": "output"}, completion)
        yield _line("kiro_lb_account_model_requests_total", labels, requests)


def _account_metrics(accounts: Iterable[Any], usage_for: Any, label_for: Any, state_for: Any) -> Iterator[str]:
    counts = dict.fromkeys(_ROUTING_STATES, 0)
    lines: list[str] = []
    for account in accounts:
        state, eligible_in = state_for(account)
        counts[state] = counts.get(state, 0) + 1
        label = label_for(account.id)
        labels = {"account": label}

        lines.append(
            _line("kiro_lb_account_requests_total", {**labels, "outcome": "success"}, account.stats.successful_requests)
        )
        lines.append(
            _line("kiro_lb_account_requests_total", {**labels, "outcome": "failure"}, account.stats.failed_requests)
        )
        lines.append(_line("kiro_lb_account_failures", labels, account.failures))
        lines.append(_line("kiro_lb_account_eligible_in_seconds", labels, eligible_in))

        # Quota comes from the dashboard's cached getUsageLimits snapshot, so a
        # scrape never triggers an upstream call. Email and subscription title
        # are deliberately left out; only the numbers are exported.
        usage = usage_for(account.id) or {}
        for metric, field in (
            ("kiro_lb_account_quota_used", "currentUsage"),
            ("kiro_lb_account_quota_limit", "usageLimit"),
            ("kiro_lb_account_quota_percent", "usagePercent"),
        ):
            value = usage.get(field)
            if value is not None:
                lines.append(_line(metric, labels, float(value)))
        # Seconds, not the upstream's days: Prometheus convention is base units,
        # and promtool fails a `_days` suffix outright.
        reset_days = usage.get("daysUntilReset")
        if reset_days is not None:
            lines.append(_line("kiro_lb_account_quota_reset_seconds", labels, float(reset_days) * 86400))

    for state in _ROUTING_STATES:
        yield _line("kiro_lb_accounts", {"state": state}, counts.get(state, 0))
    yield from lines


def render_metrics(
    *,
    started_at: float,
    version: str,
    accounts: Iterable[Any],
    models: Iterable[str],
    connection_factory: Any,
    usage_for: Any,
    label_for: Any,
    state_for: Any,
    key_names: dict[str, str],
) -> str:
    """Build the full exposition. Dependencies are injected to keep this pure.

    `models` is both the value of kiro_lb_models and the allowlist that bounds
    the `model` label, so an unknown name a client invented cannot become a new
    series.

    A failure in any one section must not cost the whole scrape, so the SQLite
    sections degrade to absent series rather than a 500: a gateway that is
    serving traffic should still report `kiro_lb_up` even if its metadata store
    is momentarily locked.
    """
    known = frozenset(models)
    lines: list[str] = list(_preamble())
    lines.append(_line("kiro_lb_up", None, 1))
    lines.append(_line("kiro_lb_uptime_seconds", None, int(time.time() - started_at)))
    lines.append(_line("kiro_lb_build_info", {"version": version}, 1))
    lines.append(_line("kiro_lb_models", None, len(known)))

    try:
        with connection_factory() as conn:
            lines.extend(_request_metrics(conn, known))
            lines.extend(_token_metrics(conn, key_names, known))
            # Separate try so a failure here cannot cost the two sections above,
            # which is the same rule the account section below follows.
            try:
                lines.extend(_account_token_metrics(conn, label_for, known))
            except Exception:
                pass
    except Exception:
        pass

    try:
        lines.extend(_account_metrics(accounts, usage_for, label_for, state_for))
    except Exception:
        pass

    return "\n".join(lines) + "\n"
