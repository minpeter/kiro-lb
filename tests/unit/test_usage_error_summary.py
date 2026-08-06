# -*- coding: utf-8 -*-
"""A failed usage poll is stored as short, single-line operator text.

``refresh_account_usage`` used to persist ``str(exc)[:240]``. For the common case
- ``httpx.HTTPStatusError`` - that is 188 characters across two lines, carrying
the refresh URL and an MDN link. The dashboard renders this column inside a
``whitespace-nowrap`` table cell, so a real 401 from the auth host widened the
accounts table past the viewport and pushed every later column off-screen.

The cell now clamps defensively, but the stored value is the actual fix: it is
operator-facing text, so it must be bounded, single-line, and free of
infrastructure detail nobody can act on.
"""

import importlib

import httpx
import pytest

from kiro.account_errors import CredentialDeadError

#: The exact string the live 401 produced, reproduced through httpx itself.
_REFRESH_URL = "https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken"


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    module = importlib.reload(importlib.import_module("kiro.dashboard"))
    module.initialize_dashboard_store()
    return module


def _status_error(status_code: int, url: str = _REFRESH_URL) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status_code, request=request)
    # Build the message the way httpx does, so the test is not asserting against
    # a hand-written approximation of the bug.
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("expected raise_for_status to raise")


class TestTheOriginalBugIsReproducible:
    """Guard the premise: without summarization the string really is oversized."""

    def test_raw_httpx_message_is_long_and_multiline(self):
        raw = str(_status_error(401))

        assert len(raw) > 150
        assert "\n" in raw
        assert _REFRESH_URL in raw


class TestSummarizeUsageError:
    def test_status_error_keeps_the_status_and_drops_the_url(self, dashboard):
        summary = dashboard._summarize_usage_error(_status_error(401))

        assert "401" in summary
        assert _REFRESH_URL not in summary
        assert "developer.mozilla.org" not in summary

    def test_credential_death_names_the_remedy(self, dashboard):
        summary = dashboard._summarize_usage_error(CredentialDeadError("acct", 401))

        assert "re-login required" in summary
        assert "401" in summary

    def test_timeout_reads_as_a_timeout(self, dashboard):
        summary = dashboard._summarize_usage_error(httpx.ReadTimeout("timed out"))

        assert "timed out" in summary

    def test_transport_failure_names_the_type(self, dashboard):
        summary = dashboard._summarize_usage_error(httpx.ConnectError("nope"))

        assert "ConnectError" in summary

    def test_an_empty_message_falls_back_to_the_type(self, dashboard):
        """Never store a blank cell; the reader needs *something* to act on."""
        summary = dashboard._summarize_usage_error(RuntimeError())

        assert summary == "RuntimeError"

    def test_an_arbitrary_long_message_is_bounded(self, dashboard):
        summary = dashboard._summarize_usage_error(RuntimeError("x" * 5000))

        assert len(summary) <= dashboard._MAX_USAGE_ERROR_CHARS
        assert summary.endswith("…")

    @pytest.mark.parametrize(
        "exc",
        [
            _status_error(401),
            _status_error(500),
            CredentialDeadError("acct", 401),
            httpx.ReadTimeout("slow"),
            httpx.ConnectError("down"),
            RuntimeError("line one\nline two\n\tindented"),
            RuntimeError("y" * 900),
        ],
        ids=["401", "500", "dead", "timeout", "transport", "multiline", "long"],
    )
    def test_every_summary_is_single_line_and_bounded(self, dashboard, exc):
        """The invariant the dashboard cell depends on, across every input.

        One newline is all it takes to break the row, so this is asserted for the
        whole taxonomy rather than only the case that was reported.
        """
        summary = dashboard._summarize_usage_error(exc)

        assert "\n" not in summary
        assert "\r" not in summary
        assert "\t" not in summary
        assert 0 < len(summary) <= dashboard._MAX_USAGE_ERROR_CHARS


class TestRefreshAccountUsageStoresTheSummary:
    def test_a_failed_poll_persists_the_short_form(self, dashboard, monkeypatch):
        """End to end: the row the dashboard reads must hold the summary."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        account = SimpleNamespace(id="acct-1")
        with patch.object(dashboard, "fetch_account_usage", AsyncMock(side_effect=_status_error(401))):
            result = asyncio.run(dashboard.refresh_account_usage(account))

        assert "401" in result["error"]
        assert "\n" not in result["error"]
        assert len(result["error"]) <= dashboard._MAX_USAGE_ERROR_CHARS

        cached = dashboard._cached_usage("acct-1")
        assert cached is not None
        assert cached["error"] == result["error"]
