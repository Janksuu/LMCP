"""Regression tests for daemon internals introduced in v3.0.2.

Covers:
- Tools cache behavior (get/set/invalidate, TTL expiry)
- Content-Length parsing (valid, invalid, negative, missing)
"""

from __future__ import annotations

import time

from lmcp.audit import AuditLogger
from lmcp.config import (
    ClientConfig,
    LmcpSettings,
    Registry,
)
from lmcp.daemon import LmcpDaemon, _parse_content_length


# --- Tools cache ---

def _make_daemon() -> LmcpDaemon:
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    registry = Registry(
        path=tmp / "registry.yaml",
        lmcp=LmcpSettings(),
        clients={},
        servers={},
    )
    audit = AuditLogger(tmp / "audit.log")
    return LmcpDaemon(registry=registry, audit=audit)


def test_tools_cache_miss_returns_none() -> None:
    daemon = _make_daemon()
    assert daemon.get_cached_tools("nonexistent") is None


def test_tools_cache_hit_returns_stored_value() -> None:
    daemon = _make_daemon()
    tools = [{"name": "t1"}, {"name": "t2"}]
    daemon.set_cached_tools("server1", tools)
    cached = daemon.get_cached_tools("server1")
    assert cached == tools


def test_tools_cache_expires_after_ttl() -> None:
    daemon = _make_daemon()
    # Shorten TTL to make the test fast
    daemon._tools_cache_ttl_s = 0.1
    daemon.set_cached_tools("server1", [{"name": "t1"}])
    assert daemon.get_cached_tools("server1") is not None
    time.sleep(0.15)
    assert daemon.get_cached_tools("server1") is None


def test_tools_cache_invalidate_clears_all() -> None:
    daemon = _make_daemon()
    daemon.set_cached_tools("s1", [{"name": "a"}])
    daemon.set_cached_tools("s2", [{"name": "b"}])
    assert daemon.get_cached_tools("s1") is not None
    assert daemon.get_cached_tools("s2") is not None
    daemon.invalidate_tools_cache()
    assert daemon.get_cached_tools("s1") is None
    assert daemon.get_cached_tools("s2") is None


# --- Content-Length parsing ---

class _FakeHandler:
    """Minimal handler stub exposing a headers dict with .get()."""
    def __init__(self, content_length: str | None) -> None:
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["content-length"] = content_length


def _handler_with_content_length(value: str | None) -> _FakeHandler:
    return _FakeHandler(value)


def test_parse_content_length_valid_positive() -> None:
    h = _handler_with_content_length("100")
    assert _parse_content_length(h) == 100


def test_parse_content_length_zero() -> None:
    h = _handler_with_content_length("0")
    assert _parse_content_length(h) == 0


def test_parse_content_length_missing_defaults_to_zero() -> None:
    h = _handler_with_content_length(None)
    assert _parse_content_length(h) == 0


def test_parse_content_length_malformed_returns_none() -> None:
    """Regression: int('abc') would crash the handler with a 500."""
    h = _handler_with_content_length("abc")
    assert _parse_content_length(h) is None


def test_parse_content_length_negative_returns_none() -> None:
    """Regression: negative Content-Length must be rejected explicitly."""
    h = _handler_with_content_length("-1")
    assert _parse_content_length(h) is None


def test_parse_content_length_empty_string_returns_none() -> None:
    h = _handler_with_content_length("")
    assert _parse_content_length(h) is None
