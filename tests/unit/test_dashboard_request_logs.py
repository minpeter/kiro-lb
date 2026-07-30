"""Pagination contract tests for the dashboard request-log API."""

import importlib
import time

import pytest


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    module = importlib.reload(importlib.import_module("kiro.dashboard"))
    module.initialize_dashboard_store()
    return module


def _seed(module, count: int) -> None:
    for index in range(count):
        module.record_request("/v1/chat/completions", f"model-{index}", 200, index)


def test_reports_total_and_window(dashboard):
    _seed(dashboard, 30)

    with dashboard._db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
        page = conn.execute(
            "SELECT latency_ms FROM request_logs ORDER BY id DESC LIMIT ? OFFSET ?", (10, 0)
        ).fetchall()

    assert total == 30
    # Newest first, so the highest seeded latency leads the first page.
    assert [row["latency_ms"] for row in page][:3] == [29, 28, 27]


def test_offset_pages_do_not_overlap(dashboard):
    _seed(dashboard, 12)

    with dashboard._db() as conn:
        first = conn.execute(
            "SELECT latency_ms FROM request_logs ORDER BY id DESC LIMIT ? OFFSET ?", (5, 0)
        ).fetchall()
        second = conn.execute(
            "SELECT latency_ms FROM request_logs ORDER BY id DESC LIMIT ? OFFSET ?", (5, 5)
        ).fetchall()

    first_values = {row["latency_ms"] for row in first}
    second_values = {row["latency_ms"] for row in second}
    assert len(first_values) == 5
    assert len(second_values) == 5
    assert first_values.isdisjoint(second_values)


def test_prune_removes_rows_past_retention(dashboard, monkeypatch):
    now = int(time.time())
    with dashboard._db() as conn:
        conn.executemany(
            "INSERT INTO request_logs(created_at, route, model, status_code, latency_ms) VALUES (?,?,?,?,?)",
            [
                (now - 30 * 86400, "/v1/messages", "m", 200, 10),
                (now - 8 * 86400, "/v1/messages", "m", 200, 10),
                (now - 6 * 86400, "/v1/messages", "m", 200, 10),
                (now, "/v1/messages", "m", 200, 10),
            ],
        )

    monkeypatch.setattr(dashboard, "REQUEST_LOG_RETENTION_DAYS", 7)
    removed = dashboard.prune_request_logs()

    with dashboard._db() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
        oldest = conn.execute("SELECT MIN(created_at) FROM request_logs").fetchone()[0]

    assert removed == 2
    assert remaining == 2
    assert oldest >= now - 7 * 86400


def test_prune_keeps_everything_inside_retention(dashboard, monkeypatch):
    _seed(dashboard, 5)

    monkeypatch.setattr(dashboard, "REQUEST_LOG_RETENTION_DAYS", 7)
    removed = dashboard.prune_request_logs()

    with dashboard._db() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]

    assert removed == 0
    assert remaining == 5


def test_prune_is_safe_on_an_empty_table(dashboard):
    assert dashboard.prune_request_logs() == 0
