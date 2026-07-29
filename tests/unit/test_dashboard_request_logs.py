"""Pagination contract tests for the dashboard request-log API."""

import importlib

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
