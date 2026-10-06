"""Every test runs against a fake site with fake credentials; no request leaves the
process. The stub transport in test_server.py answers each call."""
import pytest

from jira_lean_mcp import server as s

TEST_SITE = "https://example.atlassian.net"


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    for key in ("ATLASSIAN_SITE", "ATLASSIAN_EMAIL", "ATLASSIAN_API_TOKEN", "JIRA_LEAN_ENV_FILE",
                "JIRA_LEAN_TIMEOUT", "JIRA_LEAN_DEFAULT_FIELDS", "JIRA_LEAN_READ_ONLY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATLASSIAN_SITE", TEST_SITE)
    monkeypatch.setenv("ATLASSIAN_EMAIL", "dev@example.com")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(s, "_settings", None)
    monkeypatch.setattr(s, "_client", None)
    yield
